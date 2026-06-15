"""
strategy_spec.py - Structures de données + StrategySpec
========================================================
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

from loguru import logger

try:
    from indicators import IndicatorSet
except ImportError:
    IndicatorSet = None  # type: ignore


class SignalDirection(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    FLAT  = "FLAT"


@dataclass
class TradeSetup:
    """Résultat de l'évaluation d'un setup sur un bar."""
    direction:   SignalDirection
    entry:       float = 0.0
    stop_loss:   float = 0.0
    take_profit: float = 0.0
    risk_pts:    float = 0.0
    reward_pts:  float = 0.0
    rr_ratio:    float = 0.0
    size:        float = 0.0    # taille validée par RiskManager
    setup_name:  str   = ""
    reason:      str   = ""

    @property
    def is_tradeable(self) -> bool:
        return self.direction != SignalDirection.FLAT

    def __repr__(self) -> str:
        if not self.is_tradeable:
            return f"TradeSetup[{self.setup_name}] FLAT - {self.reason}"
        return (
            f"TradeSetup[{self.setup_name}] {self.direction.value} "
            f"E={self.entry:.2f} SL={self.stop_loss:.2f} "
            f"TP={self.take_profit:.2f} R:R={self.rr_ratio:.1f} size={self.size:.2f}"
        )


@dataclass
class StrategySpec:
    """Specification complète d'un setup de trading."""
    name:        str = "UNNAMED_SETUP"
    description: str = ""
    instrument:  str = "DAX"
    timeframe:   str = "1MINUTE"

    sl_atr_mult:   float = 1.5
    tp_atr_mult:   float = 3.0
    min_rr:        float = 1.5
    hour_start:    int   = 9
    hour_end:      int   = 17
    max_spread:    float = 2.0
    min_atr:       float = 0.0

    rsi_long_min:  float = 45.0
    rsi_long_max:  float = 65.0
    rsi_short_min: float = 35.0
    rsi_short_max: float = 55.0

    kill_switch: bool = False

    _fn_signal_long:   Optional[Callable] = field(default=None, repr=False)
    _fn_signal_short:  Optional[Callable] = field(default=None, repr=False)
    _fn_market_filter: Optional[Callable] = field(default=None, repr=False)
    _fn_invalidation:  Optional[Callable] = field(default=None, repr=False)

    def evaluate(
        self,
        iset,
        hour: int,
        spread: float = 0.0,
        current_direction: Optional[str] = None,
    ) -> TradeSetup:
        flat = TradeSetup(direction=SignalDirection.FLAT, setup_name=self.name)

        if self.kill_switch:
            flat.reason = "KILL_SWITCH_ACTIF"
            return flat

        if iset is None or not iset.is_ready:
            flat.reason = "WARMUP_INDICATEURS"
            return flat

        if not self._check_market_filter(iset, hour, spread):
            flat.reason = "FILTRE_MARCHE"
            return flat

        if current_direction and self._check_invalidation(iset, current_direction):
            return TradeSetup(
                direction=SignalDirection.FLAT,
                setup_name=self.name,
                reason=f"INVALIDATION_{current_direction}",
            )

        if self._check_signal_long(iset):
            return self._build_trade(iset, SignalDirection.LONG)

        if self._check_signal_short(iset):
            return self._build_trade(iset, SignalDirection.SHORT)

        flat.reason = "PAS_DE_SIGNAL"
        return flat

    def _check_signal_long(self, iset) -> bool:
        if self._fn_signal_long:
            return self._fn_signal_long(iset)
        return (
            iset.ema_bullish and
            self.rsi_long_min <= iset.rsi <= self.rsi_long_max and
            iset.macd_bullish and
            iset.price_above_ema_fast
        )

    def _check_signal_short(self, iset) -> bool:
        if self._fn_signal_short:
            return self._fn_signal_short(iset)
        return (
            iset.ema_bearish and
            self.rsi_short_min <= iset.rsi <= self.rsi_short_max and
            iset.macd_bearish and
            not iset.price_above_ema_fast
        )

    def _check_market_filter(self, iset, hour: int, spread: float) -> bool:
        if self._fn_market_filter:
            return self._fn_market_filter(iset, hour, spread)
        return (
            self.hour_start <= hour < self.hour_end and
            spread <= self.max_spread and
            (self.min_atr == 0.0 or iset.atr >= self.min_atr)
        )

    def _check_invalidation(self, iset, direction: str) -> bool:
        if self._fn_invalidation:
            return self._fn_invalidation(iset, direction)
        if direction == "LONG":
            return iset.ema_bearish or iset.macd_bearish
        elif direction == "SHORT":
            return iset.ema_bullish or iset.macd_bullish
        return False

    def _build_trade(self, iset, direction: SignalDirection) -> TradeSetup:
        entry = iset.close
        atr   = iset.atr
        if math.isnan(atr) or atr <= 0:
            return TradeSetup(
                direction=SignalDirection.FLAT,
                setup_name=self.name,
                reason="ATR_INVALIDE",
            )
        sl_dist = atr * self.sl_atr_mult
        tp_dist = atr * self.tp_atr_mult
        if direction == SignalDirection.LONG:
            sl, tp = entry - sl_dist, entry + tp_dist
        else:
            sl, tp = entry + sl_dist, entry - tp_dist
        risk_pts   = abs(entry - sl)
        reward_pts = abs(tp - entry)
        rr = reward_pts / risk_pts if risk_pts > 0 else 0.0
        if rr < self.min_rr:
            return TradeSetup(
                direction=SignalDirection.FLAT,
                setup_name=self.name,
                reason=f"RR_INSUFFISANT_{rr:.1f}",
            )
        return TradeSetup(
            direction=direction, entry=entry, stop_loss=sl, take_profit=tp,
            risk_pts=risk_pts, reward_pts=reward_pts, rr_ratio=rr,
            setup_name=self.name, reason="SIGNAL_VALIDE",
        )


# -----------------------------------------------------------------------
# Factories
# -----------------------------------------------------------------------

def setup_a_ema_cross() -> StrategySpec:
    """SETUP_A : EMA Cross + RSI Neutre + MACD confirmé — DAX M1."""
    return StrategySpec(
        name        = "SETUP_A_EMA_Cross",
        description = "Tendance EMA20/50 + RSI neutre + MACD confirmé - DAX M1",
        instrument  = "DAX",
        timeframe   = "1MINUTE",
        sl_atr_mult = 1.5,
        tp_atr_mult = 3.0,
        min_rr      = 1.8,
        hour_start  = 9,
        hour_end    = 17,
        max_spread  = 2.0,
    )


def setup_b_kasper_video_1() -> StrategySpec:
    """
    SETUP_B : Template à compléter depuis la vidéo Kasper #1 — DAX M1.
    Note : l'implémentation XAUUSD Bv3 production est dans strategy_engine.py.
    Ce template sert de base pour les tests et les variantes DAX.
    """
    return StrategySpec(
        name        = "SETUP_B_KASPER_V1",
        description = "Template Kasper vidéo 1 — DAX M1. Voir kasper_setups.py.",
        instrument  = "DAX",       # ← DAX (template original, tests attendent DAX)
        timeframe   = "1MINUTE",
        sl_atr_mult = 1.5,
        tp_atr_mult = 3.0,
        min_rr      = 2.0,
        hour_start  = 9,
        hour_end    = 17,
        max_spread  = 2.0,
    )


def setup_c_kasper_video_2() -> StrategySpec:
    """SETUP_C : Order Blocks frais en tendance (Kasper vidéo 2)."""
    return StrategySpec(
        name        = "SETUP_C_KASPER_V2",
        description = "OB frais en tendance, session Londres/NY — voir kasper_setups.py",
        instrument  = "XAUUSD",
        timeframe   = "5MINUTE",
        sl_atr_mult = 1.5,
        tp_atr_mult = 3.0,
        min_rr      = 2.0,
        hour_start  = 8,
        hour_end    = 23,
        max_spread  = 3.0,
    )
