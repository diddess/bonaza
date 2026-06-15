"""
s10_store.py - Stockage durable des bougies S10 (write-ahead + consolidation).
==============================================================================
Reutilise OHLCVCandle (vps_snapshot/src/data_feed.py).

Strategie (cf spec section 2.2) :
  - Ecriture APPEND-ONLY durable : un JSONL journalier par instrument
        data/s10/{name}_{YYYY-MM-DD}.jsonl
    ouvert line-buffered (chaque ligne flushee), donc survit a un crash.
  - consolidate_day(name, date) -> parquet
        data/s10/{name}_{date}.parquet
    colonnes : timestamp, open, high, low, close, volume, bid_close, ask_close, tick_count
  - Rotation a MINUIT UTC : le nom de fichier derive de la date UTC du timestamp
    de la bougie, donc une bougie >= 00:00 UTC va automatiquement dans le fichier du
    jour suivant (un handle par (name, date)).
  - Reprise apres crash : read_jsonl(name, date) relit le JSONL partiel intact.

Le JSONL est la source de verite write-ahead ; le parquet est un derive compact.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, TextIO


from data_feed import OHLCVCandle  # noqa: E402

PARQUET_COLUMNS = [
    "timestamp", "open", "high", "low", "close",
    "volume", "bid_close", "ask_close", "tick_count",
]


def _utc_date_str(ts: datetime) -> str:
    """Date UTC YYYY-MM-DD du timestamp (rotation minuit UTC).

    On convertit explicitement vers UTC : un ts tagge +02:00 a 01:30 (= 23:30 UTC
    la veille) doit etre classe dans le fichier du jour UTC precedent. Un ts naif
    est suppose deja UTC (l'agregateur emet toujours en UTC).
    """
    from datetime import timezone as _tz
    if ts.tzinfo is None:
        return ts.strftime("%Y-%m-%d")
    return ts.astimezone(_tz.utc).strftime("%Y-%m-%d")


class S10Store:
    """Magasin append-only JSONL par jour + consolidation parquet."""

    def __init__(self, base_dir: str = "data/s10",
                 fsync: bool = True) -> None:
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)
        # un handle ouvert par (name, date) pour la rotation minuit UTC
        self._handles: Dict[str, TextIO] = {}
        # derniere ts (isoformat) deja persistee par (name, date) : deduplication
        # idempotente a l'append (reprise apres crash -> pas de doublon).
        self._last_ts: Dict[str, Optional[str]] = {}
        # os.fsync() apres chaque ligne -> WAL durable a une coupure kernel/courant
        # (pas seulement a un crash processus). Desactivable pour les benchmarks.
        self._fsync = fsync

    # -- chemins --------------------------------------------------------------

    def jsonl_path(self, name: str, date: str) -> str:
        return os.path.join(self.base_dir, f"{name}_{date}.jsonl")

    def parquet_path(self, name: str, date: str) -> str:
        return os.path.join(self.base_dir, f"{name}_{date}.parquet")

    def _handle(self, name: str, date: str) -> TextIO:
        key = f"{name}_{date}"
        h = self._handles.get(key)
        if h is None or h.closed:
            path = self.jsonl_path(name, date)
            # CICATRISATION torn-write : si le fichier existe et ne se termine PAS
            # par '\n', un fragment de ligne a ete tronque par un crash en pleine
            # ecriture. On ajoute un '\n' AVANT de reprendre l'append : le fragment
            # reste isole sur sa propre ligne (saute proprement par read_jsonl) et
            # la prochaine barre valide n'est PAS concatenee a lui.
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, "rb") as fb:
                    fb.seek(-1, os.SEEK_END)
                    last = fb.read(1)
                if last != b"\n":
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write("\n")
                        fh.flush()
                        if self._fsync:
                            os.fsync(fh.fileno())
            # buffering=1 -> line-buffered (durable a chaque '\n')
            h = open(path, "a", buffering=1, encoding="utf-8")
            self._handles[key] = h
            # charge la derniere ts deja persistee (dedup reprise apres crash)
            if key not in self._last_ts:
                self._last_ts[key] = self._scan_last_ts(name, date)
        return h

    def _scan_last_ts(self, name: str, date: str) -> Optional[str]:
        """Retourne la ts (isoformat) de la derniere barre valide deja persistee."""
        last: Optional[str] = None
        for row in self.read_jsonl(name, date):
            ts = row.get("timestamp")
            if ts is not None:
                last = ts
        return last

    # -- ecriture -------------------------------------------------------------

    def append(self, name: str, candle: OHLCVCandle) -> bool:
        """Append-only write-ahead durable + idempotent.

        - Deduplication : si la ts de la bougie est <= a la derniere deja persistee
          pour ce (name, date), on N'ECRIT PAS (reprise apres crash idempotente :
          l'agregateur peut re-emettre une barre non acquittee avant le crash sans
          creer de doublon dans le JSONL ni dans le parquet).
        - Durabilite : write + flush + os.fsync() -> survit a une coupure kernel.

        Retourne True si la barre a ete ecrite, False si dedupliquee.
        """
        date = _utc_date_str(candle.timestamp)
        key = f"{name}_{date}"
        h = self._handle(name, date)  # initialise _last_ts[key] au besoin
        ts_iso = candle.timestamp.isoformat()
        last = self._last_ts.get(key)
        if last is not None and ts_iso <= last:
            # barre deja persistee (ou anterieure) : idempotent, on ignore.
            return False
        h.write(json.dumps(self._candle_to_row(candle), separators=(",", ":")) + "\n")
        # buffering=1 garantit le flush ligne par ligne ; fsync force la descente disque.
        h.flush()
        if self._fsync:
            os.fsync(h.fileno())
        self._last_ts[key] = ts_iso
        return True

    def flush_all(self) -> None:
        for h in self._handles.values():
            if not h.closed:
                h.flush()

    def close(self) -> None:
        for h in self._handles.values():
            try:
                if not h.closed:
                    h.flush()
                    h.close()
            except Exception:
                pass
        self._handles.clear()

    # -- lecture / reprise ----------------------------------------------------

    def read_jsonl(self, name: str, date: str) -> List[dict]:
        """Relit le JSONL partiel (reprise apres crash). Lignes corrompues ignorees."""
        path = self.jsonl_path(name, date)
        if not os.path.exists(path):
            return []
        rows: List[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # ligne tronquee par un crash en cours d'ecriture : on l'ignore
                    continue
        return rows

    def known_dates(self, name: str) -> List[str]:
        """Dates pour lesquelles un JSONL existe pour cet instrument."""
        prefix = f"{name}_"
        dates = []
        for fn in os.listdir(self.base_dir):
            if fn.startswith(prefix) and fn.endswith(".jsonl"):
                dates.append(fn[len(prefix):-len(".jsonl")])
        return sorted(dates)

    # -- consolidation --------------------------------------------------------

    def consolidate_day(self, name: str, date: str) -> Optional[str]:
        """
        Lit le JSONL du jour et ecrit un parquet consolide. Retourne le chemin
        parquet, ou None si rien a consolider.
        """
        rows = self.read_jsonl(name, date)
        if not rows:
            return None
        import pandas as pd
        df = pd.DataFrame(rows, columns=PARQUET_COLUMNS)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        # filet de securite : si un doublon de ts a malgre tout atterri dans le
        # JSONL (ancien fichier d'avant le fix), on garde la derniere occurrence.
        df = df.drop_duplicates(subset="timestamp", keep="last").reset_index(drop=True)
        for col in ("open", "high", "low", "close", "volume", "bid_close", "ask_close"):
            df[col] = df[col].astype("float64")
        df["tick_count"] = df["tick_count"].astype("int64")
        path = self.parquet_path(name, date)
        df.to_parquet(path, index=False)
        return path

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _candle_to_row(c: OHLCVCandle) -> dict:
        return {
            "timestamp": c.timestamp.isoformat(),
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "bid_close": c.bid_close,
            "ask_close": c.ask_close,
            "tick_count": c.tick_count,
        }
