"""
trade_logger.py - M06 : Journalisation SQLite des trades Bonaza
===============================================================
Corrections post-review :
  - session_summary() filtre sur ts_start de la session courante
    (plus de mélange avec les sessions passées)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger as log
from strategy_spec import TradeSetup

DB_PATH = Path(__file__).parent.parent / "data" / "bonaza.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    mode        TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    entry       REAL    NOT NULL,
    stop_loss   REAL    NOT NULL,
    take_profit REAL    NOT NULL,
    risk_pts    REAL    NOT NULL,
    reward_pts  REAL    NOT NULL,
    rr_ratio    REAL    NOT NULL,
    setup_name  TEXT    NOT NULL,
    reason      TEXT,
    hour_utc    INTEGER,
    day_of_week TEXT,
    instrument  TEXT    DEFAULT 'XAUUSD',
    tf          TEXT    DEFAULT 'M5'
);
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT    UNIQUE NOT NULL,
    signal_id   INTEGER REFERENCES signals(id),
    ts_open     TEXT    NOT NULL,
    ts_close    TEXT,
    direction   TEXT    NOT NULL,
    size        REAL    NOT NULL,
    entry_price REAL    NOT NULL,
    sl_price    REAL    NOT NULL,
    tp_price    REAL    NOT NULL,
    exit_price  REAL,
    exit_reason TEXT,
    pnl_eur     REAL,
    duration_min REAL,
    status      TEXT    DEFAULT 'OPEN'
);
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT    NOT NULL,
    ts_start     TEXT    NOT NULL,
    ts_end       TEXT,
    mode         TEXT    NOT NULL,
    signals_total INTEGER DEFAULT 0,
    trades_total  INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    pnl_eur       REAL    DEFAULT 0.0,
    max_dd_eur    REAL    DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_signals_ts        ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_direction ON signals(direction);
CREATE INDEX IF NOT EXISTS idx_trades_status     ON trades(status);
CREATE INDEX IF NOT EXISTS idx_sessions_date     ON sessions(date);
"""


class TradeLogger:

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()
        self._session_id:   Optional[int] = None
        self._session_mode: str = "DRY_RUN"
        log.info(f"TradeLogger initialise | DB: {self.db_path}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # --- Session ---

    def start_session(self, mode: str = "DRY_RUN") -> int:
        self._session_mode = mode
        ts = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO sessions(date, ts_start, mode) VALUES (?,?,?)",
                (ts[:10], ts, mode)
            )
            self._session_id = cur.lastrowid
        log.info(f"Session demarree | id={self._session_id} mode={mode}")
        return self._session_id

    def end_session(self) -> dict:
        if not self._session_id:
            return {}
        summary = self.session_summary()
        with self._conn() as conn:
            conn.execute(
                """UPDATE sessions SET ts_end=?, signals_total=?,
                   trades_total=?, wins=?, losses=?, pnl_eur=?
                   WHERE id=?""",
                (
                    _now(),
                    summary.get("signals", 0),
                    summary.get("trades", 0),
                    summary.get("wins", 0),
                    summary.get("losses", 0),
                    summary.get("pnl_eur", 0.0),
                    self._session_id,
                )
            )
        log.info(f"Session fermee | {summary}")
        return summary

    # --- Signaux ---

    def log_signal(
        self,
        setup:      TradeSetup,
        mode:       str = "DRY_RUN",
        instrument: str = "XAUUSD",
        tf:         str = "M5",
    ) -> int:
        ts = _now()
        dt = datetime.fromisoformat(ts)
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO signals
                   (ts,mode,direction,entry,stop_loss,take_profit,
                    risk_pts,reward_pts,rr_ratio,setup_name,reason,
                    hour_utc,day_of_week,instrument,tf)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, mode, setup.direction.value,
                    setup.entry, setup.stop_loss, setup.take_profit,
                    setup.risk_pts, setup.reward_pts, setup.rr_ratio,
                    setup.setup_name, setup.reason,
                    dt.hour, dt.strftime("%A"), instrument, tf,
                )
            )
            sid = cur.lastrowid
        log.debug(f"Signal log | id={sid} {mode} {setup.direction.value}")
        return sid

    # --- Trades ---

    def log_fill(
        self,
        position_id: str,
        direction:   str,
        entry:       float,
        sl:          float,
        tp:          float,
        size:        float,
        signal_id:   Optional[int] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO trades
                   (position_id,signal_id,ts_open,direction,
                    size,entry_price,sl_price,tp_price,status)
                   VALUES (?,?,?,?,?,?,?,?,'OPEN')""",
                (position_id, signal_id, _now(), direction,
                 size, entry, sl, tp)
            )
        log.debug(f"Fill log | pos={position_id} {direction} E={entry:.2f}")

    def log_close(
        self,
        position_id: str,
        exit_price:  float,
        reason:      str,
        pnl_eur:     Optional[float] = None,
    ) -> None:
        ts_close = _now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ts_open FROM trades WHERE position_id=?",
                (position_id,)
            ).fetchone()
            if row:
                ts_open = datetime.fromisoformat(row["ts_open"])
                ts_c    = datetime.fromisoformat(ts_close)
                dur_min = (ts_c - ts_open).total_seconds() / 60
                conn.execute(
                    """UPDATE trades SET ts_close=?, exit_price=?,
                       exit_reason=?, pnl_eur=?, duration_min=?, status='CLOSED'
                       WHERE position_id=?""",
                    (ts_close, exit_price, reason, pnl_eur, dur_min, position_id)
                )
        log.debug(f"Close log | pos={position_id} exit={exit_price:.2f}")

    # --- Statistiques ---

    def session_summary(self) -> dict:
        """
        FIX : résumé de la session COURANTE uniquement.
        Filtre par ts_start pour ne pas mélanger les sessions passées.
        """
        with self._conn() as conn:
            # Heure de début de la session courante
            ts_start = "1970-01-01T00:00:00"
            if self._session_id:
                row = conn.execute(
                    "SELECT ts_start FROM sessions WHERE id=?",
                    (self._session_id,)
                ).fetchone()
                if row:
                    ts_start = row["ts_start"]

            sig = conn.execute(
                """SELECT COUNT(*) as n,
                          SUM(CASE WHEN direction='LONG'  THEN 1 ELSE 0 END) as longs,
                          SUM(CASE WHEN direction='SHORT' THEN 1 ELSE 0 END) as shorts
                   FROM signals WHERE ts >= ?""",
                (ts_start,)
            ).fetchone()

            tr = conn.execute(
                """SELECT COUNT(*) as n,
                          SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END) as wins,
                          SUM(CASE WHEN pnl_eur < 0 THEN 1 ELSE 0 END) as losses,
                          SUM(pnl_eur) as total_pnl
                   FROM trades
                   WHERE status='CLOSED' AND ts_open >= ?""",
                (ts_start,)
            ).fetchone()

        return {
            "signals":  sig["n"]      or 0,
            "longs":    sig["longs"]  or 0,
            "shorts":   sig["shorts"] or 0,
            "trades":   tr["n"]       or 0,
            "wins":     tr["wins"]    or 0,
            "losses":   tr["losses"]  or 0,
            "pnl_eur":  round(tr["total_pnl"] or 0.0, 2),
            "win_rate": round((tr["wins"] or 0) / max(tr["n"] or 1, 1) * 100, 1),
        }

    def recent_signals(self, n: int = 10) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, mode, direction, entry, stop_loss, take_profit, "
                "rr_ratio, reason FROM signals ORDER BY id DESC LIMIT ?",
                (n,)
            ).fetchall()
        return [dict(r) for r in rows]

    def print_summary(self) -> None:
        s = self.session_summary()
        r = self.recent_signals(5)
        print("\n=== BONAZA TRADE LOGGER ===")
        print(f"  Signaux  : {s['signals']} (L:{s['longs']} S:{s['shorts']})")
        print(f"  Trades   : {s['trades']} | W:{s['wins']} L:{s['losses']}")
        print(f"  Win rate : {s['win_rate']}%")
        print(f"  P&L      : {s['pnl_eur']:+.2f} EUR")
        if r:
            print(f"\n  Derniers signaux :")
            for sig in r:
                print(f"    {sig['ts'][11:19]} {sig['mode']:8s} "
                      f"{sig['direction']:5s} E={sig['entry']:.2f} "
                      f"R:R={sig['rr_ratio']:.1f}")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


if __name__ == "__main__":
    tl = TradeLogger()
    tl.print_summary()
    print(f"\nDB : {DB_PATH}")
    print(f"Taille : {DB_PATH.stat().st_size if DB_PATH.exists() else 0} bytes")
