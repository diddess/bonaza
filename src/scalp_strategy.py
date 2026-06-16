"""
scalp_strategy.py - Strategie de scalping LIVE : momentum 3xS10 + tendance M5.
=============================================================================
Portage exact de la variante GAGNANTE du backtest (cf backtest_scalp_m1.py,
mode with_trend_only) :

  - SIGNAL : 3 bougies S10 CONSECUTIVES (contigues) de meme sens
        3 haussieres (close>open) -> LONG ; 3 baissieres -> SHORT.
  - FILTRE : on n'ouvre QUE dans le sens de la TENDANCE M5 (structure reconstruite
        en RAM par le S10Runner / MarketState). Contre-tendance et range -> rejetes.
  - SL/TP  : depuis l'ATR M5 courant (R:R = tp_mult/sl_mult). Si l'ATR M5 n'est pas
        encore disponible (warmup) -> AUCUN trade (decision via M5 obligatoire).
  - TAILLE : fixe (1 lot ~= 1 EUR/pt ; clampee au mini IG par l'executeur).

evaluate(name, candle) -> Optional[TradeSetup] (None si pas de signal/filtre KO).
Ne place AUCUN ordre : renvoie l'intention, l'entrypoint gere session / position
unique / cooldown / execution.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional

from loguru import logger

from strategy_spec import TradeSetup, SignalDirection


class ScalpParamsLive:
    def __init__(
        self,
        momentum_bars: int = 3,
        base_seconds: int = 10,
        sl_atr_mult: float = 1.0,       # R:R 3 (backtest 16/06 : SL1.5/TP2.4 cassait l'edge)
        tp_atr_mult: float = 3.0,
        tp_atr_mult_by_instrument: Optional[Dict[str, float]] = None,  # override TP par instrument
        size: float = 1.0,
        with_trend_only: bool = True,
        grab_enabled: bool = True,
        grab_lookback: int = 15,        # fenetre S10 pour le plus haut/bas balaye
        vol_window: int = 20,           # fenetre pour la moyenne de volume
        vol_spike_mult: float = 3.0,    # volume "massif" = > 3x la moyenne recente
        correlated: Optional[Dict[str, str]] = None,  # indices correles (controle de sens)
    ) -> None:
        self.momentum_bars = momentum_bars
        self.base_seconds = base_seconds
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        # TP uniforme 3.0 (R:R 3). Le -20% DAX (TP 2.4) cassait l'edge au backtest.
        self.tp_atr_mult_by_instrument = tp_atr_mult_by_instrument or {}
        self.size = size
        self.with_trend_only = with_trend_only
        self.grab_enabled = grab_enabled
        self.grab_lookback = grab_lookback
        self.vol_window = vol_window
        self.vol_spike_mult = vol_spike_mult
        # controle de correlation : on n'ouvre PAS si l'indice correle est en
        # tendance M5 OPPOSEE (evite long DAX quand CAC est bear, etc.).
        self.correlated = correlated or {"DAX": "CAC40", "CAC40": "DAX"}


class ScalpLiveStrategy:
    def __init__(self, instruments: dict, market_state, params: Optional[ScalpParamsLive] = None) -> None:
        self.market_state = market_state
        self.p = params or ScalpParamsLive()
        maxlen = max(self.p.momentum_bars, self.p.grab_lookback, self.p.vol_window) + 2
        self._buf: Dict[str, Deque] = {
            name: deque(maxlen=maxlen) for name in instruments
        }

    def evaluate(self, name: str, candle) -> Optional[TradeSetup]:
        buf = self._buf.get(name)
        if buf is None:
            return None
        buf.append(candle)

        # (1) PRIORITE : prise de liquidite + spike de volume (sweep d'un extreme S10
        #     puis rejet, avec volume "massif") -> le signal le plus fort.
        if self.p.grab_enabled:
            sig = self._grab_signal(name, candle)
            if sig is not None:
                return sig

        # (2) Momentum : 3 bougies S10 consecutives meme sens.
        mb = self.p.momentum_bars
        if len(buf) < mb:
            return None
        last = list(buf)[-mb:]
        # contiguite : les mb bougies doivent etre consecutives (pas de gap)
        for a, b in zip(last, last[1:]):
            if int(b.timestamp.timestamp()) - int(a.timestamp.timestamp()) != self.p.base_seconds:
                return None
        up = all(c.close > c.open for c in last)
        dn = all(c.close < c.open for c in last)
        if not (up or dn):
            return None
        side = SignalDirection.LONG if up else SignalDirection.SHORT
        trend = self.market_state.get(name, "M5").trend
        with_trend = (up and trend == "bull") or (dn and trend == "bear")
        if self.p.with_trend_only and not with_trend:
            return None
        return self._build_setup(name, side, candle.close, "momentum3xS10", trend)

    def _grab_signal(self, name: str, c) -> Optional[TradeSetup]:
        """Sweep d'un extreme S10 recent + rejet + volume massif, filtre tendance M5.
        - sell-side (balaie le plus bas, cloture au-dessus) + volume -> achat massif -> LONG
        - buy-side  (balaie le plus haut, cloture en-dessous) + volume -> vente massive -> SHORT
        """
        buf = list(self._buf[name])
        need = max(self.p.grab_lookback, self.p.vol_window) + 1
        if len(buf) < need:
            return None
        window = buf[-(self.p.grab_lookback + 1):-1]   # exclut la bougie courante
        ref_high = max(x.high for x in window)
        ref_low = min(x.low for x in window)
        vols = [x.volume for x in buf[-(self.p.vol_window + 1):-1]]
        avg_vol = (sum(vols) / len(vols)) if vols else 0.0
        if avg_vol <= 0 or c.volume < self.p.vol_spike_mult * avg_vol:
            return None   # pas de volume "massif"

        trend = self.market_state.get(name, "M5").trend
        if c.low < ref_low and c.close > ref_low:           # grab sell-side -> LONG
            if self.p.with_trend_only and trend != "bull":
                return None
            side = SignalDirection.LONG
        elif c.high > ref_high and c.close < ref_high:      # grab buy-side -> SHORT
            if self.p.with_trend_only and trend != "bear":
                return None
            side = SignalDirection.SHORT
        else:
            return None
        return self._build_setup(
            name, side, c.close,
            f"grab+vol(x{c.volume/avg_vol:.1f})", trend)

    def _build_setup(self, name: str, side, entry: float, tag: str, trend: str) -> Optional[TradeSetup]:
        # CONTROLE DE CORRELATION : pas d'ordre contraire a l'indice correle
        # (ex. pas de long DAX si CAC en tendance M5 baissiere).
        corr = self.p.correlated.get(name)
        if corr:
            ct = self.market_state.get(corr, "M5").trend
            if (side == SignalDirection.LONG and ct == "bear") or \
               (side == SignalDirection.SHORT and ct == "bull"):
                logger.info("[SCALP] %s %s bloque : indice correle %s en tendance opposee (%s)"
                            % (name, side.value, corr, ct))
                return None
        # SL/TP depuis l'ATR M5 (None tant que la M5 n'est pas prete -> pas de trade)
        st = self.market_state.get(name, "M5")
        atr = st.last_indicators.get("atr") if st.last_indicators else None
        if atr is None or atr <= 0:
            return None
        sl_d = self.p.sl_atr_mult * atr
        tp_d = self.p.tp_atr_mult_by_instrument.get(name, self.p.tp_atr_mult) * atr
        if side == SignalDirection.LONG:
            sl, tp = entry - sl_d, entry + tp_d
        else:
            sl, tp = entry + sl_d, entry - tp_d
        logger.info(
            f"[SCALP] {name} signal {side.value} | {tag} | trend M5={trend} "
            f"| ATR_M5={atr:.2f} | E={entry:.2f} SL={sl:.2f} TP={tp:.2f}"
        )
        return TradeSetup(
            direction=side, entry=round(entry, 2),
            stop_loss=round(sl, 2), take_profit=round(tp, 2),
            risk_pts=round(sl_d, 2), reward_pts=round(tp_d, 2),
            size=self.p.size, setup_name=f"SCALP_S10_{name}_{tag.split('(')[0]}",
        )
