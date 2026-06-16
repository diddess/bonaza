"""
main_scalp_demo.py - Entrypoint MINIMAL : scalping S10+M5 sur compte DEMO.
=========================================================================
Execute UNIQUEMENT la strategie de scalping (cf scalp_strategy.py), isole des
autres moteurs (S8/S5/S3/AI/breakout). A lancer dans un conteneur dedie
(bonaza_demo) avec .env.demo (IG_ACCOUNT_TYPE=DEMO, BONAZA_MODE=PAPER).

Pipeline reutilise tel quel :
  IGDataFeed (flux live) -> S10Runner (bougies S10 + MarketState/tendance M5 en RAM)
    -> ScalpLiveStrategy (momentum 3xS10 + filtre tendance M5)
    -> OrderExecutor._handle_signal(exact_levels=True) -> ordre DEMO avec bracket SL/TP ATR M5.

GARDE-FOU : refuse de demarrer si le compte n'est pas DEMO.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timezone

from loguru import logger

from config import config
from logger_setup import setup_logger
from data_feed import IGDataFeed
from instruments import INSTRUMENTS, SUBSCRIPTIONS
from order_executor import build_executor
from strategy_engine import build_engine_for
from s10_runner import build_s10_runner
from trade_logger import TradeLogger
from scalp_strategy import ScalpLiveStrategy, ScalpParamsLive
from multileg import build_legs, MultiLegManager

COOLDOWN_S = float(os.getenv("SCALP_COOLDOWN_S", "60"))   # delai mini entre 2 entrees / instrument


async def main() -> None:
    setup_logger(config.logs.level, config.logs.path)
    logger.info("=== SCALP DEMO S10+M5 : demarrage ===")
    logger.info(f"mode={config.trading.mode} | account_type={config.ig.account_type} "
                f"| account={config.ig.account_id} | instruments={list(INSTRUMENTS)}")

    # --- GARDE-FOU : jamais sur un compte reel ---
    if config.ig.account_type.upper() != "DEMO":
        logger.error(f"REFUS : compte non-DEMO ({config.ig.account_type}). "
                     f"Cet entrypoint est reserve au demo. Arret.")
        return

    # --- Feed live ---
    feed = IGDataFeed(config=config, subscriptions=SUBSCRIPTIONS)
    await feed.start()
    logger.info("IGDataFeed demarre (demo)")

    # --- S10Runner (bougies S10 + MarketState/tendance M5) ---
    s10_runner = build_s10_runner(feed, INSTRUMENTS, config)
    scalp_q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    s10_runner.scalp_out = scalp_q   # publie chaque bougie S10 finalisee

    # --- RiskManagers + OrderExecutor (session IG demo partagee avec le feed) ---
    capital = float(os.getenv("BONAZA_CAPITAL", "1000"))
    rm_map = {}
    for name, inst in INSTRUMENTS.items():
        _, rm = build_engine_for(inst, config, capital=capital)
        rm_map[name] = rm
    trade_log = TradeLogger()
    try:
        trade_log.start_session(mode="DEMO_SCALP")
    except Exception:
        pass
    executor = build_executor(config, rm_map, trade_logger=trade_log)
    ok = await executor.connect(ig_service=feed._ig_service)
    if not ok:
        logger.error("Connexion OrderExecutor (demo) echouee. Arret.")
        await feed.stop()
        return
    executor._feed = feed
    try:
        n = await executor.reload_open_positions()
        logger.info(f"Positions demo deja ouvertes rechargees : {n}")
    except Exception as e:
        logger.warning(f"reload_open_positions : {e}")

    # --- Strategie scalp + verrou de prise de benefice (regles du copieur) ---
    strat = ScalpLiveStrategy(INSTRUMENTS, s10_runner.market_state, ScalpParamsLive())
    mlm = MultiLegManager(executor, feed, INSTRUMENTS)
    last_entry = {name: 0.0 for name in INSTRUMENTS}

    async def scalp_loop() -> None:
        while True:
            name, candle = await scalp_q.get()
            try:
                inst = INSTRUMENTS.get(name)
                if inst is None:
                    continue
                ts = candle.timestamp
                hour = ts.astimezone(timezone.utc).hour if ts.tzinfo else ts.hour
                # filtre de session de l'instrument
                if not (inst.session_start <= hour < inst.session_end):
                    continue
                # une seule position par instrument
                if any(p.instrument == name for p in executor.open_positions()):
                    continue
                # cooldown entre entrees
                now = ts.timestamp()
                if now - last_entry[name] < COOLDOWN_S:
                    continue
                setup = strat.evaluate(name, candle)
                if setup is None:
                    continue
                # 3 JAMBES : ATR M5 + prix live -> TP echelonnes 3/4.5/6, SL commun 2xATR
                atr = s10_runner.market_state.get(name, "M5").last_indicators.get("atr")
                if not atr or atr <= 0:
                    continue
                entry = feed.get_price(inst.epic) or candle.close
                deal_ids = []
                for leg in build_legs(setup.direction, entry, atr, name):
                    d = await executor._handle_signal(leg, instrument=name, exact_levels=True)
                    deal_ids.append(d)
                if any(deal_ids):
                    last_entry[name] = now
                    mlm.register(name, setup.direction, entry, atr, deal_ids)
                    logger.info(f"[SCALP-DEMO] {name} 3 JAMBES | {setup.direction.value} "
                                f"E={entry:.2f} ATR={atr:.2f} deals={[d for d in deal_ids if d]}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SCALP-DEMO] erreur {name} : {e}")

    async def status_loop() -> None:
        while True:
            try:
                await asyncio.sleep(120)
                sm = s10_runner.market_state.trend_summary()
                pos = ", ".join(f"{p.instrument}:{getattr(p,'direction','?')}"
                                for p in executor.open_positions()) or "aucune"
                logger.info(f"[SCALP-DEMO] S10 bars={s10_runner.bars} | tendances {sm} "
                            f"| positions: {pos}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SCALP-DEMO] status : {e}")

    tasks = [
        asyncio.create_task(s10_runner.run(), name="s10_collector"),
        asyncio.create_task(executor.run_poll(), name="executor_poll"),
        asyncio.create_task(executor.session_keeper(), name="session_keeper"),
        asyncio.create_task(executor.close_resolver(), name="close_resolver"),
        asyncio.create_task(scalp_loop(), name="scalp_loop"),
        asyncio.create_task(mlm.run(), name="multileg"),
        asyncio.create_task(status_loop(), name="status_loop"),
    ]
    logger.info("[SCALP-DEMO] toutes les taches lancees")
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        logger.warning(f"[SCALP-DEMO] tache terminee : {[t.get_name() for t in done]}")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await executor.disconnect()
        except Exception:
            pass
        await feed.stop()
        logger.info("=== SCALP DEMO S10+M5 : arret ===")


if __name__ == "__main__":
    asyncio.run(main())
