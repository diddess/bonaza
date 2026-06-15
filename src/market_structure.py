"""
market_structure.py - Analyse de structure de marche (en RAM + journal DB).
===========================================================================
Au-dessus des bougies multi-timeframe (cf mtf_service.py), maintient pour chaque
(instrument, timeframe) :
  - les DERNIERS INDICATEURS CLES (snapshot du dernier IndicatorSet) ;
  - les SWINGS (sommets/creux pivots, fractal N-N, confirmes N bougies plus tard) ;
  - l'ETAT DE TENDANCE :
        bull  = suite de plus hauts plus hauts (HH) ET plus bas plus hauts (HL)
        bear  = suite de plus hauts plus bas  (LH) ET plus bas plus bas  (LL)
        range = sinon ;
  - les RETOURNEMENTS (CHoCH) : en tendance haussiere, cloture SOUS le dernier
    creux confirme -> reversal baissier ; symetrique en baissiere. + un evenement
    'trend_change' au changement de label ;
  - les PRISES DE LIQUIDITE (liquidity grab, definition stricte "meche + rejet +
    retour intra-bougie") :
        cote vente (sell-side) : low < swing_low ET close > swing_low (sweep d'un
            creux puis retour au-dessus) -> grab haussier ;
        cote achat (buy-side)  : high > swing_high ET close < swing_high -> grab
            baissier.
    Le swing balaye est marque "swept" pour ne pas re-declencher sur le meme niveau.

TOUT est conserve EN MEMOIRE (MarketState, accessible via le runner) ET les
EVENEMENTS sont journalises en base : data/s10/structure_{name}_{TF}_{date}.jsonl.
ZERO decision de trading : observabilite/structure uniquement.
"""
from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

from data_feed import OHLCVCandle

PIVOT_DEFAULT = 3          # fractal 3-3 (3 bougies de chaque cote)
SWING_KEEP = 50            # swings confirmes gardes par cote
EVENT_KEEP = 30           # derniers evenements gardes en RAM par TF
CANDLE_BUFFER = 2 * PIVOT_DEFAULT + 50  # assez pour confirmer les pivots


@dataclass
class Swing:
    ts: str
    price: float
    kind: str            # 'H' (sommet) ou 'L' (creux)
    swept: bool = False  # True une fois pris en liquidite (anti re-trigger)


@dataclass
class StructureState:
    """Etat EN RAM d'un (instrument, TF). Sérialisable via to_dict()."""
    name: str
    tf: str
    trend: str = "range"                 # 'bull' | 'bear' | 'range'
    last_indicators: dict = field(default_factory=dict)
    last_close: Optional[float] = None
    last_ts: Optional[str] = None
    swing_highs: List[Swing] = field(default_factory=list)
    swing_lows: List[Swing] = field(default_factory=list)
    last_reversal: Optional[dict] = None
    events: Deque[dict] = field(default_factory=lambda: deque(maxlen=EVENT_KEEP))

    def to_dict(self) -> dict:
        return {
            "name": self.name, "tf": self.tf, "trend": self.trend,
            "last_close": self.last_close, "last_ts": self.last_ts,
            "last_indicators": self.last_indicators,
            "swing_highs": [s.price for s in self.swing_highs[-5:]],
            "swing_lows": [s.price for s in self.swing_lows[-5:]],
            "last_reversal": self.last_reversal,
            "recent_events": list(self.events)[-5:],
        }


class MarketState:
    """Registre EN MEMOIRE de toute la structure : state[name][tf] -> StructureState."""

    def __init__(self) -> None:
        self._state: Dict[str, Dict[str, StructureState]] = {}

    def get(self, name: str, tf: str) -> StructureState:
        per = self._state.setdefault(name, {})
        st = per.get(tf)
        if st is None:
            st = StructureState(name=name, tf=tf)
            per[tf] = st
        return st

    def snapshot(self) -> dict:
        return {n: {tf: st.to_dict() for tf, st in per.items()}
                for n, per in self._state.items()}

    def trend_summary(self) -> str:
        parts = []
        for n, per in self._state.items():
            tfs = ",".join(f"{tf}:{st.trend[0]}" for tf, st in per.items())
            parts.append(f"{n}[{tfs}]")
        return " ".join(parts)


class StructureAnalyzer:
    """
    Analyse incrementale d'une (instrument, TF) sur les bougies CLOTUREES.

    on_closed_bar(candle, indicators) :
      1. stocke les indicateurs cles dans le StructureState (RAM) ;
      2. detecte un pivot confirme (fractal pivot-pivot) sur la bougie a -pivot ;
      3. met a jour les sequences de swings + le label de tendance ;
      4. detecte CHoCH (retournement) et liquidity grab sur la bougie cloturee ;
      5. journalise chaque evenement (structure_{name}_{TF}_{date}.jsonl).
    Retourne la liste des evenements emis par cette bougie.
    """

    def __init__(self, name: str, tf: str, state: StructureState,
                 store=None, pivot: int = PIVOT_DEFAULT) -> None:
        self.name = name
        self.tf = tf
        self.state = state
        self.store = store
        self.pivot = pivot
        self._bars: Deque[OHLCVCandle] = deque(maxlen=CANDLE_BUFFER)
        self._handles: Dict[str, object] = {}

    def on_closed_bar(self, candle: OHLCVCandle,
                      indicators: Optional[dict] = None) -> List[dict]:
        events: List[dict] = []
        st = self.state
        if indicators is not None:
            st.last_indicators = self._key_indicators(indicators)
        st.last_close = candle.close
        st.last_ts = candle.timestamp.isoformat()
        self._bars.append(candle)

        # (2) pivot confirme : la bougie candidate est a -pivot du bord droit.
        self._detect_pivot(events)
        # (4) CHoCH + liquidity grab sur la bougie qui vient de cloturer.
        self._detect_reversal(candle, events)
        self._detect_liquidity_grab(candle, events)

        for ev in events:
            self._emit(candle.timestamp, ev)
            st.events.append(ev)
        return events

    # -- detection ------------------------------------------------------------

    def _detect_pivot(self, events: List[dict]) -> None:
        n = len(self._bars)
        if n < 2 * self.pivot + 1:
            return
        bars = self._bars
        c_idx = n - 1 - self.pivot              # bougie candidate (centre)
        cand = bars[c_idx]
        left = [bars[i] for i in range(c_idx - self.pivot, c_idx)]
        right = [bars[i] for i in range(c_idx + 1, c_idx + 1 + self.pivot)]

        # swing high : sommet strictement superieur a ses voisins.
        if all(cand.high > b.high for b in left) and all(cand.high > b.high for b in right):
            self._add_swing(Swing(cand.timestamp.isoformat(), cand.high, "H"), events)
        # swing low : creux strictement inferieur a ses voisins.
        if all(cand.low < b.low for b in left) and all(cand.low < b.low for b in right):
            self._add_swing(Swing(cand.timestamp.isoformat(), cand.low, "L"), events)

    def _add_swing(self, sw: Swing, events: List[dict]) -> None:
        st = self.state
        seq = st.swing_highs if sw.kind == "H" else st.swing_lows
        seq.append(sw)
        if len(seq) > SWING_KEEP:
            del seq[:-SWING_KEEP]
        self._update_trend(events)

    def _update_trend(self, events: List[dict]) -> None:
        st = self.state
        sh, sl = st.swing_highs, st.swing_lows
        if len(sh) < 2 or len(sl) < 2:
            return
        hh = sh[-1].price > sh[-2].price        # higher high
        hl = sl[-1].price > sl[-2].price        # higher low
        ll = sl[-1].price < sl[-2].price        # lower low
        lh = sh[-1].price < sh[-2].price        # lower high
        if hh and hl:
            new_trend = "bull"
        elif lh and ll:
            new_trend = "bear"
        else:
            new_trend = "range"
        if new_trend != st.trend:
            old = st.trend
            st.trend = new_trend
            events.append({
                "ts": sh[-1].ts if sh[-1].ts >= sl[-1].ts else sl[-1].ts,
                "name": self.name, "tf": self.tf, "type": "trend_change",
                "from": old, "to": new_trend,
                "swing_high": sh[-1].price, "swing_low": sl[-1].price,
            })

    def _detect_reversal(self, candle: OHLCVCandle, events: List[dict]) -> None:
        """CHoCH : cassure du dernier swing oppose CONTRE la tendance en cours."""
        st = self.state
        if st.trend == "bull" and st.swing_lows:
            ref = st.swing_lows[-1]
            if candle.close < ref.price:
                ev = {
                    "ts": candle.timestamp.isoformat(), "name": self.name,
                    "tf": self.tf, "type": "reversal", "direction": "bearish",
                    "broke_swing_low": ref.price, "close": candle.close,
                }
                st.last_reversal = ev
                events.append(ev)
        elif st.trend == "bear" and st.swing_highs:
            ref = st.swing_highs[-1]
            if candle.close > ref.price:
                ev = {
                    "ts": candle.timestamp.isoformat(), "name": self.name,
                    "tf": self.tf, "type": "reversal", "direction": "bullish",
                    "broke_swing_high": ref.price, "close": candle.close,
                }
                st.last_reversal = ev
                events.append(ev)

    def _detect_liquidity_grab(self, candle: OHLCVCandle, events: List[dict]) -> None:
        st = self.state
        # sell-side : balaie un creux confirme puis cloture au-dessus (rejet+retour).
        for sw in reversed(st.swing_lows):
            if sw.swept:
                break  # les plus anciens sont deja traites
            if candle.low < sw.price and candle.close > sw.price:
                sw.swept = True
                events.append({
                    "ts": candle.timestamp.isoformat(), "name": self.name,
                    "tf": self.tf, "type": "liquidity_grab", "side": "sell_side",
                    "bias": "bullish", "swept_level": sw.price,
                    "wick_low": candle.low, "close": candle.close,
                })
        # buy-side : balaie un sommet confirme puis cloture en-dessous.
        for sw in reversed(st.swing_highs):
            if sw.swept:
                break
            if candle.high > sw.price and candle.close < sw.price:
                sw.swept = True
                events.append({
                    "ts": candle.timestamp.isoformat(), "name": self.name,
                    "tf": self.tf, "type": "liquidity_grab", "side": "buy_side",
                    "bias": "bearish", "swept_level": sw.price,
                    "wick_high": candle.high, "close": candle.close,
                })

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _key_indicators(ind: dict) -> dict:
        keys = ["close", "ema_fast", "ema_slow", "rsi", "macd_line",
                "macd_signal", "macd_hist", "atr", "bb_upper", "bb_mid",
                "bb_lower", "stoch_k", "stoch_d", "volume_ma", "bar_count"]
        return {k: ind.get(k) for k in keys if k in ind}

    def _emit(self, ts: datetime, ev: dict) -> None:
        if self.store is None:
            return
        date = (ts.astimezone(timezone.utc) if ts.tzinfo else ts).strftime("%Y-%m-%d")
        h = self._handles.get(date)
        if h is None:
            path = os.path.join(
                self.store.base_dir, f"structure_{self.name}_{self.tf}_{date}.jsonl")
            h = open(path, "a", buffering=1, encoding="utf-8")
            self._handles[date] = h
        h.write(json.dumps(ev, separators=(",", ":")) + "\n")
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
