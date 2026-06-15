"""
kasper_setups.py - Implementations Python des setups Kasper Trading
====================================================================
Trois setups extraits par analyse des videos Kasper Trading.
Chaque setup est une StrategySpec injectable dans strategy_engine.py.

Sources :
  SETUP_B : https://www.youtube.com/watch?v=nz4D8myPijw
  SETUP_C : https://www.youtube.com/watch?v=fXxhb8alRBE
  SETUP_D : https://www.youtube.com/watch?v=RV8V0KIP5hM (filtres uniquement)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from loguru import logger
from strategy_spec import StrategySpec, SignalDirection, TradeSetup


# -----------------------------------------------------------------------
# Utilitaires communs
# -----------------------------------------------------------------------

def _calc_ote_zone(
    swing_low: float, swing_high: float
) -> Tuple[float, float]:
    """
    Calcule la zone OTE (Optimal Trade Entry) selon Kasper.
    Zone = retracement Fibonacci [0.618, 0.786].
    Retourne (ote_low, ote_high).
    """
    spread = swing_high - swing_low
    return (
        swing_low + 0.618 * spread,
        swing_low + 0.786 * spread,
    )


def _price_in_ote_long(
    price: float, swing_low: float, swing_high: float
) -> bool:
    """True si le prix est dans la zone OTE pour un retracement haussier."""
    ote_low, ote_high = _calc_ote_zone(swing_low, swing_high)
    return ote_low <= price <= ote_high


def _price_in_ote_short(
    price: float, swing_low: float, swing_high: float
) -> bool:
    """True si le prix est dans la zone OTE pour un retracement baissier."""
    ote_low  = swing_high - 0.786 * (swing_high - swing_low)
    ote_high = swing_high - 0.618 * (swing_high - swing_low)
    return ote_low <= price <= ote_high


def _detect_fvg_bull(candles_low: list, candles_high: list, idx: int) -> bool:
    """
    Detecte un Fair Value Gap haussier sur 3 bougies.
    FVG bull : high[idx-2] < low[idx]
    """
    if idx < 2 or idx >= len(candles_low):
        return False
    return candles_high[idx - 2] < candles_low[idx]


def _detect_fvg_bear(candles_low: list, candles_high: list, idx: int) -> bool:
    """FVG baissier : low[idx-2] > high[idx]"""
    if idx < 2 or idx >= len(candles_high):
        return False
    return candles_low[idx - 2] > candles_high[idx]


# -----------------------------------------------------------------------
# Sessions
# -----------------------------------------------------------------------

# Horaires Paris (UTC+1/UTC+2)
SESSIONS = {
    "XAUUSD_US":    (14, 18),
    "DAX_EUROPE":   (9,  12),
    "LONDON":       (8,  17),
    "NEWYORK":      (14, 23),
    "EUROPE_NY":    (8,  23),
}


def in_session(hour: int, session_key: str) -> bool:
    start, end = SESSIONS.get(session_key, (9, 17))
    return start <= hour < end


def london_or_ny(hour: int) -> bool:
    return in_session(hour, "LONDON") or in_session(hour, "NEWYORK")


# -----------------------------------------------------------------------
# SETUP B : Scalping OTE - MA200 H1 - Session
# -----------------------------------------------------------------------

@dataclass
class SetupBOTEContext:
    """Contexte externe necessaire pour evaluer SETUP_B."""
    swing_low:    float
    swing_high:   float
    h1_close:     float
    h1_ma200:     float
    daily_pnl:    float = 0.0
    daily_target: float = 0.0
    is_news:      bool  = False


def build_setup_b_ote(
    instrument:   str   = "XAUUSD",
    session_key:  str   = "XAUUSD_US",
    sl_atr_mult:  float = 1.5,
    rr_min:       float = 2.0,
    max_spread:   float = 3.0,
) -> StrategySpec:
    """
    SETUP_B : Scalping OTE avec filtre tendance H1 (MA200) et session.
    Source : https://www.youtube.com/watch?v=nz4D8myPijw
    """
    def signal_long(iset) -> bool:
        return False  # Evaluation complete via evaluate_setup_b()

    def signal_short(iset) -> bool:
        return False

    return StrategySpec(
        name          = "SETUP_B_OTE_MA200",
        description   = (
            "Scalping OTE Fibonacci [0.618-0.786] avec filtre tendance H1 MA200. "
            "Source : Kasper Trading https://www.youtube.com/watch?v=nz4D8myPijw"
        ),
        instrument    = instrument,
        timeframe     = "M1",
        sl_atr_mult   = sl_atr_mult,
        tp_atr_mult   = sl_atr_mult * rr_min,
        min_rr        = rr_min,
        max_spread    = max_spread,
        hour_start    = SESSIONS[session_key][0],
        hour_end      = SESSIONS[session_key][1],
        _fn_signal_long  = signal_long,
        _fn_signal_short = signal_short,
    )


def evaluate_setup_b(
    iset,
    ctx: SetupBOTEContext,
    hour: int,
    spread: float = 0.0,
    current_direction: Optional[str] = None,
) -> TradeSetup:
    """Evaluation complete de SETUP_B avec contexte externe."""
    spec = build_setup_b_ote()
    flat = TradeSetup(direction=SignalDirection.FLAT, setup_name=spec.name)

    if spec.kill_switch:
        flat.reason = "KILL_SWITCH"
        return flat

    if iset is None or not iset.is_ready:
        flat.reason = "WARMUP"
        return flat

    if ctx.daily_target > 0 and ctx.daily_pnl >= ctx.daily_target:
        flat.reason = "DAILY_TARGET_REACHED"
        return flat

    if ctx.is_news:
        flat.reason = "NEWS_FILTER"
        return flat

    if not (spec.hour_start <= hour < spec.hour_end):
        flat.reason = "SESSION_FILTER"
        return flat

    if spread > spec.max_spread:
        flat.reason = "SPREAD_FILTER"
        return flat

    if math.isnan(iset.atr) or iset.atr <= 0:
        flat.reason = "ATR_INVALIDE"
        return flat

    price = iset.close

    # --- Signal LONG ---
    h1_bull = ctx.h1_close > ctx.h1_ma200

    if h1_bull:
        if _price_in_ote_long(price, ctx.swing_low, ctx.swing_high):
            sl  = ctx.swing_low - iset.atr * 0.5
            tp  = price + spec.min_rr * (price - sl)
            rr  = (tp - price) / (price - sl) if (price - sl) > 0 else 0
            if rr >= spec.min_rr:
                logger.info("SETUP_B LONG signal",
                            price=round(price, 2), sl=round(sl, 2),
                            tp=round(tp, 2), rr=round(rr, 2))
                return TradeSetup(
                    direction=SignalDirection.LONG,
                    entry=price, stop_loss=sl, take_profit=tp,
                    risk_pts=price - sl, reward_pts=tp - price,
                    rr_ratio=rr, setup_name=spec.name,
                    reason="OTE_BULL_MA200_H1",
                )

    # --- Signal SHORT ---
    h1_bear = ctx.h1_close < ctx.h1_ma200

    if h1_bear:
        if _price_in_ote_short(price, ctx.swing_low, ctx.swing_high):
            sl  = ctx.swing_high + iset.atr * 0.5
            tp  = price - spec.min_rr * (sl - price)
            rr  = (price - tp) / (sl - price) if (sl - price) > 0 else 0
            if rr >= spec.min_rr:
                logger.info("SETUP_B SHORT signal",
                            price=round(price, 2), sl=round(sl, 2),
                            tp=round(tp, 2), rr=round(rr, 2))
                return TradeSetup(
                    direction=SignalDirection.SHORT,
                    entry=price, stop_loss=sl, take_profit=tp,
                    risk_pts=sl - price, reward_pts=price - tp,
                    rr_ratio=rr, setup_name=spec.name,
                    reason="OTE_BEAR_MA200_H1",
                )

    flat.reason = "PAS_DE_SIGNAL"
    return flat


# -----------------------------------------------------------------------
# SETUP C : Order Blocks - OB Frais - Tendance - Session - RR2
# -----------------------------------------------------------------------

@dataclass
class OrderBlock:
    """Representation d'un Order Block detecte."""
    ob_type:     str
    ob_high:     float
    ob_low:      float
    ob_mid:      float
    touch_count: int  = 0
    is_fresh:    bool = True

    @property
    def is_bullish(self) -> bool:
        return self.ob_type == "BULLISH"

    @property
    def is_bearish(self) -> bool:
        return self.ob_type == "BEARISH"

    def midpoint(self) -> float:
        return (self.ob_high + self.ob_low) / 2.0

    def price_in_zone(self, price: float) -> bool:
        return self.ob_low <= price <= self.ob_high

    def price_at_mid(self, price: float, tolerance_pct: float = 0.1) -> bool:
        mid = self.midpoint()
        tol = (self.ob_high - self.ob_low) * tolerance_pct
        return abs(price - mid) <= tol


@dataclass
class SetupCOBContext:
    """Contexte externe pour SETUP_C."""
    active_ob:  Optional[OrderBlock]
    trend:      str
    daily_pnl:  float = 0.0
    is_news:    bool  = False


def build_setup_c_ob(
    instrument:       str   = "XAUUSD",
    entry_variant:    str   = "MIDPOINT",
    require_confirm:  bool  = False,
    rr_min:           float = 2.0,
    sl_buffer_mult:   float = 0.5,
    max_spread:       float = 3.0,
) -> StrategySpec:
    """
    SETUP_C : Order Block frais en tendance + session Londres/NY.
    Source : https://www.youtube.com/watch?v=fXxhb8alRBE
    """
    return StrategySpec(
        name        = "SETUP_C_OB_Frais",
        description = (
            "OB frais en tendance, session Londres/NY, RR>=2. "
            "Source : Kasper Trading https://www.youtube.com/watch?v=fXxhb8alRBE"
        ),
        instrument  = instrument,
        timeframe   = "M5",
        sl_atr_mult = sl_buffer_mult,
        tp_atr_mult = sl_buffer_mult * rr_min * 2,
        min_rr      = rr_min,
        max_spread  = max_spread,
        hour_start  = 8,
        hour_end    = 23,
    )


def evaluate_setup_c(
    iset,
    ctx:  SetupCOBContext,
    hour: int,
    spread: float = 0.0,
    entry_variant: str = "MIDPOINT",
) -> TradeSetup:
    """Evaluation complete de SETUP_C avec contexte OB externe."""
    spec = build_setup_c_ob()
    flat = TradeSetup(direction=SignalDirection.FLAT, setup_name=spec.name)

    if iset is None or not iset.is_ready:
        flat.reason = "WARMUP"
        return flat

    if ctx.is_news:
        flat.reason = "NEWS_FILTER"
        return flat

    if not london_or_ny(hour):
        flat.reason = "SESSION_FILTER"
        return flat

    if spread > spec.max_spread:
        flat.reason = "SPREAD_FILTER"
        return flat

    ob = ctx.active_ob
    if ob is None:
        flat.reason = "PAS_DE_OB"
        return flat

    if not ob.is_fresh or ob.touch_count > 0:
        flat.reason = "OB_PAS_FRAIS"
        return flat

    if ctx.trend == "BULL" and not ob.is_bullish:
        flat.reason = "OB_CONTRA_TENDANCE"
        return flat
    if ctx.trend == "BEAR" and not ob.is_bearish:
        flat.reason = "OB_CONTRA_TENDANCE"
        return flat

    price = iset.close
    atr   = iset.atr

    # Point d'entree selon la variante
    if entry_variant == "EDGE":
        entry_level_long  = ob.ob_low
        entry_level_short = ob.ob_high
    else:  # MIDPOINT
        entry_level_long  = ob.midpoint()
        entry_level_short = ob.midpoint()

    # --- Signal LONG ---
    if ob.is_bullish and ctx.trend in ("BULL", "NEUTRAL"):
        tolerance = atr * 0.3
        if abs(price - entry_level_long) <= tolerance:
            sl = ob.ob_low - atr * spec.sl_atr_mult
            tp = price + spec.min_rr * (price - sl)
            rr = (tp - price) / (price - sl) if (price - sl) > 0 else 0
            if rr >= spec.min_rr:
                logger.info("SETUP_C LONG signal",
                            price=round(price, 2), ob_mid=round(ob.midpoint(), 2),
                            sl=round(sl, 2), tp=round(tp, 2), rr=round(rr, 2))
                return TradeSetup(
                    direction=SignalDirection.LONG,
                    entry=price, stop_loss=sl, take_profit=tp,
                    risk_pts=price - sl, reward_pts=tp - price,
                    rr_ratio=rr, setup_name=spec.name,
                    reason=f"OB_BULL_FRAIS_{entry_variant}",
                )

    # --- Signal SHORT ---
    if ob.is_bearish and ctx.trend in ("BEAR", "NEUTRAL"):
        tolerance = atr * 0.3
        if abs(price - entry_level_short) <= tolerance:
            sl = ob.ob_high + atr * spec.sl_atr_mult
            tp = price - spec.min_rr * (sl - price)
            rr = (price - tp) / (sl - price) if (sl - price) > 0 else 0
            if rr >= spec.min_rr:
                logger.info("SETUP_C SHORT signal",
                            price=round(price, 2), sl=round(sl, 2),
                            tp=round(tp, 2), rr=round(rr, 2))
                return TradeSetup(
                    direction=SignalDirection.SHORT,
                    entry=price, stop_loss=sl, take_profit=tp,
                    risk_pts=sl - price, reward_pts=price - tp,
                    rr_ratio=rr, setup_name=spec.name,
                    reason=f"OB_BEAR_FRAIS_{entry_variant}",
                )

    flat.reason = "PAS_DE_SIGNAL"
    return flat


# -----------------------------------------------------------------------
# SETUP D : SMC - Detecteurs de zones (couche filtre uniquement)
# -----------------------------------------------------------------------

@dataclass
class SMCZones:
    """Zones SMC detectees sur le marche courant."""
    bias:       str  = "NEUTRAL"
    fvg_bull:   bool = False
    fvg_bear:   bool = False
    ob_bull:    bool = False
    ob_bear:    bool = False
    in_ote:     bool = False
    amd_phase:  str  = "UNKNOWN"

    def confluence_score_long(self) -> int:
        return sum([
            self.bias == "BULL",
            self.fvg_bull,
            self.ob_bull,
            self.in_ote,
            self.amd_phase == "DISTRIBUTION",
        ])

    def confluence_score_short(self) -> int:
        return sum([
            self.bias == "BEAR",
            self.fvg_bear,
            self.ob_bear,
            self.in_ote,
            self.amd_phase == "DISTRIBUTION",
        ])


def compute_smc_zones(
    closes: list, highs: list, lows: list,
    swing_low: float, swing_high: float,
    atr: float,
) -> SMCZones:
    """
    Calcule les zones SMC depuis les donnees de prix.
    Retourne un SMCZones utilisable comme couche de filtre.
    """
    n = len(closes)
    if n < 10:
        return SMCZones()

    price = closes[-1]
    zones = SMCZones()

    # --- Biais de structure (proxy simplifie) ---
    # Tendance haussiere : derniere cloture > premiere des 10 derniers bars
    # Seuil minimal : 0.0001 (1 tick) pour eviter le bruit pur
    recent = closes[-10:]
    if recent[-1] > recent[0]:
        zones.bias = "BULL"
    elif recent[-1] < recent[0]:
        zones.bias = "BEAR"

    # --- FVG sur les 20 dernieres bougies ---
    for i in range(2, min(n, 20)):
        if _detect_fvg_bull(lows, highs, n - 1 - i + 2):
            zones.fvg_bull = True
        if _detect_fvg_bear(lows, highs, n - 1 - i + 2):
            zones.fvg_bear = True

    # --- Zone OTE ---
    if swing_high > swing_low:
        zones.in_ote = _price_in_ote_long(price, swing_low, swing_high)

    # --- Phase AMD (proxy par volatilite) ---
    if n >= 20:
        atr_recent = sum(
            highs[-i] - lows[-i] for i in range(1, 11)
        ) / 10
        atr_older = sum(
            highs[-i] - lows[-i] for i in range(11, 21)
        ) / 10
        if atr_recent < atr_older * 0.7:
            zones.amd_phase = "ACCUMULATION"
        elif atr_recent > atr_older * 1.3:
            zones.amd_phase = "DISTRIBUTION"
        else:
            zones.amd_phase = "MANIPULATION"

    return zones


def smc_filter_passes(zones: SMCZones, direction: str, min_score: int = 2) -> bool:
    """
    Filtre SMC : True si le contexte SMC confirme la direction.
    min_score = nombre minimum de confluences requises.
    """
    if direction == "LONG":
        return zones.confluence_score_long() >= min_score
    elif direction == "SHORT":
        return zones.confluence_score_short() >= min_score
    return False
