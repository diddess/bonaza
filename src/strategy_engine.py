"""
strategy_engine.py - Moteur Bv3 (multi-instruments)
=====================================================
Corrections post-review :
  - Kill switch .env effectivement appliqué au RiskManager (BONAZA_KILL_SWITCH=TRUE)
  - Taille de position (size) incluse dans le TradeSetup → plus de double-calcul
  - Filtre MA200 désactivé en DEMO (réactiver pour LIVE)
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Optional

import numpy as np
import talib
from loguru import logger
from scipy.signal import argrelextrema

from config import BonazaConfig
from data_feed import OHLCVCandle
from instruments import InstrumentConfig
from risk_manager import RiskManager, RiskConfig, KillSwitchReason
from strategy_spec import SignalDirection, TradeSetup

BV3_SL_MULT        = 1.0
BV3_TP_MULT        = 3.0
BV3_SESSION_START  = 16
BV3_SESSION_END    = 21
BV3_LONG_ONLY      = True
BV3_MIN_SPREAD_ATR = 1.5

BUFFER_SIZE        = 2600
BUFFER_MIN_WARMUP  = 2430


@dataclass
class Bv3State:
    last_long_bar:          int = 0
    last_short_bar:         int = 0
    bar_count:              int = 0
    signals_emitted:        int = 0
    signals_blocked_adx:    int = 0
    signals_blocked_oos:    int = 0
    signals_blocked_spread: int = 0

    def __post_init__(self):
        self.last_long_bar  = -100
        self.last_short_bar = -100


class StrategyEngine:

    def __init__(self, instrument: InstrumentConfig, risk_manager: RiskManager) -> None:
        self.instrument = instrument
        self.rm         = risk_manager
        self._buffer: Deque[OHLCVCandle] = deque(maxlen=BUFFER_SIZE)
        self._state = Bv3State()

    async def run(self, candle_queue: asyncio.Queue, signal_queue: asyncio.Queue) -> None:
        inst = self.instrument
        logger.info(f"[{inst.name}] StrategyEngine demarrage | {inst.tf} | {inst.mode}")
        logger.info(
            f"[{inst.name}] SL={inst.sl_mult}xATR TP={inst.tp_mult}xATR "
            f"ADX>={inst.adx_min} OTE [0.618-0.786] "
            f"Session {inst.session_start}h-{inst.session_end}h UTC | "
            f"MA200 filtre : DESACTIVE (DEMO)"
        )
        while True:
            candle: Optional[OHLCVCandle] = await candle_queue.get()
            if candle is None:
                logger.info(f"[{inst.name}] StrategyEngine arret")
                break
            setup = self._process_candle(candle)
            if setup is not None:
                await signal_queue.put(setup)
                logger.info(
                    f"[{inst.name}] Signal {setup.direction.value} | "
                    f"E={setup.entry:.2f} SL={setup.stop_loss:.2f} "
                    f"TP={setup.take_profit:.2f} R:R={setup.rr_ratio:.2f} "
                    f"size={setup.size:.2f} | {setup.reason}"
                )

    def _process_candle(self, candle: OHLCVCandle) -> Optional[TradeSetup]:
        self._buffer.append(candle)
        self._state.bar_count += 1

        hour = candle.timestamp.hour
        inst = self.instrument
        if not (inst.session_start <= hour < inst.session_end):
            self._state.signals_blocked_oos += 1
            return None

        if len(self._buffer) < BUFFER_MIN_WARMUP:
            return None

        return self._detect_bv3(candle)

    def _detect_bv3(self, latest: OHLCVCandle) -> Optional[TradeSetup]:
        inst = self.instrument
        buf  = list(self._buffer)
        n    = len(buf)
        i    = n - 1

        close = np.array([c.close for c in buf], dtype=np.float64)
        high  = np.array([c.high  for c in buf], dtype=np.float64)
        low   = np.array([c.low   for c in buf], dtype=np.float64)

        atr   = talib.ATR(high, low, close, 14)
        adx   = talib.ADX(high, low, close, 14)
        ma_h1 = talib.SMA(close, min(inst.ma_period, n - 1))

        atr_now = atr[-1]
        adx_now = adx[-1]
        ma_now  = ma_h1[-1]
        price   = close[-1]

        if np.isnan(atr_now) or np.isnan(adx_now) or np.isnan(ma_now):
            return None
        if adx_now < inst.adx_min:
            self._state.signals_blocked_adx += 1
            return None

        sh_idx = argrelextrema(high, np.greater, order=inst.swing_order)[0]
        sl_idx = argrelextrema(low,  np.less,    order=inst.swing_order)[0]
        conf_sh = sh_idx[sh_idx < i - inst.swing_order]
        conf_sl = sl_idx[sl_idx < i - inst.swing_order]

        if len(conf_sh) < 1 or len(conf_sl) < 1:
            return None

        # Swing le plus récent avec spread suffisant
        sh_v = sl_v = spread = None
        last_sh_i = last_sl_i = None
        for sh_i in reversed(conf_sh):
            for sl_i in reversed(conf_sl):
                s = high[sh_i] - low[sl_i]
                if s >= BV3_MIN_SPREAD_ATR * atr_now:
                    sh_v, sl_v, spread = high[sh_i], low[sl_i], s
                    last_sh_i, last_sl_i = sh_i, sl_i
                    break
            if spread is not None:
                break

        if spread is None:
            self._state.signals_blocked_spread += 1
            return None

        bars_since_long = self._state.bar_count - self._state.last_long_bar
        setup = None
        trend_info = "LONG" if price > ma_now else "SOUS_MA200"

        if last_sl_i < last_sh_i:
            ote_lo = sl_v + 0.618 * spread
            ote_hi = sl_v + 0.786 * spread
            if ote_lo <= price <= ote_hi:
                if bars_since_long >= inst.cooldown_bars:
                    sl_pts = inst.sl_mult * atr_now
                    tp_pts = inst.tp_mult * atr_now
                    sl     = round(price - sl_pts, 2)
                    tp     = round(price + tp_pts, 2)
                    rr     = round(tp_pts / sl_pts, 2)

                    try:
                        decision = self.rm.validate_signal(
                            direction  = "LONG",
                            entry      = price,
                            sl         = sl,
                            tp         = tp,
                            atr        = atr_now,
                            spread     = latest.ask_close - latest.bid_close,
                            setup_name = "SETUP_B_Bv3",
                            instrument = inst.name,
                        )
                    except Exception as e:
                        logger.debug(f"[{inst.name}] validate_signal exception : {e}")
                        decision = None

                    if decision and decision.approved:
                        self._state.last_long_bar = self._state.bar_count
                        self._state.signals_emitted += 1
                        setup = TradeSetup(
                            direction   = SignalDirection.LONG,
                            entry       = round(price, 2),
                            stop_loss   = sl,
                            take_profit = tp,
                            risk_pts    = round(sl_pts, 3),
                            reward_pts  = round(tp_pts, 3),
                            rr_ratio    = rr,
                            size        = decision.size,   # ← taille du RiskManager
                            setup_name  = f"SETUP_B_Bv3_{inst.name}",
                            reason      = (
                                f"OTE [{ote_lo:.2f}-{ote_hi:.2f}] "
                                f"ADX={adx_now:.1f} MA={ma_now:.2f}({trend_info}) "
                                f"SH={sh_v:.2f} SL={sl_v:.2f}"
                            ),
                        )
                    elif decision:
                        logger.debug(f"[{inst.name}] LONG rejete : {decision.reason}")
                    elif inst.mode == "DRY_RUN":
                        # En DRY_RUN sans RiskManager valide → size symbolique 0.1
                        self._state.last_long_bar = self._state.bar_count
                        self._state.signals_emitted += 1
                        setup = TradeSetup(
                            direction   = SignalDirection.LONG,
                            entry       = round(price, 2),
                            stop_loss   = sl,
                            take_profit = tp,
                            risk_pts    = round(sl_pts, 3),
                            reward_pts  = round(tp_pts, 3),
                            rr_ratio    = rr,
                            size        = 0.1,
                            setup_name  = f"SETUP_B_Bv3_{inst.name}",
                            reason      = (
                                f"OTE [{ote_lo:.2f}-{ote_hi:.2f}] "
                                f"ADX={adx_now:.1f} DRY_RUN"
                            ),
                        )

        return setup

    def get_status(self) -> dict:
        return {
            "instrument":             self.instrument.name,
            "mode":                   self.instrument.mode,
            "bar_count":              self._state.bar_count,
            "buffer_size":            len(self._buffer),
            "warmup_pct":             min(100, len(self._buffer) / BUFFER_MIN_WARMUP * 100),
            "signals_emitted":        self._state.signals_emitted,
            "signals_blocked_adx":    self._state.signals_blocked_adx,
            "signals_blocked_oos":    self._state.signals_blocked_oos,
            "signals_blocked_spread": self._state.signals_blocked_spread,
        }


def build_engine_for(
    instrument: InstrumentConfig,
    config:     BonazaConfig,
    capital:    float = 1745.0,
) -> tuple:
    # Granularite et plafond size depuis ig_rules + .env MAX_POSITION_SIZE
    size_step = 0.0
    try:
        from ig_rules import RULES as _IG_RULES
        r = _IG_RULES.get(instrument.name)
        if r:
            size_step = float(r.min_deal_size)
    except Exception:
        pass

    risk_cfg = RiskConfig(
        risk_pct         = float(config.trading.max_capital_pct),
        max_capital_pct  = float(config.trading.max_capital_pct),
        max_daily_dd_pct = float(config.trading.max_daily_dd_pct),
        min_rr           = instrument.tp_mult / instrument.sl_mult,
        is_live          = config.trading.is_live(),
        size_step        = size_step,
        max_position_size = float(config.trading.max_position_size or 0.0),
    )
    rm = RiskManager(config=risk_cfg, capital=capital)

    # FIX 1 : kill switch .env effectivement appliqué
    if config.trading.kill_switch:
        rm.activate_kill_switch(KillSwitchReason.EXTERNAL)
        logger.warning(
            f"[{instrument.name}] Kill switch activé depuis .env "
            f"(BONAZA_KILL_SWITCH=TRUE) — aucun trade ne sera exécuté"
        )

    engine = StrategyEngine(instrument=instrument, risk_manager=rm)
    return engine, rm


def build_engine(config: BonazaConfig, capital: float = 1745.0) -> tuple:
    """Compatibilité mono-instrument XAUUSD."""
    from instruments import INSTRUMENTS
    return build_engine_for(INSTRUMENTS["XAUUSD"], config, capital)
