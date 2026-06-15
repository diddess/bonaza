"""daily_monitor.py - Surveillance automatique Bonaza Portfolio v2.
================================================================================

OBJECTIF : verifier 3x/jour de trading que le systeme tourne, que les strategies
emettent des signaux conformes, et que la courbe de P&L progresse sans regresser.

CIBLE PNL :
  - +5 a +15 EUR par jour positif
  - Amelioration >= +1% jour/jour (compose tant que possible)
  - PRIORITE ABSOLUE : pas de regression (continuite)

LOGIQUE :
  1. Collecte stats du jour depuis SQLite + status.json + trade_events.jsonl
  2. Compare avec le jour precedent / le streak / la cible
  3. Verdict deterministe : PROGRESSION / STABLE / WARNING / REGRESSION / ALERT
  4. Push Telegram avec rapport structure
  5. Stocke un snapshot dans data/monitoring/

ZERO appel API IA. Pur Python + SQLite + requests Telegram.

USAGE :
  python src/daily_monitor.py           # run immediat
  Via cron (3x/jour) sur le VPS :
    0 12 * * 1-5 cd /opt/bonaza && docker exec bonaza_main python /app/src/daily_monitor.py
    0 17 * * 1-5 ...
    30 21 * * 1-5 ...
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import requests
from loguru import logger

# Conditionnel pour pouvoir s'executer dans un test local
try:
    sys.path.insert(0, os.path.dirname(__file__))
    from config import config
except Exception as exc:
    print(f"Erreur import config : {exc}")
    sys.exit(1)


# -----------------------------------------------------------------------
# Cibles & seuils
# -----------------------------------------------------------------------
TARGET_PNL_MIN_PER_DAY = 5.0    # euros, minimum cible jour positif
TARGET_PNL_MAX_PER_DAY = 15.0   # euros, max raisonnable jour positif
TARGET_GROWTH_PCT      = 1.0    # +1% du PNL par rapport a la veille
LOSS_TOLERATED_EUR     = 5.0    # jour perdant tolere en absolu
WARN_DD_PCT            = 1.0    # alerte si DD jour > 1%
ALERT_DD_PCT           = 2.0    # critical si DD jour > 2%


# -----------------------------------------------------------------------
# Sources
# -----------------------------------------------------------------------
DB_PATH      = Path(os.getenv("BONAZA_DB_PATH", "/app/data/bonaza.db"))
DATA_DIR     = DB_PATH.parent
STATUS_FILE  = DATA_DIR / "status.json"
TRADE_EVTS   = DATA_DIR / "trade_events.jsonl"
MONIT_DIR    = DATA_DIR / "monitoring"
MONIT_DIR.mkdir(parents=True, exist_ok=True)

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")


# -----------------------------------------------------------------------
# Collecte de donnees
# -----------------------------------------------------------------------

def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def trades_for_day(d: date) -> List[Dict]:
    """Trades fermes ce jour d (ts_close)."""
    if not DB_PATH.exists():
        return []
    with _con() as con:
        rows = con.execute(
            "SELECT * FROM trades "
            "WHERE date(ts_close) = ? "
            "ORDER BY ts_close",
            (str(d),)
        ).fetchall()
        return [dict(r) for r in rows]


def signals_for_day(d: date) -> List[Dict]:
    """Signaux emis ce jour d."""
    if not DB_PATH.exists():
        return []
    with _con() as con:
        rows = con.execute(
            "SELECT * FROM signals WHERE date(ts) = ? ORDER BY ts",
            (str(d),)
        ).fetchall()
        return [dict(r) for r in rows]


def trade_summary(trades: List[Dict]) -> Dict:
    """Aggreges du jour : pnl, n trades, wr, par strategy."""
    n = len(trades)
    pnl = sum(t["pnl_eur"] or 0 for t in trades)
    wins = sum(1 for t in trades if (t["pnl_eur"] or 0) > 0)
    losses = sum(1 for t in trades if (t["pnl_eur"] or 0) < 0)
    wr = (wins / n * 100) if n else 0.0
    # Par instrument
    by_inst: Dict[str, Dict] = {}
    for t in trades:
        # Inferer instrument depuis position_id - pas dispo direct, on prend signal_id
        inst = "?"   # fallback, on remet le vrai instrument plus tard si signal_id matche
        sigid = t.get("signal_id")
        # Pour eviter une jointure couteuse, on lit instrument depuis signals.instrument
        # via un dict externe (pas la peine ici, on garde par direction)
        by_inst.setdefault(inst, {"n": 0, "pnl": 0.0, "wins": 0})
        by_inst[inst]["n"] += 1
        by_inst[inst]["pnl"] += (t["pnl_eur"] or 0)
        if (t["pnl_eur"] or 0) > 0:
            by_inst[inst]["wins"] += 1
    return {
        "n_trades": n, "pnl_eur": round(pnl, 2),
        "wins": wins, "losses": losses, "win_rate_pct": round(wr, 1),
        "by_instrument": by_inst,
    }


def signal_summary(signals: List[Dict]) -> Dict:
    """Distribution des signaux du jour par strategie & instrument."""
    n = len(signals)
    by_strat = {}
    by_inst = {}
    for s in signals:
        setup = s.get("setup_name", "?")
        inst = s.get("instrument", "?")
        by_strat.setdefault(setup, 0)
        by_inst.setdefault(inst, 0)
        by_strat[setup] += 1
        by_inst[inst] += 1
    return {"n_signals": n, "by_strategy": by_strat, "by_instrument": by_inst}


def read_status() -> Dict:
    """Lit le snapshot status.json (ecrit toutes les 30s par main.py)."""
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def count_trade_events_today(d: date) -> Dict:
    """Compte les events TRADE pushes par streaming aujourd'hui (CONFIRMS / OPU / WOU)."""
    if not TRADE_EVTS.exists():
        return {"confirms": 0, "opu": 0, "wou": 0, "total": 0}
    confirms = opu = wou = 0
    day_str = str(d)
    try:
        with TRADE_EVTS.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or day_str not in line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                ts = evt.get("ts", "")
                if not ts.startswith(day_str):
                    continue
                if evt.get("confirms"): confirms += 1
                if evt.get("opu"):      opu += 1
                if evt.get("wou"):      wou += 1
    except Exception:
        pass
    return {"confirms": confirms, "opu": opu, "wou": wou,
            "total": confirms + opu + wou}


# -----------------------------------------------------------------------
# Comparaison avec veille
# -----------------------------------------------------------------------

def previous_trading_day(today: date) -> date:
    """Prochain jour de trading precedent (sauter weekend)."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:   # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    return d


def compute_verdict(today_pnl: float, prev_pnl: float,
                    today_n: int, prev_n: int,
                    dd_today_pct: float) -> Tuple[str, List[str]]:
    """Verdict deterministe + raisons.
    Niveaux : OK_PROGRESSION / OK_STABLE / WARN_FAIBLE / WARN_REGRESSION / ALERT
    """
    reasons: List[str] = []

    # ALERT : DD critique
    if dd_today_pct >= ALERT_DD_PCT:
        return "ALERT", [f"DD jour {dd_today_pct:.2f}% >= seuil critique {ALERT_DD_PCT}%"]

    # ALERT : perte jour > tolere
    if today_pnl < -LOSS_TOLERATED_EUR * 2:
        return "ALERT", [f"PNL jour {today_pnl:+.2f} EUR < seuil critique -{LOSS_TOLERATED_EUR*2:.0f} EUR"]

    # WARN : regression franche (pnl jour bcp moins bon que veille positive)
    if prev_pnl > 0 and today_pnl < prev_pnl * 0.5:
        reasons.append(f"PNL jour {today_pnl:+.2f} EUR < 50% du PNL veille ({prev_pnl:+.2f} EUR)")

    if dd_today_pct >= WARN_DD_PCT:
        reasons.append(f"DD jour {dd_today_pct:.2f}% >= seuil warning {WARN_DD_PCT}%")

    if reasons:
        return "WARN_REGRESSION", reasons

    # Cas pnl trop bas vs cible mais positif
    if 0 <= today_pnl < TARGET_PNL_MIN_PER_DAY * 0.3:
        return "WARN_FAIBLE", [
            f"PNL jour {today_pnl:+.2f} EUR << cible minimum +{TARGET_PNL_MIN_PER_DAY:.0f} EUR"
        ]

    # OK PROGRESSION : pnl meilleur que veille (au moins +1%)
    if prev_pnl > 0 and today_pnl >= prev_pnl * (1 + TARGET_GROWTH_PCT / 100):
        return "OK_PROGRESSION", [
            f"PNL jour {today_pnl:+.2f} EUR >= veille ({prev_pnl:+.2f} EUR) + {TARGET_GROWTH_PCT:.1f}%"
        ]

    # OK STABLE : pnl positif comparable a veille
    if today_pnl > 0 and (prev_pnl <= 0 or today_pnl >= prev_pnl * 0.9):
        return "OK_STABLE", [
            f"PNL jour {today_pnl:+.2f} EUR conforme (veille {prev_pnl:+.2f} EUR)"
        ]

    # Cas fall-back
    return "WARN_FAIBLE", [f"PNL jour {today_pnl:+.2f} EUR vs veille {prev_pnl:+.2f} EUR"]


# -----------------------------------------------------------------------
# Rapport Telegram
# -----------------------------------------------------------------------

VERDICT_HEADER = {
    "OK_PROGRESSION":  "PROGRESSION CONFIRMEE",
    "OK_STABLE":       "STABLE / CONFORME",
    "WARN_FAIBLE":     "ATTENTION : PNL FAIBLE",
    "WARN_REGRESSION": "ATTENTION : REGRESSION DETECTEE",
    "ALERT":           "ALERTE CRITIQUE",
}


def build_report(verdict: str, reasons: List[str],
                  today_summary: Dict, prev_summary: Dict,
                  sig_summary: Dict, status: Dict,
                  evt_count: Dict, day: date) -> str:
    """Construit le texte Telegram (plain text, pas markdown pour eviter parse error)."""
    lines = []
    lines.append(f"=== Bonaza monitor - {day.isoformat()} ===")
    lines.append(f"=> {VERDICT_HEADER.get(verdict, verdict)}")
    lines.append("")

    # Raisons
    if reasons:
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")

    # PNL jour vs veille
    lines.append(f"PNL aujourd'hui  : {today_summary['pnl_eur']:+7.2f} EUR ({today_summary['n_trades']} trades)")
    lines.append(f"PNL veille       : {prev_summary['pnl_eur']:+7.2f} EUR ({prev_summary['n_trades']} trades)")
    if prev_summary['pnl_eur'] != 0:
        growth = (today_summary['pnl_eur'] - prev_summary['pnl_eur']) / abs(prev_summary['pnl_eur']) * 100
        lines.append(f"Evolution        : {growth:+.1f}%")
    lines.append(f"Win rate jour    : {today_summary['win_rate_pct']:.1f}%  ({today_summary['wins']}W / {today_summary['losses']}L)")
    lines.append("")

    # Signaux par strategie
    lines.append(f"Signaux emis     : {sig_summary['n_signals']}")
    for strat, count in sorted(sig_summary['by_strategy'].items()):
        lines.append(f"  - {strat:24s} : {count}")
    lines.append("")

    # Activite streaming
    lines.append(f"Stream IG TRADE  : {evt_count['total']} events")
    lines.append(f"  CONFIRMS={evt_count['confirms']}  OPU={evt_count['opu']}  WOU={evt_count['wou']}")

    # Etat agent IA + Bv3 (doivent etre off)
    ai_agent = status.get("ai_agent")
    if ai_agent and ai_agent.get("calls_made"):
        lines.append(f"!! Agent IA actif (calls={ai_agent.get('calls_made')}) - devrait etre off !")

    # Etat portfolio runner
    pf = status.get("portfolio")
    if pf:
        lines.append("")
        lines.append("Portfolio runner :")
        lines.append(f"  signaux_total   = {pf.get('signals_total', 0)}")
        lines.append(f"  signaux_emis    = {pf.get('signals_emitted', 0)}")
        lines.append(f"  rejetes RM      = {pf.get('signals_blocked_rm', 0)}")

    # MARKET states
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        logger.warning("TG_TOKEN/TG_CHAT absent, skip envoi")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text[:4000]},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as exc:
        logger.error(f"telegram fail : {exc}")
        return False


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    now = datetime.now(tz=timezone.utc)
    today = now.date()
    prev = previous_trading_day(today)

    today_trades = trades_for_day(today)
    prev_trades  = trades_for_day(prev)
    today_signals = signals_for_day(today)

    today_sum = trade_summary(today_trades)
    prev_sum  = trade_summary(prev_trades)
    sig_sum   = signal_summary(today_signals)
    status    = read_status()
    evt_cnt   = count_trade_events_today(today)

    # DD du jour : pnl_jour / capital - assume capital ~ status.equity ou .env CAPITAL
    cap = float(os.getenv("BONAZA_CAPITAL", "485.79"))
    rm_xau = (status.get("rm_metrics") or {}).get("XAUUSD") or {}
    if rm_xau.get("equity"):
        cap = float(rm_xau["equity"])
    dd_today = abs(today_sum["pnl_eur"]) / cap * 100 if today_sum["pnl_eur"] < 0 else 0.0

    verdict, reasons = compute_verdict(
        today_sum["pnl_eur"], prev_sum["pnl_eur"],
        today_sum["n_trades"], prev_sum["n_trades"],
        dd_today,
    )

    report = build_report(verdict, reasons, today_sum, prev_sum,
                          sig_sum, status, evt_cnt, today)

    # Header avec heure UTC
    header = f"[{now.strftime('%H:%M UTC')}] "
    full = header + report
    print(full)
    send_telegram(full)

    # Snapshot JSON archive
    snap = {
        "ts": now.isoformat(),
        "day": today.isoformat(),
        "verdict": verdict, "reasons": reasons,
        "today": today_sum, "previous": prev_sum,
        "signals": sig_sum, "trade_events": evt_cnt,
        "dd_today_pct": round(dd_today, 3),
        "capital_used": cap,
    }
    out = MONIT_DIR / f"snapshot_{now.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
