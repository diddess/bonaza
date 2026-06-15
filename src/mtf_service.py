"""
mtf_service.py - Construction multi-timeframe (M1..H4) a partir du flux S10.
===========================================================================
Remplace la partie "TF rafraichies toutes les 10s" de s10_indicator_service.py.

Conception :
  - UNE seule source : les bougies S10 finalisees (cf s10_aggregator / s10_runner).
  - Pour chaque instrument, un _TFBuilder par timeframe (M1, M5, M15, M30, H1, H4)
    agrege les S10 en bougies TF. Le bucket est aligne sur l'epoch UTC absolu
    (floor(epoch / tf_seconds) * tf_seconds), donc chaque TF est independante et
    coherente avec le live (pas de cascade : chaque TF lit directement le S10).
  - A LA CLOTURE d'une bougie TF (frontiere de bucket franchie) UNIQUEMENT :
        a. la bougie OHLCV est persistee   -> data/s10/{name}_{TF}_{date}.jsonl
        b. si TF >= M5 : l'IndicatorSet est calcule sur les bougies CLOTUREES
           (meme compute_from_arrays que le live) -> analysis_{name}_{TF}_{date}.jsonl
     M1 = bougie seule (pas d'indicateurs).
  - ZERO look-ahead : on ne calcule jamais sur une bougie en cours de formation ;
    une bougie TF n'est emise que lorsqu'un S10 d'un bucket posterieur arrive
    (le flush S10 amont garantit la fermeture des buckets en marche lente).

NE cree AUCUNE session IG, NE prend AUCUNE decision de trading (observabilite).
Execute dans l'executor mono-thread du S10Runner -> pas d'appel bloquant dans
l'event loop de trading, etat mutable serialise (thread-safe sans verrou).
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from data_feed import OHLCVCandle
from indicators import IndicatorSet, compute_from_arrays, BUFFER_DEFAULT
from market_structure import StructureAnalyzer, MarketState

# Timeframes construites (secondes). Bucket aligne sur l'epoch UTC absolu.
MTF_TF_SECONDS: Dict[str, int] = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400,
}
# TF qui recoivent les indicateurs TA-Lib ("a partir du M5"). M1 = bougie seule.
MTF_INDICATOR_TFS = frozenset({"M5", "M15", "M30", "H1", "H4"})
# Nombre de bougies TF *cloturees* gardees pour le calcul (>= EMA50/MACD stables).
MTF_BUFFER = BUFFER_DEFAULT


def _tf_bucket(epoch_s: float, tf_seconds: int) -> int:
    return int(epoch_s // tf_seconds) * tf_seconds


class _TFBuilder:
    """
    Agregateur INCREMENTAL S10 -> une timeframe. add() retourne la bougie TF qui
    vient de se CLOTURER (ou None). On ne deque QUE des bougies completes ->
    jamais d'OHLC partiel. On conserve jusqu'a max_complete bougies cloturees.
    """

    def __init__(self, tf_seconds: int, epic: str, scale: str,
                 max_complete: int = MTF_BUFFER) -> None:
        self.tf_seconds = tf_seconds
        self.epic = epic
        self.scale = scale
        self.max_complete = max_complete
        self._complete: List[OHLCVCandle] = []
        self._cur: Optional[OHLCVCandle] = None
        self._cur_bucket: Optional[int] = None

    def add(self, c: OHLCVCandle) -> Optional[OHLCVCandle]:
        b = _tf_bucket(c.timestamp.timestamp(), self.tf_seconds)
        if self._cur is None:
            self._open(b, c)
            return None
        if b == self._cur_bucket:
            self._merge(c)
            return None
        if b > self._cur_bucket:
            # frontiere TF franchie : la bougie courante est CLOTUREE.
            self._cur.is_complete = True
            closed = self._cur
            self._complete.append(closed)
            if len(self._complete) > self.max_complete:
                self._complete = self._complete[-self.max_complete:]
            self._open(b, c)
            return closed
        # b < cur_bucket : S10 en retard -> ignore (anti-look-ahead amont).
        return None

    def complete_bars(self) -> List[OHLCVCandle]:
        """Les dernieres bougies TF CLOTUREES (jamais la forming)."""
        return self._complete

    def _open(self, bucket: int, c: OHLCVCandle) -> None:
        self._cur_bucket = bucket
        self._cur = OHLCVCandle(
            epic=self.epic, scale=self.scale,
            timestamp=datetime.fromtimestamp(bucket, tz=timezone.utc),
            open=c.open, high=c.high, low=c.low, close=c.close,
            volume=c.volume, tick_count=c.tick_count,
            bid_close=c.bid_close, ask_close=c.ask_close,
            is_complete=False,
        )

    def _merge(self, c: OHLCVCandle) -> None:
        cur = self._cur
        if c.high > cur.high:
            cur.high = c.high
        if c.low < cur.low:
            cur.low = c.low
        cur.close = c.close
        cur.volume += c.volume
        cur.tick_count += c.tick_count
        cur.bid_close = c.bid_close
        cur.ask_close = c.ask_close


def _iset_to_dict(iset: IndicatorSet) -> dict:
    """Snapshot complet et numerique (NaN -> None) d'un IndicatorSet."""
    flds = [
        "close", "high", "low", "volume", "ema_fast", "ema_slow", "rsi",
        "macd_line", "macd_signal", "macd_hist", "atr",
        "bb_upper", "bb_mid", "bb_lower", "bb_percent_b", "bb_bandwidth",
        "stoch_k", "stoch_d", "volume_ma",
    ]
    d: dict = {"bar_count": iset.bar_count}
    for f in flds:
        v = getattr(iset, f)
        d[f] = None if (isinstance(v, float) and math.isnan(v)) else v
    return d


class MTFService:
    """
    Construit les bougies M1..H4 d'un instrument depuis le flux S10 et persiste,
    A LA CLOTURE de chaque TF : la bougie OHLCV (toutes TF) + l'IndicatorSet
    (TF >= M5). Reutilise un S10Store pour les bougies (filenames {name}_{TF}_...).

    on_s10_bar(candle) -> liste des cles TF cloturees par cette barre S10.
    """

    def __init__(
        self,
        epic: str,
        name: str,
        store=None,
        tf_seconds: Optional[Dict[str, int]] = None,
        indicator_tfs=MTF_INDICATOR_TFS,
        tf_buffer: int = MTF_BUFFER,
        market_state: Optional[MarketState] = None,
    ) -> None:
        self.epic = epic
        self.name = name
        self.store = store
        self.tf_seconds = dict(tf_seconds or MTF_TF_SECONDS)
        self.indicator_tfs = frozenset(indicator_tfs)
        self._builders: Dict[str, _TFBuilder] = {
            tf: _TFBuilder(secs, self.epic, tf, max_complete=tf_buffer)
            for tf, secs in self.tf_seconds.items()
        }
        self.bars_emitted: Dict[str, int] = {tf: 0 for tf in self.tf_seconds}
        self._handles: Dict[str, object] = {}
        # Analyse de structure (tendance / retournement / liquidite) EN RAM +
        # journal DB, sur les TF a indicateurs. market_state partage (expose par
        # le runner) ; cree localement si non fourni.
        self.market_state = market_state if market_state is not None else MarketState()
        self._structure: Dict[str, StructureAnalyzer] = {
            tf: StructureAnalyzer(
                self.name, tf, self.market_state.get(self.name, tf), store=store)
            for tf in self.tf_seconds if tf in self.indicator_tfs
        }

    def on_s10_bar(self, candle: OHLCVCandle) -> List[str]:
        closed_tfs: List[str] = []
        for tf, builder in self._builders.items():
            closed = builder.add(candle)
            if closed is None:
                continue
            closed_tfs.append(tf)
            self.bars_emitted[tf] += 1
            # (a) persiste la bougie OHLCV cloturee
            if self.store is not None:
                self.store.append(f"{self.name}_{tf}", closed)
            # (b) indicateurs a la cloture, TF >= M5 uniquement
            if tf in self.indicator_tfs:
                bars = builder.complete_bars()
                iset = self._compute_tf(bars)
                ind = _iset_to_dict(iset)
                snapshot = {
                    "ts": closed.timestamp.isoformat(),
                    "epic": self.epic,
                    "name": self.name,
                    "tf": tf,
                    "ohlc": {
                        "open": closed.open, "high": closed.high,
                        "low": closed.low, "close": closed.close,
                        "volume": closed.volume, "tick_count": closed.tick_count,
                    },
                    "ind": ind,
                }
                self._emit(tf, closed.timestamp, snapshot)
                # (c) structure de marche (RAM + journal) sur la bougie cloturee
                analyzer = self._structure.get(tf)
                if analyzer is not None:
                    analyzer.on_closed_bar(closed, ind)
        return closed_tfs

    # -- interne --------------------------------------------------------------

    @staticmethod
    def _compute_tf(bars: List[OHLCVCandle]) -> IndicatorSet:
        if not bars:
            return IndicatorSet()
        close = np.array([b.close for b in bars], dtype=np.float64)
        high = np.array([b.high for b in bars], dtype=np.float64)
        low = np.array([b.low for b in bars], dtype=np.float64)
        volume = np.array([b.volume for b in bars], dtype=np.float64)
        return compute_from_arrays(close, high, low, volume)

    def _emit(self, tf: str, ts: datetime, snapshot: dict) -> None:
        if self.store is None:
            return
        # rotation minuit UTC : date UTC du timestamp (conversion explicite).
        if ts.tzinfo is not None:
            date = ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
        else:
            date = ts.strftime("%Y-%m-%d")
        key = f"{tf}_{date}"
        h = self._handles.get(key)
        if h is None:
            path = os.path.join(
                self.store.base_dir, f"analysis_{self.name}_{tf}_{date}.jsonl")
            h = open(path, "a", buffering=1, encoding="utf-8")
            self._handles[key] = h
        h.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
        h.flush()
        if getattr(self.store, "_fsync", False):
            os.fsync(h.fileno())

    def close(self) -> None:
        for h in self._handles.values():
            try:
                h.flush()
                h.close()
            except Exception:
                pass
        self._handles.clear()
        for analyzer in self._structure.values():
            analyzer.close()
