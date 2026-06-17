"""
main_scalp_live.py - Entrypoint LIVE : scalping 3 jambes, DAX UNIQUEMENT.
=========================================================================
Bascule decidee le 16/06/2026 : on arrete le copieur TRADAMAX sur le compte
REEL (LUZQM) et on dedie 100% du compte au scalp 3 jambes valide en demo.

Differences avec main_scalp_demo.py :
  - GARDE-FOU INVERSE : refuse de demarrer SAUF si compte LIVE *et* double
    confirmation explicite (ALLOW_LIVE_SCALP=true + CONFIRM_LIVE_SCALP=true).
  - On OUVRE uniquement le DAX. CAC40 reste abonne/structure (sans etre trade)
    car le filtre tendance M5 + le controle de correlation DAX<->CAC en dependent.
  - FIN DE SEANCE (16h UTC) : cloture des positions DAX GAGNANTES seulement
    (les perdantes restent gerees par leur SL/TP IG, peuvent passer la nuit).
    Decision operateur 16/06 ("cloturer si gagnant").

Modele (multileg.py) : LIVE = 2 jambes TP 3/6 xATR (sous-capitalise : le reel ne
marge pas 3 jambes -> INSUFFICIENT_FUNDS sur la 3e). SL commun 2xATR, SL -> entree
+/- 0.5xATR des que TP1 touche. 0.5 lot/jambe. (La demo garde 3 jambes 3/4.5/6.)
Backtest 17/06 : 2 jambes 3/6 ~= 3 jambes (net 3279 vs 3196, Sharpe 1.85 vs 1.79).
"""
from __future__ import annotations

import asyncio
import os
from datetime import timezone, datetime

from loguru import logger

from config import config
from logger_setup import setup_logger
from data_feed import IGDataFeed
from instruments import INSTRUMENTS as ALL_INSTRUMENTS
from order_executor import build_executor
from strategy_engine import build_engine_for
from s10_runner import build_s10_runner
from trade_logger import TradeLogger
from scalp_strategy import ScalpLiveStrategy, ScalpParamsLive
from multileg import build_legs, MultiLegManager

COOLDOWN_S = float(os.getenv("SCALP_COOLDOWN_S", "60"))   # delai mini entre 2 entrees / instrument

# DAX seul est TRADE ; CAC40 garde en flux pour les filtres (tendance + correlation).
FEED_NAMES  = ("DAX", "CAC40")
TRADE_ONLY  = {"DAX"}
INSTRUMENTS = {k: ALL_INSTRUMENTS[k] for k in FEED_NAMES if k in ALL_INSTRUMENTS}
SUBSCRIPTIONS = [{"epic": inst.epic, "scale": inst.scale} for inst in INSTRUMENTS.values()]

# Fin de seance : cloturer les positions DAX gagnantes (>= ce seuil de pts).
EOD_HOUR_UTC      = INSTRUMENTS["DAX"].session_end   # 16
EOD_MIN_PROFIT_PT = float(os.getenv("EOD_MIN_PROFIT_PT", "0.0"))

# CAPITAL : le compte reel ne marge pas 3 jambes DAX (INSUFFICIENT_FUNDS sur la 3e).
# -> 2 jambes ASSUMEES : TP1 rapide (3xATR) + runner (6xATR), SL commun 2xATR,
#    SL -> entree+0.5xATR des TP1 (gestion multileg inchangee). Decision 17/06.
LIVE_TPS = [3.0, 6.0]


def _live_authorized() -> bool:
    """LIVE autorise seulement avec double confirmation explicite."""
    if config.ig.account_type.upper() != "LIVE":
        logger.error("REFUS : compte non-LIVE (%s). main_scalp_live est reserve au reel."
                     % config.ig.account_type)
        return False
    if os.getenv("ALLOW_LIVE_SCALP", "").lower() != "true" or \
       os.getenv("CONFIRM_LIVE_SCALP", "").lower() != "true":
        logger.error("REFUS : ALLOW_LIVE_SCALP/CONFIRM_LIVE_SCALP non confirmes. "
                     "Scalp LIVE non arme. Arret.")
        return False
    logger.warning("!!! SCALP LIVE ARME SUR COMPTE REEL !!! account=%s | DAX uniquement | "
                   "%d jambes TP%s xATR | 0.5 lot/jambe | EOD close-gagnants %dh UTC" %
                   (config.ig.account_id, len(LIVE_TPS), LIVE_TPS, EOD_HOUR_UTC))
    return True


async def main() -> None:
    setup_logger(config.logs.level, config.logs.path)
    logger.info("=== SCALP LIVE DAX (%d jambes TP%s xATR) : demarrage ===" % (len(LIVE_TPS), LIVE_TPS))
    logger.info(f"mode={config.trading.mode} | account_type={config.ig.account_type} "
                f"| account={config.ig.account_id} | feed={list(INSTRUMENTS)} "
                f"| trade={sorted(TRADE_ONLY)}")

    if not _live_authorized():
        return

    # --- Feed live (DAX + CAC40) ---
    feed = IGDataFeed(config=config, subscriptions=SUBSCRIPTIONS)
    await feed.start()
    logger.info("IGDataFeed demarre (LIVE)")

    # --- S10Runner (bougies S10 + MarketState/tendance M5 DAX & CAC) ---
    s10_runner = build_s10_runner(feed, INSTRUMENTS, config)
    scalp_q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    s10_runner.scalp_out = scalp_q

    # --- RiskManagers + OrderExecutor (session IG reelle partagee avec le feed) ---
    capital = float(os.getenv("BONAZA_CAPITAL", "1000"))
    rm_map = {}
    for name, inst in INSTRUMENTS.items():
        _, rm = build_engine_for(inst, config, capital=capital)
        rm_map[name] = rm
    trade_log = TradeLogger()
    try:
        trade_log.start_session(mode="LIVE_SCALP_DAX")
    except Exception:
        pass
    executor = build_executor(config, rm_map, trade_logger=trade_log)
    ok = await executor.connect(ig_service=feed._ig_service)
    if not ok:
        logger.error("Connexion OrderExecutor (LIVE) echouee. Arret.")
        await feed.stop()
        return
    executor._feed = feed
    try:
        n = await executor.reload_open_positions()
        logger.info(f"Positions reelles deja ouvertes rechargees : {n}")
    except Exception as e:
        logger.warning(f"reload_open_positions : {e}")

    # --- Strategie scalp + gestion 3 jambes ---
    strat = ScalpLiveStrategy(INSTRUMENTS, s10_runner.market_state, ScalpParamsLive())
    mlm = MultiLegManager(executor, feed, INSTRUMENTS)
    last_entry = {name: 0.0 for name in INSTRUMENTS}

    async def scalp_loop() -> None:
        while True:
            name, candle = await scalp_q.get()
            try:
                if name not in TRADE_ONLY:          # CAC40 : flux/filtre seulement, jamais trade
                    continue
                inst = INSTRUMENTS.get(name)
                if inst is None:
                    continue
                ts = candle.timestamp
                hour = ts.astimezone(timezone.utc).hour if ts.tzinfo else ts.hour
                if not (inst.session_start <= hour < inst.session_end):
                    continue
                if any(p.instrument == name for p in executor.open_positions()):
                    continue
                now = ts.timestamp()
                if now - last_entry[name] < COOLDOWN_S:
                    continue
                setup = strat.evaluate(name, candle)
                if setup is None:
                    continue
                atr = s10_runner.market_state.get(name, "M5").last_indicators.get("atr")
                if not atr or atr <= 0:
                    continue
                entry = feed.get_price(inst.epic) or candle.close
                deal_ids = []
                for leg in build_legs(setup.direction, entry, atr, name, tps=LIVE_TPS):
                    d = await executor._handle_signal(leg, instrument=name, exact_levels=True)
                    deal_ids.append(d)
                if any(deal_ids):
                    last_entry[name] = now
                    mlm.register(name, setup.direction, entry, atr, deal_ids)
                    logger.info(f"[SCALP-LIVE] {name} {len(LIVE_TPS)} JAMBES | {setup.direction.value} "
                                f"E={entry:.2f} ATR={atr:.2f} deals={[d for d in deal_ids if d]}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SCALP-LIVE] erreur {name} : {e}")

    async def eod_close_loop() -> None:
        """A partir de EOD_HOUR_UTC : cloture les positions DAX GAGNANTES.
        Les perdantes restent (gerees par leur SL/TP IG)."""
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now(timezone.utc)
                if now.hour != EOD_HOUR_UTC:
                    continue
                for p in [x for x in executor.open_positions() if x.instrument in TRADE_ONLY]:
                    epic = getattr(p, "epic", "") or INSTRUMENTS[p.instrument].epic
                    price = feed.get_price(epic)
                    if not price or price <= 0:
                        continue
                    long = p.direction in ("LONG", "BUY")
                    profit = (price - p.entry_level) if long else (p.entry_level - price)
                    if profit > EOD_MIN_PROFIT_PT:
                        logger.info("[SCALP-LIVE] EOD %dh : %s %s gagnant +%.1f pts -> cloture"
                                    % (EOD_HOUR_UTC, p.instrument, p.deal_id, profit))
                        await executor.close_position(p.deal_id, "EOD_WINNER_%+.1fpts" % profit)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[SCALP-LIVE] eod_close : %s" % e)

    async def status_loop() -> None:
        while True:
            try:
                await asyncio.sleep(120)
                sm = s10_runner.market_state.trend_summary()
                pos = ", ".join(f"{p.instrument}:{getattr(p,'direction','?')}"
                                for p in executor.open_positions()) or "aucune"
                logger.info(f"[SCALP-LIVE] S10 bars={s10_runner.bars} | tendances {sm} "
                            f"| positions: {pos}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SCALP-LIVE] status : {e}")

    tasks = [
        asyncio.create_task(s10_runner.run(), name="s10_collector"),
        asyncio.create_task(executor.run_poll(), name="executor_poll"),
        asyncio.create_task(executor.session_keeper(), name="session_keeper"),
        asyncio.create_task(executor.close_resolver(), name="close_resolver"),
        asyncio.create_task(scalp_loop(), name="scalp_loop"),
        asyncio.create_task(mlm.run(), name="multileg"),
        asyncio.create_task(eod_close_loop(), name="eod_close"),
        asyncio.create_task(status_loop(), name="status_loop"),
    ]
    logger.info("[SCALP-LIVE] toutes les taches lancees (DAX live, CAC en filtre)")
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        logger.warning(f"[SCALP-LIVE] tache terminee : {[t.get_name() for t in done]}")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await executor.disconnect()
        except Exception:
            pass
        await feed.stop()
        logger.info("=== SCALP LIVE DAX : arret ===")


if __name__ == "__main__":
    asyncio.run(main())
