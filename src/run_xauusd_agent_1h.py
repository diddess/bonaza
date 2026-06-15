"""
run_xauusd_agent_1h.py - Session XAU/USD agent IA suivant les plages marche
==============================================================================
Lance une session continue qui :
  1. Se connecte a IG Markets (DEMO uniquement, refuse si LIVE)
  2. Ne souscrit qu'a XAUUSD (pas DAX, pas CAC40)
  3. Bv3 DESACTIVE : seul l'agent IA Claude declenche des ordres
  4. **L'agent IA respecte automatiquement les plages d'ouverture XAUUSD**
     (Dim 22h UTC -> Ven 21h UTC, pause quotidienne 21-22h UTC)
     -> hors marche : skip silencieux, aucun appel Claude, aucun ordre
  5. Watchdog : arret apres N secondes (par defaut 7 jours = 1 semaine de marche)
  6. Rapport final (JSON + console) : nb signaux, decisions, P&L estime, erreurs

ATTENTION :
  Stoppe d'abord main.py si actif (sinon 2 sessions IG concurrentes -> 401).

Pre-requis dans .env :
  AI_AGENT_ENABLED=true
  ANTHROPIC_API_KEY=sk-ant-api03-...
  IG_ACCOUNT_TYPE=DEMO
  BONAZA_MODE=PAPER

Usage :
  .\\bonaza_shell.bat
  python src\\run_xauusd_agent_1h.py                  # 7 jours par defaut (1 cycle marche)
  python src\\run_xauusd_agent_1h.py --duration 3600  # 1h pour test
  python src\\run_xauusd_agent_1h.py --dry-claude     # mock Claude (debug)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from ai_trading_agent import build_ai_agent
from config import config, validate_config
from data_feed import IGDataFeed
from instruments import InstrumentConfig
from logger_setup import setup_logger
from loguru import logger
from order_executor import build_executor
from risk_manager import RiskConfig, RiskManager, KillSwitchReason
from strategy_engine import build_engine_for
from trade_logger import TradeLogger
from warmup_loader import warmup_from_parquet


CAPITAL = 1745.0
# Duree par defaut : 7 jours = couvre tout un cycle hebdo XAUUSD
# (dim 22h UTC -> ven 21h UTC). L'agent IA ne trade que pendant les plages
# d'ouverture grace au garde-fou MarketHours (skip silencieux hors marche).
DEFAULT_DURATION_SEC = 7 * 24 * 3600
REPORT_DIR = Path(__file__).parent.parent / "data" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# Instrument XAUUSD dedie : PAPER, session etendue 0-24h (l'IA decide elle-meme)
XAUUSD_INSTRUMENT = InstrumentConfig(
    name          = "XAUUSD",
    epic          = "CS.D.CFEGOLD.CFE.IP",
    scale         = "5MINUTE",
    tf            = "M5",
    mode          = "PAPER",
    session_start = 0,
    session_end   = 24,
)


# -----------------------------------------------------------------------
# SessionReporter inline (simple, pas de surstructure)
# -----------------------------------------------------------------------

class SessionReporter:
    def __init__(self, label: str) -> None:
        self.label        = label
        self.start_time   = datetime.now(tz=timezone.utc)
        self.end_time     = None
        self.candles_seen = 0
        self.signals_emitted = 0
        self.signals_blocked = 0
        self.orders_sent  = 0
        self.orders_filled= 0
        self.errors       = []
        self.last_decision = None

    def finalize(self, agent_status: dict, executor_status: dict, rm_metrics) -> dict:
        self.end_time = datetime.now(tz=timezone.utc)
        report = {
            "label":           self.label,
            "start_utc":       self.start_time.isoformat(),
            "end_utc":         self.end_time.isoformat(),
            "duration_sec":    round((self.end_time - self.start_time).total_seconds(), 1),
            "candles_seen":    self.candles_seen,
            "agent": {
                "calls_made":       agent_status.get("calls_made", 0),
                "signals_emitted":  agent_status.get("signals_emitted", 0),
                "signals_skipped":  agent_status.get("signals_skipped", 0),
                "trades_last_hour": agent_status.get("trades_last_hour", 0),
                "last_decision":    agent_status.get("last_decision"),
            },
            "executor": {
                "connected":         executor_status.get("connected"),
                "open_positions":    executor_status.get("open_positions", 0),
                "orders_sent":       self.orders_sent,
                "orders_filled":     self.orders_filled,
            },
            "risk_manager": rm_metrics.to_dict() if rm_metrics else None,
            "errors":          self.errors[-20:],
        }
        return report

    def write(self, report: dict) -> Path:
        ts = self.start_time.strftime("%Y%m%d_%H%M")
        path = REPORT_DIR / f"session_{self.label}_{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        return path

    def print_summary(self, report: dict) -> None:
        a = report["agent"]
        x = report["executor"]
        r = report.get("risk_manager") or {}
        bar = "=" * 60
        print(f"\n{bar}\n  RAPPORT FINAL - {report['label']}\n{bar}")
        print(f"  Duree            : {report['duration_sec']}s "
              f"({report['duration_sec']/60:.1f} min)")
        print(f"  Bougies observees: {report['candles_seen']}")
        print(f"\n  AGENT IA")
        print(f"    Appels Claude  : {a['calls_made']}")
        print(f"    Signaux emis   : {a['signals_emitted']}")
        print(f"    Signaux skip   : {a['signals_skipped']}")
        print(f"    Derniere deci. : {a['last_decision']}")
        print(f"    Trades 1h      : {a['trades_last_hour']}")
        print(f"\n  EXECUTOR")
        print(f"    Connecte       : {x['connected']}")
        print(f"    Positions ouv. : {x['open_positions']}")
        if r:
            print(f"\n  RISK MANAGER")
            print(f"    Equity         : {r.get('equity')} EUR")
            print(f"    Realized P&L   : {r.get('realized_pnl')} EUR")
            print(f"    Trades cloture : {r.get('closed_today')}")
            print(f"    Win/Loss       : {r.get('wins_today')}/{r.get('losses_today')}")
            print(f"    Win rate       : {r.get('win_rate')}%")
            print(f"    Daily DD       : {r.get('daily_dd_pct')}%")
            print(f"    Kill switch    : {r.get('kill_switch')}")
        if report["errors"]:
            print(f"\n  ERREURS RECENTES ({len(report['errors'])}) :")
            for e in report["errors"]:
                print(f"    - {e}")
        print(bar)


# -----------------------------------------------------------------------
# Orchestrateur de session
# -----------------------------------------------------------------------

async def run_session(duration_sec: int, dry_claude: bool = False) -> int:
    setup_logger(config.logs.level, config.logs.path)
    label = "xauusd_ai_1h"

    bar = "=" * 60
    logger.info(bar)
    logger.info(f"BONAZA - SESSION {label.upper()} - {duration_sec}s")
    logger.info(f"  Compte         : {config.ig.account_id} ({config.ig.account_type})")
    logger.info(f"  Mode           : {config.trading.mode}")
    logger.info(f"  Agent IA       : {config.agent.model} every {config.agent.interval_sec}s")
    logger.info(bar)

    # --- Pre-checks ---
    if config.ig.account_type.upper() != "DEMO":
        logger.error(f"REFUSE : compte non-DEMO ({config.ig.account_type}). Abandon.")
        return 2
    if not config.agent.is_ready():
        logger.error("Agent IA non-ready. Verifie AI_AGENT_ENABLED=true et ANTHROPIC_API_KEY dans .env")
        return 3
    if not validate_config():
        logger.error("Configuration invalide.")
        return 4

    reporter = SessionReporter(label=label)

    # --- Setup composants ---
    rm = RiskManager(
        config  = RiskConfig.from_trading_config(config.trading),
        capital = CAPITAL,
    )
    if config.trading.kill_switch:
        rm.activate_kill_switch(KillSwitchReason.EXTERNAL)

    engine = build_engine_for(XAUUSD_INSTRUMENT, config, capital=CAPITAL)[0]
    await warmup_from_parquet(engine, instrument="XAUUSD", tf="M5")
    logger.info(f"Warmup termine : buffer Bv3 = {len(engine._buffer)} bougies "
                f"(re-utilise pour seeder l'agent IA)")

    trade_log = TradeLogger()
    trade_log.start_session(mode="AI_AGENT_1H")
    executor  = build_executor(config, {"XAUUSD": rm}, trade_logger=trade_log)

    # Agent IA (force enabled meme si .env=false ? non, on respecte .env)
    try:
        agent = build_ai_agent(config, rm, instrument="XAUUSD")
    except RuntimeError as e:
        logger.error(f"Echec build agent: {e}")
        return 5
    if agent is None:
        logger.error("Agent IA non instancie (config invalide).")
        return 5
    # Seed l'agent avec les bougies historiques deja en buffer Bv3
    for c in list(engine._buffer)[-300:]:
        agent.add_candle(c)
    logger.info(f"Agent IA warmup buffer = {len(agent._buffer)} bougies")

    # --- Feed IG + executor connect ---
    feed = IGDataFeed(config=config, subscriptions=[
        {"epic": XAUUSD_INSTRUMENT.epic, "scale": XAUUSD_INSTRUMENT.scale},
    ])
    await feed.start()
    if not await executor.connect(ig_service=feed._ig_service):
        logger.error("Executor connect echoue. Abandon.")
        await feed.stop()
        return 6
    logger.info("Executor pret | session partagee")

    # --- Queues & tasks ---
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=20)

    async def dispatcher():
        try:
            async for candle in feed.iter_candles():
                if candle.epic != XAUUSD_INSTRUMENT.epic:
                    continue
                reporter.candles_seen += 1
                agent.add_candle(candle)
        except asyncio.CancelledError:
            pass

    async def signal_consumer():
        try:
            while True:
                setup = await signal_queue.get()
                if setup is None:
                    break
                sig_id = trade_log.log_signal(
                    setup=setup, mode="PAPER", instrument="XAUUSD", tf="M5",
                )
                logger.info(
                    f"[XAUUSD] PAPER AI #{sig_id} {setup.direction.value} "
                    f"E={setup.entry} SL={setup.stop_loss} TP={setup.take_profit} "
                    f"size={setup.size}"
                )
                reporter.orders_sent += 1
                try:
                    await executor._handle_signal(setup, signal_id=sig_id)
                    reporter.orders_filled += 1
                except Exception as e:
                    err = f"executor handle: {e}"
                    reporter.errors.append(err)
                    logger.error(err)
        except asyncio.CancelledError:
            pass

    async def watchdog():
        logger.info(f"Watchdog actif : arret dans {duration_sec}s")
        await asyncio.sleep(duration_sec)
        logger.info("Watchdog : duree atteinte, arret propre demande")

    shutdown_event = asyncio.Event()
    def _stop(sig, frame):
        logger.info(f"Signal {sig} recu - arret")
        shutdown_event.set()
    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    tasks = [
        asyncio.create_task(dispatcher(),           name="dispatcher"),
        asyncio.create_task(signal_consumer(),      name="signal_consumer"),
        asyncio.create_task(agent.run(signal_queue),name="ai_agent"),
        asyncio.create_task(executor.run_poll(),    name="executor_poll"),
        asyncio.create_task(executor.session_keeper(), name="session_keeper"),
    ]
    watchdog_task = asyncio.create_task(watchdog(), name="watchdog")
    shutdown_task = asyncio.create_task(shutdown_event.wait(), name="shutdown")

    # Attend la 1ere terminaison (watchdog, signal, ou erreur)
    done, pending = await asyncio.wait(
        tasks + [watchdog_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in done:
        if t.cancelled() or t is shutdown_task or t is watchdog_task:
            continue
        if t.exception():
            reporter.errors.append(f"task {t.get_name()}: {t.exception()}")

    # Cleanup
    logger.info("Arret en cours...")
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await signal_queue.put(None)
    await feed.stop()
    await executor.disconnect()
    trade_log.end_session()

    # Rapport
    report = reporter.finalize(
        agent_status    = agent.get_status(),
        executor_status = executor.get_status(),
        rm_metrics      = rm.get_metrics(),
    )
    path = reporter.write(report)
    reporter.print_summary(report)
    logger.info(f"Rapport JSON sauvegarde : {path}")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Session continue XAU/USD agent IA (suivi plages marche)",
    )
    p.add_argument("--duration", type=int, default=DEFAULT_DURATION_SEC,
                   help=f"Duree max en secondes (defaut {DEFAULT_DURATION_SEC}s "
                        f"= {DEFAULT_DURATION_SEC // 3600}h = 7 jours). "
                        f"L'agent skip automatiquement quand le marche est ferme.")
    p.add_argument("--dry-claude", action="store_true",
                   help="(non implemente) mock Claude pour debug local")
    args = p.parse_args()
    sys.exit(asyncio.run(run_session(args.duration, args.dry_claude)))


if __name__ == "__main__":
    main()
