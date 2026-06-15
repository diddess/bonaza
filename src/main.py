"""
main.py - Bonaza Multi-instruments
====================================
"""
from __future__ import annotations

import argparse
import asyncio
import signal
from datetime import datetime, timezone

from loguru import logger

from ai_trading_agent import build_ai_agent
from boost_manager import BoostManager
from config import config, validate_config
from data_feed import IGDataFeed
from engines_control import is_bv3_enabled, is_ai_enabled, is_portfolio_enabled
from instruments import INSTRUMENTS, SUBSCRIPTIONS
from logger_setup import setup_logger
from order_executor import build_executor
from strategies.portfolio_runner import build_portfolio_runner
from strategy_engine import build_engine_for
from telegram_alerts import alerts as telegram
from trade_logger import TradeLogger
from warmup_loader import warmup_from_parquet
import os as _os

# Capital de reference pour le RM (sizing + DD%). En LIVE doit refleter la
# balance reelle IG. Surcharge via env BONAZA_CAPITAL (priorise sur le hardcode).
CAPITAL = float(_os.getenv("BONAZA_CAPITAL", "500.0"))


async def run_all(force_dry: bool = False) -> None:

    logger.info("=" * 60)
    logger.info("BONAZA MULTI-INSTRUMENTS — SETUP_B Bv3")
    logger.info(f"Compte     : {config.ig.account_id} ({config.ig.account_type})")
    logger.info(f"Capital ref: {CAPITAL:,.0f} EUR")
    for name, inst in INSTRUMENTS.items():
        mode = "DRY_RUN (force)" if force_dry else inst.mode
        logger.info(
            f"  {name:8s} : {inst.tf} | {mode:8s} | "
            f"session {inst.session_start}h-{inst.session_end}h UTC | {inst.epic}"
        )
    logger.info("=" * 60)

    # ===== ETAT DU VERROU ANTI-LIVE (log critique au demarrage) =====
    ig_type    = config.ig.account_type.upper()
    is_real    = ig_type not in ("DEMO",)
    bonaza_md  = config.trading.mode.upper()
    live_auth  = config.security.live_authorized

    if is_real or bonaza_md == "LIVE":
        if live_auth:
            logger.warning(
                f"!!! MODE LIVE ACTIF ET AUTORISE !!! "
                f"IG_ACCOUNT_TYPE={ig_type} BONAZA_MODE={bonaza_md} | "
                f"ALLOW_LIVE_TRADING=true + CONFIRM_LIVE_TRADING OK"
            )
            try:
                from telegram_alerts import alerts as _tg
                _tg().send(
                    f"BONAZA DEMARRE EN MODE LIVE\n"
                    f"IG_ACCOUNT_TYPE={ig_type} BONAZA_MODE={bonaza_md}\n"
                    f"Verrou OK - ordres reels possibles",
                    parse_mode=None,
                )
            except Exception:
                pass
        else:
            logger.critical(
                f"[LIVE BLOQUE AU DEMARRAGE] compte reel detecte mais verrou KO. "
                f"IG_ACCOUNT_TYPE={ig_type} BONAZA_MODE={bonaza_md} | "
                f"ALLOW_LIVE_TRADING={config.security.allow_live} "
                f"CONFIRM_LIVE_TRADING={'OK' if config.security.confirm_live=='I_UNDERSTAND_THE_RISK' else 'MANQUANT'}"
            )
            try:
                from telegram_alerts import alerts as _tg
                _tg().send(
                    f"BONAZA DEMARRE - LIVE BLOQUE\n"
                    f"Compte {ig_type} mais verrou KO - aucun ordre ne partira",
                    parse_mode=None,
                )
            except Exception:
                pass
    else:
        logger.info(f"Mode {bonaza_md} sur compte {ig_type} — pas de verrou live requis")

    if not force_dry and not validate_config():
        logger.error("Configuration invalide. Verifier .env")
        return

    candle_queues = {name: asyncio.Queue(maxsize=200) for name in INSTRUMENTS}
    signal_queues = {name: asyncio.Queue(maxsize=50)  for name in INSTRUMENTS}
    epic_to_name  = {inst.epic: name for name, inst in INSTRUMENTS.items()}

    engines = {}
    rm_map  = {}
    for name, inst in INSTRUMENTS.items():
        engine, rm = build_engine_for(inst, config, capital=CAPITAL)
        engines[name] = engine
        rm_map[name]  = rm

    for name, inst in INSTRUMENTS.items():
        n = await warmup_from_parquet(engines[name], instrument=name, tf=inst.tf)
        if n:
            logger.info(f"[{name}] Warmup : {n} barres")
        else:
            logger.warning(f"[{name}] Pas de donnees historiques")

    trade_log = TradeLogger()
    mode_label = "DRY_RUN" if force_dry else "MULTI"
    trade_log.start_session(mode=mode_label)

    executor = build_executor(config, rm_map, trade_logger=trade_log)

    feed = IGDataFeed(config=config, subscriptions=SUBSCRIPTIONS)
    await feed.start()

    any_paper = (not force_dry) and any(inst.is_paper for inst in INSTRUMENTS.values())
    if any_paper:
        ok = await executor.connect(ig_service=feed._ig_service)
        if not ok:
            logger.error("Connexion executor échouée.")
            await feed.stop()
            return
        # Donner au feed a l'executor pour le gating MARKET_STATE avant ordre
        executor._feed = feed
        # Recharger les positions deja ouvertes chez IG (anti-orphelin au redeploiement)
        try:
            await executor.reload_open_positions()
        except Exception as e:
            logger.warning(f"reload_open_positions au demarrage : {e}")
        paper_names = [n for n, i in INSTRUMENTS.items() if i.is_paper]
        logger.info(f"OrderExecutor prêt | session partagée avec data_feed | PAPER: {paper_names}")

    # --- Agent IA Claude (optionnel, active via AI_AGENT_ENABLED=true) ---
    # 2026-05-30 : par defaut HARD-LOCKED OFF via engines_control.AI_HARD_LOCK,
    # remplace par le PortfolioRunner deterministe ci-dessous.
    ai_instrument = config.agent.instrument
    ai_agent = None
    if not is_ai_enabled():
        logger.info("[AI] Agent IA desactive via engines_control (AI_HARD_LOCK actif)")
    elif config.agent.is_ready() and ai_instrument in INSTRUMENTS:
        try:
            ai_agent = build_ai_agent(
                config, rm_map[ai_instrument], instrument=ai_instrument
            )
            # Warmup l'agent avec les bougies historiques du buffer Bv3 (sans tracer)
            for c in list(engines[ai_instrument]._buffer)[-300:]:
                ai_agent.add_candle(c)
            logger.info(
                f"[AI] Agent IA active sur {ai_instrument} | modele={config.agent.model} | "
                f"intervalle={config.agent.interval_sec}s | buffer warmup={len(ai_agent._buffer)}"
            )
        except Exception as e:
            logger.error(f"[AI] Impossible de demarrer l'agent IA : {e}")
            ai_agent = None
    elif config.agent.enabled and not config.agent.api_key:
        logger.warning("[AI] AI_AGENT_ENABLED=true mais ANTHROPIC_API_KEY manquant -> agent desactive")

    # --- PortfolioRunner deterministe (S8 + S5 + S3) ---
    portfolio_runner = None
    if is_portfolio_enabled():
        try:
            portfolio_runner = build_portfolio_runner(config, rm_map)
            # Warmup avec bougies historiques des buffers Bv3
            warmup_loaded = 0
            for inst_name in ("XAUUSD", "CAC40", "DAX"):
                if inst_name in engines:
                    for c in list(engines[inst_name]._buffer)[-1500:]:
                        portfolio_runner.add_candle(c)
                    warmup_loaded += 1
            logger.info(
                f"[Portfolio] Runner active | {warmup_loaded} instruments warmed | "
                f"strats : {portfolio_runner.get_status()['strategies']}"
            )
        except Exception as e:
            logger.error(f"[Portfolio] Impossible de demarrer le runner : {e}")
            portfolio_runner = None

    # -----------------------------------------------------------------------
    # Tâches asyncio
    # -----------------------------------------------------------------------

    async def dispatcher_task():
        try:
            async for candle in feed.iter_candles():
                name = epic_to_name.get(candle.epic)
                if name and name in candle_queues:
                    await candle_queues[name].put(candle)
                # Route aussi vers l'agent IA (lecture seule, parallele a Bv3)
                if ai_agent is not None and name == ai_instrument:
                    ai_agent.add_candle(candle)
                # Route aussi vers le PortfolioRunner (XAUUSD + CAC40 + DAX)
                if portfolio_runner is not None and name in ("XAUUSD", "CAC40", "DAX"):
                    portfolio_runner.add_candle(candle)
        except asyncio.CancelledError:
            pass
        finally:
            await feed.stop()
            for q in candle_queues.values():
                await q.put(None)

    async def engine_task(name: str):
        # Multiplexeur Bv3 : engine emet TOUJOURS dans une queue interne,
        # une coroutine relais decide si on forward vers signal_queue ou trash
        # selon (a) le static instruments.bv3_enabled (b) le toggle runtime
        # engines_control.is_bv3_enabled().
        inst = INSTRUMENTS[name]
        internal_q: asyncio.Queue = asyncio.Queue(maxsize=50)

        async def _relay():
            while True:
                setup = await internal_q.get()
                if setup is None:
                    await signal_queues[name].put(None)
                    return
                # Filtre static (bv3 desactive sur cet instrument par config)
                if not inst.bv3_enabled:
                    continue
                # Filtre runtime (toggle global /engines bv3 off)
                if not is_bv3_enabled():
                    logger.debug(f"[{name}] Bv3 signal ignore (engines_control bv3=False)")
                    continue
                await signal_queues[name].put(setup)

        relay_task = asyncio.create_task(_relay(), name=f"relay_{name}")
        try:
            await engines[name].run(candle_queues[name], internal_q)
        finally:
            await internal_q.put(None)
            await relay_task

    async def signal_task(name: str):
        inst     = INSTRUMENTS[name]
        is_paper = inst.is_paper and not force_dry
        log_mode = "PAPER" if is_paper else "DRY_RUN"

        while True:
            setup = await signal_queues[name].get()
            if setup is None:
                break
            sig_id = trade_log.log_signal(
                setup=setup, mode=log_mode, instrument=name, tf=inst.tf
            )
            if is_paper:
                # Notification Telegram (silencieuse si non configure)
                source_lbl = "AI" if "AI_AGENT" in (setup.reason or "") else "Bv3"
                telegram().signal_emitted(
                    source=source_lbl, instrument=name,
                    direction=setup.direction.value,
                    entry=setup.entry, sl=setup.stop_loss, tp=setup.take_profit,
                    rr=setup.rr_ratio, size=setup.size,
                    reason=setup.reason[:200] if setup.reason else "",
                )
                logger.info(
                    f"[{name}] PAPER #{sig_id} {setup.direction.value} | "
                    f"E={setup.entry:.2f} SL={setup.stop_loss:.2f} "
                    f"TP={setup.take_profit:.2f} R:R={setup.rr_ratio:.2f} "
                    f"size={setup.size:.2f}"
                )
                await executor._handle_signal(setup, signal_id=sig_id, instrument=name)
            else:
                logger.info(
                    f"[{name}] DRY_RUN #{sig_id} {setup.direction.value} | "
                    f"E={setup.entry:.2f} SL={setup.stop_loss:.2f} "
                    f"TP={setup.take_profit:.2f} R:R={setup.rr_ratio:.2f} | "
                    f"{setup.reason}"
                )

    # Consumer des events TRADE stream (CONFIRMS/OPU/WOU)
    async def trade_events_consumer():
        import json as _json
        from pathlib import Path as _Path
        out = _Path(config.db.path).parent / "trade_events.jsonl"
        try:
            while True:
                evt = await feed.trade_event_queue.get()
                try:
                    # memorise les clotures OPU pour _sync_positions (prix IG exact)
                    try:
                        executor.note_trade_event(evt)
                    except Exception as e:
                        logger.debug(f"[TRADE-stream] note_trade_event : {e}")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with out.open("a", encoding="utf-8") as f:
                        f.write(_json.dumps(evt, default=str) + "\n")
                    # Log court : type d'event detecte
                    for k in ("opu", "confirms", "wou"):
                        v = evt.get(k)
                        if v:
                            logger.info(f"[TRADE-stream] {k.upper()} -> log {out.name}")
                            break
                except Exception as e:
                    logger.error(f"[TRADE-stream] erreur ecriture : {e}")
                feed.trade_event_queue.task_done()
        except asyncio.CancelledError:
            return

    # Consumer des changements MARKET_STATE - alerte Telegram si EDITS_ONLY/CLOSED
    async def market_state_consumer():
        try:
            while True:
                evt = await feed.market_state_queue.get()
                state = evt.get("state")
                epic = evt.get("epic")
                previous = evt.get("previous")
                if state in ("EDITS_ONLY", "CLOSED", "OFFLINE", "SUSPENDED"):
                    msg = (f"MARCHE {epic}: {previous} -> {state}\n"
                           f"Aucun nouvel ordre ne devrait etre tente sur cet EPIC.")
                    logger.warning(f"[MARKET-stream] {msg}")
                    try:
                        telegram().send(msg, parse_mode=None)
                    except Exception:
                        pass
                feed.market_state_queue.task_done()
        except asyncio.CancelledError:
            return

    # Status writer pour Telegram bot (lit data/status.json toutes les 30s)
    async def status_writer_task():
        import json as _json
        from pathlib import Path as _Path
        status_path = _Path(config.db.path).parent / "status.json"
        while True:
            try:
                payload = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "engines": {n: engines[n].get_status() for n in INSTRUMENTS},
                    "rm_metrics": {
                        n: rm_map[n].get_metrics().to_dict() for n in INSTRUMENTS
                    },
                    "ai_agent": ai_agent.get_status() if ai_agent else None,
                    "portfolio": portfolio_runner.get_status() if portfolio_runner else None,
                    "executor": (executor.get_status()
                                 if any_paper else None),
                    "boost": boost_manager.status_dict(),
                }
                status_path.write_text(
                    _json.dumps(payload, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.debug(f"status_writer erreur : {e}")
            await asyncio.sleep(30)

    # ===== Boost manager : hot-swap modele selon calendrier + triggers =====
    boost_manager = BoostManager(
        engines=engines,
        risk_manager=rm_map.get("XAUUSD"),
        order_executor=(executor if any_paper else None),
        telegram_sender=telegram().send,
    )
    await boost_manager.calendar.refresh()  # premier fetch sync au boot

    async def boost_manager_task():
        # Premier tick rapide (3s) puis cadence 60s
        await asyncio.sleep(3)
        while True:
            try:
                await boost_manager.tick()
            except Exception as e:
                logger.error(f"[BOOST] tick erreur : {e}")
            await asyncio.sleep(60)

    async def status_task():
        while True:
            await asyncio.sleep(3600)
            for name, engine in engines.items():
                st = engine.get_status()
                logger.info(
                    f"[{name}] bars={st['bar_count']} | "
                    f"warmup={st['warmup_pct']:.0f}% | "
                    f"sig={st['signals_emitted']} | "
                    f"oos={st['signals_blocked_oos']} "
                    f"adx={st['signals_blocked_adx']}"
                )

    async def daily_loss_guard(limit_eur: float = 300.0):
        """Coupe-circuit : si la perte REALISEE du jour (trades clotures) depasse
        -limit_eur, declenche le kill switch (bloque tout nouvel ordre) + alerte.
        NE compte PAS le flottant des positions ouvertes. Ne ferme pas les positions."""
        import sqlite3
        from pathlib import Path as _P
        flag = _P(config.db.path).parent / "kill_switch.flag"
        logger.info(f"[GUARD] Coupe-circuit perte REALISEE journaliere actif : seuil -{limit_eur:.0f} EUR")
        while True:
            await asyncio.sleep(30)
            try:
                if flag.exists():
                    continue
                realized = 0.0
                try:
                    con = sqlite3.connect(config.db.path)
                    r = con.execute("SELECT COALESCE(SUM(pnl_eur),0) FROM trades "
                                    "WHERE status='CLOSED' AND date(ts_close)=date('now')").fetchone()
                    realized = float(r[0] or 0.0); con.close()
                except Exception:
                    pass
                if realized <= -abs(limit_eur):
                    msg = (f"COUPE-CIRCUIT perte REALISEE du jour {realized:+.0f} EUR "
                           f"<= -{limit_eur:.0f} -> KILL SWITCH, plus aucun ordre. /unkill pour reprendre.")
                    logger.critical(f"[GUARD] {msg}")
                    try: flag.write_text(f"DAILY_LOSS {int(realized)}EUR")
                    except Exception: pass
                    try: telegram().send("🛑 " + msg, parse_mode=None)
                    except Exception: pass
            except Exception as e:
                logger.error(f"[GUARD] erreur : {e}")

    # --- Collecteur S10 (capture ticks 10s + stockage + reconstitution indicateurs) ---
    # Observabilite uniquement : consomme le flux de ticks du feed partage (aucune
    # nouvelle session IG), n'emet aucun ordre. Gate par S10_COLLECTOR_ENABLED (defaut on).
    s10_runner = None
    if _os.getenv("S10_COLLECTOR_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
        try:
            from s10_runner import build_s10_runner
            s10_runner = build_s10_runner(feed, INSTRUMENTS, config)
            logger.info("[S10] collecteur S10 active (capture + reconstitution indicateurs)")
        except Exception as e:
            logger.error(f"[S10] impossible de demarrer le collecteur : {e}")
            s10_runner = None
    else:
        logger.info("[S10] collecteur S10 desactive (S10_COLLECTOR_ENABLED=false)")

    shutdown_event = asyncio.Event()

    def _shutdown(sig, frame):
        logger.info(f"Arret (signal {sig})...")
        shutdown_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    tasks = [
        asyncio.create_task(dispatcher_task(),         name="dispatcher"),
        asyncio.create_task(status_task(),             name="status"),
        asyncio.create_task(status_writer_task(),      name="status_writer"),
        asyncio.create_task(boost_manager_task(),      name="boost_manager"),
        asyncio.create_task(trade_events_consumer(),   name="trade_events_consumer"),
        asyncio.create_task(market_state_consumer(),   name="market_state_consumer"),
    ]
    if s10_runner is not None:
        tasks.append(asyncio.create_task(s10_runner.run(), name="s10_collector"))
    for name in INSTRUMENTS:
        tasks.append(asyncio.create_task(engine_task(name),  name=f"engine_{name}"))
        tasks.append(asyncio.create_task(signal_task(name),  name=f"signal_{name}"))

    if any_paper:
        tasks.append(asyncio.create_task(executor.run_poll(),       name="executor_poll"))
        tasks.append(asyncio.create_task(executor.session_keeper(), name="session_keeper"))
        tasks.append(asyncio.create_task(executor.close_resolver(), name="close_resolver"))

    # Agent IA : pousse ses signaux dans la meme signal_queue que Bv3
    # -> consommee par signal_task -> OrderExecutor (PAPER)
    if ai_agent is not None and ai_instrument in signal_queues:
        tasks.append(asyncio.create_task(
            ai_agent.run(signal_queues[ai_instrument]),
            name=f"ai_agent_{ai_instrument}",
        ))

    # PortfolioRunner : push signaux dans signal_queues par instrument
    # -> consomme par signal_task -> OrderExecutor
    if portfolio_runner is not None:
        tasks.append(asyncio.create_task(
            portfolio_runner.run(signal_queues),
            name="portfolio_runner",
        ))
        # Gestion ACTIVE des positions ouvertes (SL theorique / TP / trailing /
        # retournement) au prix live. Le filet IG (SL large) reste en backstop.
        if any_paper:
            tasks.append(asyncio.create_task(
                portfolio_runner.manage_positions(executor, feed),
                name="portfolio_manage",
            ))
            # Entree breakout au NIVEAU (prix live) + scalper 10s CAC40
            tasks.append(asyncio.create_task(
                portfolio_runner.monitor_breakouts(executor, feed, signal_queues),
                name="breakout_monitor",
            ))
            tasks.append(asyncio.create_task(
                portfolio_runner.scalper_loop(executor, feed, signal_queues),
                name="scalper_10s",
            ))
            # Copieur de signaux Telegram (TRADAMAX) -> XAUUSD demo
            try:
                from telegram_reader import run_telegram_copier
                TG_GROUP_ID = -1001723080041
                tasks.append(asyncio.create_task(
                    run_telegram_copier(executor, TG_GROUP_ID),
                    name="telegram_copier",
                ))
                logger.info("[TG] tache copieur Telegram lancee")
            except Exception as e:
                logger.error(f"[TG] impossible de lancer le copieur : {e}")
            # Coupe-circuit perte journaliere (-300 EUR -> kill switch)
            tasks.append(asyncio.create_task(
                daily_loss_guard(300.0), name="daily_loss_guard"))

    shutdown_task = asyncio.create_task(shutdown_event.wait(), name="shutdown")
    done, pending = await asyncio.wait(
        tasks + [shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # FIX : identifier quelle tâche s'est terminée et pourquoi
    for t in done:
        tname = t.get_name()
        if t is shutdown_task:
            logger.info("Session terminée par signal d'arrêt")
        elif t.cancelled():
            logger.warning(f"Tâche '{tname}' annulée de façon inattendue")
        elif t.exception():
            logger.error(
                f"Tâche '{tname}' terminée sur exception : {t.exception()}"
            )
        else:
            logger.info(f"Tâche '{tname}' terminée normalement")

    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    summary = trade_log.end_session()
    logger.info("=" * 60)
    logger.info("FIN DE SESSION MULTI-INSTRUMENTS")
    logger.info(f"  Signaux totaux : {summary.get('signals', 0)}")
    for name, engine in engines.items():
        st = engine.get_status()
        logger.info(
            f"  {name:8s} : {st['signals_emitted']} signaux | "
            f"bars={st['bar_count']}"
        )
    logger.info("=" * 60)

    if any_paper:
        await executor.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description="Bonaza Multi-Instruments — SETUP_B Bv3")
    p.add_argument("--dry", action="store_true", help="Forcer DRY_RUN (aucun ordre)")
    args = p.parse_args()
    setup_logger(config.logs.level, config.logs.path)
    logger.info(f"Demarrage Bonaza | {datetime.now(timezone.utc).isoformat()}")
    asyncio.run(run_all(force_dry=args.dry))


if __name__ == "__main__":
    main()
