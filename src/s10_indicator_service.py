"""
s10_indicator_service.py - Reconstitution des indicateurs "comme en reel".
==========================================================================
Reutilise le VRAI moteur du code live :
  - IndicatorEngine / compute_from_arrays (vps_snapshot/src/indicators.py)
  - OHLCVCandle (vps_snapshot/src/data_feed.py)

Sur chaque nouvelle bougie S10 (cf spec section 2.3) :
  a. Indicateurs S10 NATIFS : un IndicatorEngine alimente en barres S10 ->
     IndicatorSet au pas de 10s.
  b. TF SUPERIEURES en cours de formation : on agrege les barres S10 en M5/M15/H1
     (bougie courante INCLUSE, non clôturee) et on calcule l'IndicatorSet de chaque
     TF avec compute_from_arrays -- exactement ce que les strategies voient, mais
     rafraichi toutes les 10s.

Snapshot emis/loggue en JSONL : data/s10/analysis_{name}_{date}.jsonl

FIDELITE : l'agregation S10->TF reconstruit l'OHLC exact (open=1er S10, close=dernier,
high/low = extremes), et l'IndicatorSet est calcule par le MEME compute_from_arrays
que le live -> les valeurs sont identiques (preuve dans test_s10.py).
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np


from data_feed import OHLCVCandle  # noqa: E402
from indicators import (  # noqa: E402
    IndicatorEngine, IndicatorSet, compute_from_arrays, BUFFER_DEFAULT,
)

S10_SECONDS = 10
DEFAULT_TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600}
# Nombre de bougies TF *completes* gardees par timeframe pour le calcul.
# On garde >= BUFFER_DEFAULT (100) bougies TF de sorte que EMA50 / MACD / etc.
# soient STABLES et NON-NaN pour CHAQUE TF (y compris H1), exactement comme le
# moteur live. C'est un buffer de bougies TF (pas de S10), donc H1 ne depend plus
# d'une fenetre de S10 finie et n'a plus jamais ema_slow en NaN permanent.
TF_BUFFER = BUFFER_DEFAULT


def _tf_bucket(epoch_s: float, tf_seconds: int) -> int:
    return int(epoch_s // tf_seconds) * tf_seconds


def aggregate_s10_to_tf(
    s10: List[OHLCVCandle],
    tf_seconds: int,
    epic: str,
    scale: str,
) -> List[OHLCVCandle]:
    """
    Agrege une liste de bougies S10 (ordonnees) en bougies de timeframe tf_seconds.
    open = 1er S10 du bucket, close = dernier, high/low = extremes, volume = somme.
    La DERNIERE bougie TF est potentiellement EN COURS DE FORMATION (incluse telle
    quelle, non clôturee) -- c'est voulu pour refleter l'etat live au pas de 10s.
    """
    out: List[OHLCVCandle] = []
    cur_bucket: Optional[int] = None
    cur: Optional[OHLCVCandle] = None
    for c in s10:
        b = _tf_bucket(c.timestamp.timestamp(), tf_seconds)
        if cur is None or b != cur_bucket:
            if cur is not None:
                out.append(cur)
            cur_bucket = b
            cur = OHLCVCandle(
                epic=epic, scale=scale,
                timestamp=datetime.fromtimestamp(b, tz=timezone.utc),
                open=c.open, high=c.high, low=c.low, close=c.close,
                volume=c.volume, tick_count=c.tick_count,
                bid_close=c.bid_close, ask_close=c.ask_close,
                is_complete=False,
            )
        else:
            if c.high > cur.high:
                cur.high = c.high
            if c.low < cur.low:
                cur.low = c.low
            cur.close = c.close
            cur.volume += c.volume
            cur.tick_count += c.tick_count
            cur.bid_close = c.bid_close
            cur.ask_close = c.ask_close
    if cur is not None:
        out.append(cur)
    return out


class _TFAggregator:
    """
    Agregateur INCREMENTAL S10 -> une timeframe (M5/M15/H1).

    Maintient :
      - une liste bornee des dernieres bougies TF COMPLETES (max_complete) ;
      - la bougie TF EN COURS DE FORMATION (current), incluse dans le calcul.

    Avantages vs re-scan d'un buffer de S10 a chaque barre :
      - O(1) par barre S10 (pas de re-agregation O(buffer)) ;
      - on ne tronque JAMAIS une bougie TF au milieu d'un bucket : on ne deque
        que des bougies COMPLETES -> pas d'OHLC partiel silencieux ;
      - on garde assez de bougies TF (>= TF_BUFFER) pour que EMA50/MACD soient
        stables pour CHAQUE TF, H1 inclus.
    """

    def __init__(self, tf_seconds: int, epic: str, scale: str,
                 max_complete: int = TF_BUFFER) -> None:
        self.tf_seconds = tf_seconds
        self.epic = epic
        self.scale = scale
        # Fenetre = identique a un IndicatorEngine(maxlen=max_complete) du live :
        # au plus `max_complete` bougies (completes + la forming). On garde donc
        # jusqu'a max_complete-1 completes + 1 forming, OU max_complete completes
        # quand aucune forming. La fenetre EXPOSEE est cappee a max_complete.
        self.max_complete = max_complete
        self._complete: List[OHLCVCandle] = []
        self._cur: Optional[OHLCVCandle] = None
        self._cur_bucket: Optional[int] = None

    def add(self, c: OHLCVCandle) -> None:
        b = _tf_bucket(c.timestamp.timestamp(), self.tf_seconds)
        if self._cur is None:
            self._open(b, c)
            return
        if b == self._cur_bucket:
            self._merge(c)
            return
        if b > self._cur_bucket:
            # frontiere TF franchie : la bougie courante est COMPLETE.
            self._cur.is_complete = True
            self._complete.append(self._cur)
            if len(self._complete) > self.max_complete:
                # on ne deque QUE des bougies completes : pas de troncature partielle.
                self._complete = self._complete[-self.max_complete:]
            self._open(b, c)
        # b < cur_bucket : barre S10 en retard -> ignoree (anti-look-ahead amont).

    def bars_including_current(self) -> List[OHLCVCandle]:
        """Fenetre EXACTE d'un IndicatorEngine(maxlen=max_complete) live : les
        dernieres `max_complete` bougies TF (completes + la forming, non clôturee).
        On cappe a max_complete pour que le calcul soit byte-identique au live."""
        bars = self._complete if self._cur is None else self._complete + [self._cur]
        if len(bars) > self.max_complete:
            bars = bars[-self.max_complete:]
        return bars

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


class S10IndicatorService:
    """
    Reconstitue les indicateurs sur chaque barre S10 et les loggue.

    on_s10_bar(candle) -> snapshot dict {ts, s10:{...}, tf:{M5:{...}, ...}}
    """

    def __init__(
        self,
        epic: str,
        name: str,
        store=None,
        tf_seconds: Optional[Dict[str, int]] = None,
        s10_buffer: int = BUFFER_DEFAULT,
        tf_buffer: int = TF_BUFFER,
    ) -> None:
        self.epic = epic
        self.name = name
        self.store = store
        self.tf_seconds = dict(tf_seconds or DEFAULT_TF_SECONDS)
        # (a) moteur S10 natif (le vrai IndicatorEngine du live)
        self._s10_engine = IndicatorEngine(maxlen=s10_buffer)
        # (b) un agregateur INCREMENTAL par TF : garde >= tf_buffer bougies TF
        # COMPLETES (pas un buffer de S10), donc H1 reste stable et non-NaN.
        self._tf_aggs: Dict[str, _TFAggregator] = {
            tf_name: _TFAggregator(secs, self.epic, tf_name, max_complete=tf_buffer)
            for tf_name, secs in self.tf_seconds.items()
        }
        self.snapshots_emitted = 0
        self._handles: Dict[str, object] = {}

    def on_s10_bar(self, candle: OHLCVCandle) -> dict:
        """Traite une bougie S10 finalisee. Retourne le snapshot d'analyse."""
        # (a) indicateurs S10 natifs
        self._s10_engine.push(candle)
        s10_set = self._s10_engine.compute()

        # (b) TF superieures en cours de formation (agregation incrementale O(1))
        tf_snaps: Dict[str, dict] = {}
        for tf_name, agg in self._tf_aggs.items():
            agg.add(candle)
            tf_bars = agg.bars_including_current()
            iset = self._compute_tf(tf_bars)
            tf_snaps[tf_name] = _iset_to_dict(iset)

        snapshot = {
            "ts": candle.timestamp.isoformat(),
            "epic": self.epic,
            "name": self.name,
            "s10": _iset_to_dict(s10_set),
            "tf": tf_snaps,
        }
        self._emit(candle.timestamp, snapshot)
        return snapshot

    # -- interne --------------------------------------------------------------

    @staticmethod
    def _compute_tf(tf_bars: List[OHLCVCandle]) -> IndicatorSet:
        if not tf_bars:
            return IndicatorSet()
        close = np.array([b.close for b in tf_bars], dtype=np.float64)
        high = np.array([b.high for b in tf_bars], dtype=np.float64)
        low = np.array([b.low for b in tf_bars], dtype=np.float64)
        volume = np.array([b.volume for b in tf_bars], dtype=np.float64)
        return compute_from_arrays(close, high, low, volume)

    def _emit(self, ts: datetime, snapshot: dict) -> None:
        self.snapshots_emitted += 1
        if self.store is None:
            return
        # rotation minuit UTC : date UTC du timestamp (conversion explicite).
        if ts.tzinfo is not None:
            date = ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
        else:
            date = ts.strftime("%Y-%m-%d")
        key = date
        h = self._handles.get(key)
        if h is None:
            path = os.path.join(self.store.base_dir, f"analysis_{self.name}_{date}.jsonl")
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
