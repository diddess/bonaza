"""portfolio_runner.py - Orchestrateur du portfolio de 3 strategies validees.
=================================================================================

Strategies actives (validees walk-forward + sensitivity sur 1 an XAUUSD/CAC40) :

  S8 regime_adaptive  : CAC40 M15, session 7h-16h UTC, SL=1.0xATR TP=2.0xATR
    LOGIQUE : ADX>25 + EMA9 cross EMA21 (trend follow)
              OU ADX<15 + close hors BB(20,2.0) + RSI extreme (mean-rev range)
    BACKTEST : Sharpe IS 1.36 / OOS 3.03, PF IS 1.49 / OOS 2.27
    Sensitivity : 25/25 parametres positifs

  S5 tod_momentum    : XAUUSD M15, sans session filter (24/5), SL=1.0xATR TP=3.0xATR
    LOGIQUE : Entry uniquement aux heures historiquement directionnelles
              - LONG si hour == 21h UTC + close > EMA20 + ADX > 18
              - SHORT si hour == 5h UTC  + close < EMA20 + ADX > 18
    BACKTEST : Sharpe IS 0.67 / OOS 2.17, PF 1.50 / OOS 1.91
    Note : OOS > IS = strategie qui s'ameliore avec le temps (regime favorable)

  S3 orb              : XAUUSD M15, session 13h-21h UTC, SL=1.0xATR TP=3.0xATR
    LOGIQUE : Opening Range = high/low des 60 premieres minutes (4 bougies M15)
              Entry sur breakout (close > ORH ou close < ORL) avec ADX > 20
              Un seul breakout par jour
    BACKTEST : Sharpe IS 1.34 / OOS 1.23, PF 1.27 / OOS 1.23 (le plus stable)

PORTFOLIO COMBINE (3 strats equal-weight) :
  Sharpe 2.10 / DD 0.22% / +1.41%/an a sizing 10% capital
  Correlations <0.10 entre strats (vraie diversification)
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, time as dtime
from typing import Deque, Optional, List, Dict

import numpy as np
import talib
from loguru import logger

from data_feed import OHLCVCandle
from risk_manager import RiskManager
from strategy_spec import SignalDirection, TradeSetup


# -----------------------------------------------------------------------
# Buffer M5 -> M15 et indicateurs
# -----------------------------------------------------------------------

@dataclass
class Bar:
    ts:     datetime
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float


def m5_to_m15(buf: Deque[OHLCVCandle]) -> List[Bar]:
    """Agrege M5 -> M15 (3 bougies M5 -> 1 bougie M15).
    On groupe par tranche de 15 min UTC (minute = 0, 15, 30, 45).
    Retourne uniquement les bougies M15 completes (au moins 3 M5 dedans).
    """
    if not buf:
        return []
    by15 = {}
    counts: Dict = {}
    for c in buf:
        # Tronquer la minute au multiple de 15
        ts = c.timestamp.replace(minute=(c.timestamp.minute // 15) * 15,
                                 second=0, microsecond=0)
        counts[ts] = counts.get(ts, 0) + 1
        if ts not in by15:
            by15[ts] = Bar(ts=ts, open=c.open, high=c.high, low=c.low,
                           close=c.close, volume=c.volume or 0)
        else:
            b = by15[ts]
            b.high = max(b.high, c.high)
            b.low = min(b.low, c.low)
            b.close = c.close
            b.volume += c.volume or 0
    keys = sorted(by15.keys())
    # Retirer le DERNIER slot s'il est incomplet (< 3 M5) : c'est une bougie M15
    # partielle (en cours de formation ou tronquee par le warmup) -> l'inclure
    # fausserait ADX/EMA/ATR et donc la direction du signal.
    if keys and counts[keys[-1]] < 3:
        keys = keys[:-1]
    return [by15[k] for k in keys]


def arrays_from_bars(bars: List[Bar]):
    o = np.array([b.open for b in bars], dtype=np.float64)
    h = np.array([b.high for b in bars], dtype=np.float64)
    l = np.array([b.low for b in bars], dtype=np.float64)
    c = np.array([b.close for b in bars], dtype=np.float64)
    return o, h, l, c


# -----------------------------------------------------------------------
# Strategies
# -----------------------------------------------------------------------

class StrategyBase:
    """Interface commune."""
    name: str = "BASE"
    instrument: str = "XAUUSD"
    tf_minutes: int = 15
    sl_mult: float = 1.0
    tp_mult: float = 2.0
    session_start: Optional[int] = None  # heure UTC inclusive, None = 24/7
    session_end: Optional[int] = None    # heure UTC exclusive
    min_bars: int = 60
    cooldown_bars: int = 12
    # Gestion active des sorties (prise de benefices = trailing stop ATR)
    trail_trigger_atr: float = 1.0   # active le trailing a +1xATR de profit
    trail_dist_atr:    float = 1.0   # trailing a 1xATR derriere l'extreme favorable
    # Quels declencheurs de sortie sont actifs (les breakout = SL/TP seuls, pour
    # coller au backtest ; pas de trailing ni de retournement).
    exit_trailing:  bool = True
    exit_reversal:  bool = True

    def __init__(self):
        # Cooldown base sur le TIMESTAMP (et non l'indice len(bars)) : l'indice
        # se fige quand le buffer circulaire atteint maxlen -> cooldown jamais
        # expire -> la strat cesse d'emettre apres ~8j d'uptime.
        self._last_signal_ts: Optional[datetime] = None

    def session_open(self, ts: datetime) -> bool:
        if self.session_start is None or self.session_end is None:
            return True
        h = ts.hour
        return self.session_start <= h < self.session_end

    def evaluate(self, bars: List[Bar]) -> Optional[TradeSetup]:
        """Retourne TradeSetup tradeable ou None."""
        if len(bars) < self.min_bars:
            return None
        now_ts = bars[-1].ts
        # cooldown temporel (robuste au buffer circulaire)
        if (self.cooldown_bars > 0 and self._last_signal_ts is not None and
                (now_ts - self._last_signal_ts)
                < timedelta(minutes=self.cooldown_bars * self.tf_minutes)):
            return None
        if not self.session_open(now_ts):
            return None
        sig = self._signal(bars)
        if sig is not None and sig.is_tradeable:
            self._last_signal_ts = now_ts
            return sig
        return None

    def _signal(self, bars: List[Bar]) -> Optional[TradeSetup]:
        raise NotImplementedError

    def _build(self, bars: List[Bar], direction: SignalDirection,
               atr: float, reason: str) -> TradeSetup:
        last = bars[-1]
        entry = last.close
        if direction == SignalDirection.LONG:
            sl = entry - self.sl_mult * atr
            tp = entry + self.tp_mult * atr
        else:
            sl = entry + self.sl_mult * atr
            tp = entry - self.tp_mult * atr
        risk_pts = abs(entry - sl)
        reward_pts = abs(tp - entry)
        rr = reward_pts / risk_pts if risk_pts > 0 else 0.0
        return TradeSetup(
            direction=direction, entry=entry, stop_loss=sl, take_profit=tp,
            risk_pts=risk_pts, reward_pts=reward_pts, rr_ratio=rr,
            setup_name=self.name, reason=reason,
        )

    # -------------------------------------------------------------------
    # Gestion ACTIVE des positions ouvertes (appelee par manage_positions)
    # -------------------------------------------------------------------
    def should_exit(self, pos, price: float, bars: List[Bar]) -> Optional[str]:
        """Decide s'il faut fermer la position 'pos' au prix live 'price'.
        Retourne un motif ("THEO_SL"/"THEO_TP"/"TRAIL_TP"/"REVERSAL") ou None.
        Mute pos.trail_peak / pos.trail_sl (trailing stop) au passage.
        pos est un OpenPosition : direction, entry_level, theo_sl, theo_tp, atr,
        trail_peak, trail_sl.
        """
        long = pos.direction == "LONG"
        # 1. SL theorique serre (le vrai SL, gere par le moteur)
        if pos.theo_sl:
            if long and price <= pos.theo_sl:      return "THEO_SL"
            if not long and price >= pos.theo_sl:  return "THEO_SL"
        # 2. TP theorique (objectif plein)
        if pos.theo_tp:
            if long and price >= pos.theo_tp:      return "THEO_TP"
            if not long and price <= pos.theo_tp:  return "THEO_TP"
        # 3. Prise de benefices : trailing stop active a +trail_trigger x ATR
        atr = pos.atr or 0.0
        if self.exit_trailing and atr > 0:
            fav = (price - pos.entry_level) if long else (pos.entry_level - price)
            # (a) ACTIVER / mettre a jour le trailing quand le profit >= trigger
            if fav >= self.trail_trigger_atr * atr:
                if long:
                    pos.trail_peak = max(pos.trail_peak or pos.entry_level, price)
                    pos.trail_sl = max(pos.trail_sl,
                                       pos.trail_peak - self.trail_dist_atr * atr)
                else:
                    pos.trail_peak = min(pos.trail_peak or pos.entry_level, price)
                    nt = pos.trail_peak + self.trail_dist_atr * atr
                    pos.trail_sl = nt if pos.trail_sl == 0 else min(pos.trail_sl, nt)
            # (b) VERIFIER le trailing stop s'il est actif (independamment de fav,
            #     sinon un repli sous le seuil ne declencherait jamais la sortie)
            if pos.trail_sl:
                if long and price <= pos.trail_sl:      return "TRAIL_TP"
                if not long and price >= pos.trail_sl:  return "TRAIL_TP"
        # 4. Retournement (sur bougies closes, specifique a la strategie)
        if self.exit_reversal:
            try:
                if self._reversal(pos, bars): return "REVERSAL"
            except Exception:
                pass
        return None

    def _reversal(self, pos, bars: List[Bar]) -> bool:
        """Detection de retournement specifique a la strategie. Defaut : aucun."""
        return False

    def _ema_cross_reversal(self, pos, bars: List[Bar], fast=9, slow=21) -> bool:
        """Retournement = croisement EMA fast/slow OPPOSE a la position (bougie close)."""
        if len(bars) < slow + 5:
            return False
        _, h, l, c = arrays_from_bars(bars)
        ef = talib.EMA(c, fast); es = talib.EMA(c, slow)
        i = len(c) - 1
        if any(np.isnan(x) for x in (ef[i], es[i], ef[i-1], es[i-1])):
            return False
        if pos.direction == "LONG":
            return ef[i] < es[i] and ef[i-1] >= es[i-1]   # cross baissier
        return ef[i] > es[i] and ef[i-1] <= es[i-1]       # cross haussier


class S8RegimeAdaptive(StrategyBase):
    """S8 : ADX>25 trend follow EMA9/21 cross  OR  ADX<15 mean-rev BB extreme."""
    name = "S8_RegimeAdaptive"
    instrument = "CAC40"
    tf_minutes = 15
    sl_mult = 1.0
    tp_mult = 2.0
    session_start = 7
    session_end = 16
    min_bars = 80
    cooldown_bars = 24

    def _signal(self, bars):
        _, h, l, c = arrays_from_bars(bars)
        adx = talib.ADX(h, l, c, 14)
        ema_f = talib.EMA(c, 9)
        ema_s = talib.EMA(c, 21)
        upper, _, lower = talib.BBANDS(c, 20, 2.0, 2.0)
        rsi = talib.RSI(c, 14)
        atr = talib.ATR(h, l, c, 14)
        i = len(c) - 1
        if any(np.isnan(x[i]) for x in (adx, ema_f, ema_s, upper, lower, rsi, atr)):
            return None
        # Trend regime
        if adx[i] > 25:
            if ema_f[i] > ema_s[i] and ema_f[i-1] <= ema_s[i-1]:
                return self._build(bars, SignalDirection.LONG, atr[i], "TREND_EMA_X_UP")
            if ema_f[i] < ema_s[i] and ema_f[i-1] >= ema_s[i-1]:
                return self._build(bars, SignalDirection.SHORT, atr[i], "TREND_EMA_X_DN")
        # Range regime
        if adx[i] < 15:
            if c[i] < lower[i] and rsi[i] < 30:
                return self._build(bars, SignalDirection.LONG, atr[i], "RANGE_BB_OVERSOLD")
            if c[i] > upper[i] and rsi[i] > 70:
                return self._build(bars, SignalDirection.SHORT, atr[i], "RANGE_BB_OVERBOUGHT")
        return None

    def _reversal(self, pos, bars):
        # Retournement S8 = croisement EMA9/21 oppose
        return self._ema_cross_reversal(pos, bars, 9, 21)


class S5TodMomentum(StrategyBase):
    """S5 : entry uniquement aux heures favorables identifiees historiquement."""
    name = "S5_ToDMomentum"
    instrument = "XAUUSD"
    tf_minutes = 15
    sl_mult = 1.0
    tp_mult = 3.0
    # Pas de session filter (24/5)
    session_start = None
    session_end = None
    min_bars = 60
    cooldown_bars = 60
    long_hours = (21,)
    short_hours = (5,)
    adx_min = 18.0

    def _signal(self, bars):
        last = bars[-1]
        h_utc = last.ts.hour
        if h_utc not in self.long_hours and h_utc not in self.short_hours:
            return None
        _, h, l, c = arrays_from_bars(bars)
        ema20 = talib.EMA(c, 20)
        adx = talib.ADX(h, l, c, 14)
        atr = talib.ATR(h, l, c, 14)
        i = len(c) - 1
        if any(np.isnan(x[i]) for x in (ema20, adx, atr)):
            return None
        if adx[i] < self.adx_min:
            return None
        if h_utc in self.long_hours and c[i] > ema20[i]:
            return self._build(bars, SignalDirection.LONG, atr[i],
                              f"TOD_LONG_{h_utc}UTC_ADX{adx[i]:.0f}")
        if h_utc in self.short_hours and c[i] < ema20[i]:
            return self._build(bars, SignalDirection.SHORT, atr[i],
                              f"TOD_SHORT_{h_utc}UTC_ADX{adx[i]:.0f}")
        return None

    def _reversal(self, pos, bars):
        # Retournement S5 = cloture repassee du mauvais cote de l'EMA20
        if len(bars) < 25:
            return False
        _, h, l, c = arrays_from_bars(bars)
        ema = talib.EMA(c, 20)
        i = len(c) - 1
        if np.isnan(ema[i]):
            return False
        return (c[i] < ema[i]) if pos.direction == "LONG" else (c[i] > ema[i])


class S3ORB(StrategyBase):
    """S3 : Opening Range Breakout sur les 4 premieres bougies M15 de la session.

    2026-05-30 : ajout filtre volume confirme (volume bougie cassure > MA20 * 1.5).
    Validation walk-forward 70/30 : OOS Sharpe 2.59 vs baseline 1.23 (+110%).
    Quarterly : 3/3 trimestres positifs.
    Le filtre volume reduit les faux breakouts en exigeant une confirmation d'activite.
    """
    name = "S3_ORB"
    instrument = "XAUUSD"
    tf_minutes = 15
    sl_mult = 1.0
    tp_mult = 3.0
    session_start = 13
    session_end = 21
    min_bars = 60
    cooldown_bars = 0  # gere par "un seul breakout/jour" via _day_done

    or_bars = 4              # 60 min de range
    adx_min = 20.0
    # Filtre volume sur la bougie cassure : volume > MA(vol, vol_period) * vol_threshold
    use_volume_filter = True
    vol_period = 20
    vol_threshold = 1.5

    def __init__(self):
        super().__init__()
        self._day_done: Dict = {}     # date -> bool

    def _signal(self, bars):
        last = bars[-1]
        day = last.ts.date()
        if self._day_done.get(day):
            return None
        _, h_arr, l_arr, c_arr = arrays_from_bars(bars)
        # Volume array
        v_arr = np.array([b.volume for b in bars], dtype=np.float64)
        in_day = [i for i, b in enumerate(bars)
                  if b.ts.date() == day and self.session_open(b.ts)]
        if len(in_day) <= self.or_bars:
            return None
        or_idx = in_day[:self.or_bars]
        orh = h_arr[or_idx].max()
        orl = l_arr[or_idx].min()
        adx = talib.ADX(h_arr, l_arr, c_arr, 14)
        atr = talib.ATR(h_arr, l_arr, c_arr, 14)
        i = len(c_arr) - 1
        if any(np.isnan(x[i]) for x in (adx, atr)):
            return None
        if adx[i] < self.adx_min:
            return None
        # Filtre volume confirme (anti faux-breakout)
        if self.use_volume_filter:
            if i < self.vol_period:
                return None
            vol_ma = v_arr[i - self.vol_period:i].mean()
            if vol_ma <= 0:
                # Volume non exploitable (ex CAC40 Dukascopy) -> fallback range/ATR proxy
                range_curr = h_arr[i] - l_arr[i]
                range_ma = np.mean([h_arr[j] - l_arr[j] for j in range(max(0, i - self.vol_period), i)])
                if range_curr < range_ma * 1.3:
                    return None
            elif v_arr[i] < vol_ma * self.vol_threshold:
                return None
        # Breakout
        if c_arr[i] > orh:
            self._day_done[day] = True
            return self._build(bars, SignalDirection.LONG, atr[i],
                              f"ORB_LONG_orh{orh:.2f}_ADX{adx[i]:.0f}_volOK")
        if c_arr[i] < orl:
            self._day_done[day] = True
            return self._build(bars, SignalDirection.SHORT, atr[i],
                              f"ORB_SHORT_orl{orl:.2f}_ADX{adx[i]:.0f}_volOK")
        return None

    def _reversal(self, pos, bars):
        # Retournement S3 = breakout rate : cloture revenue DANS le range d'ouverture
        day = bars[-1].ts.date()
        in_day = [b for b in bars if b.ts.date() == day and self.session_open(b.ts)]
        if len(in_day) <= self.or_bars:
            return False
        orh = max(b.high for b in in_day[:self.or_bars])
        orl = min(b.low  for b in in_day[:self.or_bars])
        c = bars[-1].close
        return orl < c < orh


class S8VolumeTrendDAX(StrategyBase):
    """S8 sur DAX M5 + filtre volume confirme.
    Validation walk-forward : IS Sharpe 1.58 / OOS 3.27 / PF 2.43 - ROBUSTE.
    Quarterly 2025Q2/2026Q1/2026Q2 : Sh 4.35 / 1.02 / 4.41 - stable.

    Specificite vs S8 baseline : volume confirme requiere en mode trend.
    Sur DAX M5, ce filtre transforme une strat perdante (baseline Sharpe -0.23)
    en strat solide (Sharpe +2.09).
    """
    name = "S8_VolumeTrend_DAX"
    instrument = "DAX"
    tf_minutes = 5
    sl_mult = 1.0
    tp_mult = 2.0
    session_start = 7
    session_end = 16
    min_bars = 80
    cooldown_bars = 24
    vol_period = 20
    vol_threshold = 1.3

    def _signal(self, bars):
        _, h, l, c = arrays_from_bars(bars)
        v = np.array([b.volume for b in bars], dtype=np.float64)
        adx = talib.ADX(h, l, c, 14)
        ema_f = talib.EMA(c, 9)
        ema_s = talib.EMA(c, 21)
        upper, _, lower = talib.BBANDS(c, 20, 2.0, 2.0)
        rsi = talib.RSI(c, 14)
        atr = talib.ATR(h, l, c, 14)
        i = len(c) - 1
        if any(np.isnan(x[i]) for x in (adx, ema_f, ema_s, upper, lower, rsi, atr)):
            return None
        # Volume confirme requis en mode trend
        if i >= self.vol_period:
            vol_ma = v[i - self.vol_period:i].mean()
            # Volume OBLIGATOIRE en mode trend : si volume mort (vol_ma<=0, ex IG
            # sans LTV), on REFUSE le trade trend. Autoriser (ancien comportement)
            # faisait retomber la strat dans son regime perdant sans filtre.
            vol_ok = vol_ma > 0 and v[i] > vol_ma * self.vol_threshold
        else:
            vol_ok = False
        # Trend regime + volume
        if adx[i] > 25 and vol_ok:
            if ema_f[i] > ema_s[i] and ema_f[i-1] <= ema_s[i-1]:
                return self._build(bars, SignalDirection.LONG, atr[i],
                                  f"TREND_EMA_X_UP_volOK_ADX{adx[i]:.0f}")
            if ema_f[i] < ema_s[i] and ema_f[i-1] >= ema_s[i-1]:
                return self._build(bars, SignalDirection.SHORT, atr[i],
                                  f"TREND_EMA_X_DN_volOK_ADX{adx[i]:.0f}")
        # Range regime (sans filtre volume)
        if adx[i] < 15:
            if c[i] < lower[i] and rsi[i] < 30:
                return self._build(bars, SignalDirection.LONG, atr[i],
                                  f"RANGE_BB_OVERSOLD_ADX{adx[i]:.0f}")
            if c[i] > upper[i] and rsi[i] > 70:
                return self._build(bars, SignalDirection.SHORT, atr[i],
                                  f"RANGE_BB_OVERBOUGHT_ADX{adx[i]:.0f}")
        return None

    def _reversal(self, pos, bars):
        # Retournement S8 DAX = croisement EMA9/21 oppose
        return self._ema_cross_reversal(pos, bars, 9, 21)


class BreakoutStrategy(StrategyBase):
    """Entree breakout au NIVEAU Donchian + filtre tendance EMA200.
    N'entre PAS au close (_signal=None) : l'entree se fait au franchissement du
    niveau via PortfolioRunner.monitor_breakouts (prix LIVE). Exit SL/TP seuls.
    Backtest walk-forward OOS robuste (CAC M5 Sharpe 3.1 / XAU M15 1.2).
    L'entree au close DETRUIT l'edge (teste) -> entree au niveau obligatoire."""
    sl_mult = 2.0
    tp_mult = 4.0            # R:R 2
    exit_trailing = False    # SL/TP seuls (colle au backtest)
    exit_reversal = False

    def __init__(self, name, instrument, tf_minutes, donchian_n,
                 session_start, session_end, cooldown_bars):
        super().__init__()
        self.name = name
        self.instrument = instrument
        self.tf_minutes = tf_minutes
        self.donchian_n = donchian_n
        self.session_start = session_start
        self.session_end = session_end
        self.cooldown_bars = cooldown_bars
        self.min_bars = max(donchian_n + 15, 210)
        self._last_entry_ts = None
        self._last_price = None   # prix du tick precedent (detection cassure FRAICHE)

    def _signal(self, bars):
        return None  # pas d'entree au close ; entree via check_entry (prix live)

    def check_entry(self, bars, price, prev):
        """Au prix LIVE : TradeSetup si le prix FRANCHIT (cassure fraiche) le niveau
        Donchian dans le sens EMA200. 'prev' = prix du tick precedent.
        Cassure fraiche = prev sous le niveau, price au-dessus (et inversement).
        Sans ca, on entrerait des que price EST au-dessus (etat permanent) -> entree
        au redemarrage + sur-trade vs backtest."""
        if not bars or len(bars) < self.min_bars or not price or price <= 0:
            return None
        if prev is None:
            return None   # 1er tick (post-restart) : pas d'entree, on memorise juste
        now_ts = datetime.now(tz=timezone.utc)
        if not self.session_open(now_ts):
            return None
        if (self._last_entry_ts is not None and
                (now_ts - self._last_entry_ts) <
                timedelta(minutes=self.cooldown_bars * self.tf_minutes)):
            return None
        _, h, l, c = arrays_from_bars(bars)
        ema = talib.EMA(c, 200); atr = talib.ATR(h, l, c, 14)
        i = len(c) - 1
        if any(np.isnan(x) for x in (ema[i], atr[i])) or atr[i] <= 0:
            return None
        a = atr[i]
        ph = h[max(0, i - self.donchian_n + 1):i + 1].max()
        pl = l[max(0, i - self.donchian_n + 1):i + 1].min()
        d = None
        if price > ph and prev <= ph and price > ema[i]:      # cassure HAUSSIERE fraiche
            d = SignalDirection.LONG; lvl = ph
        elif price < pl and prev >= pl and price < ema[i]:    # cassure BAISSIERE fraiche
            d = SignalDirection.SHORT; lvl = pl
        if d is None:
            return None
        entry = price
        if d == SignalDirection.LONG:
            sl = entry - self.sl_mult * a; tp = entry + self.tp_mult * a
        else:
            sl = entry + self.sl_mult * a; tp = entry - self.tp_mult * a
        risk = abs(entry - sl); rew = abs(tp - entry)
        return TradeSetup(
            direction=d, entry=entry, stop_loss=sl, take_profit=tp,
            risk_pts=risk, reward_pts=rew, rr_ratio=(rew / risk if risk > 0 else 0),
            setup_name=self.name,
            reason=f"BREAKOUT_{'UP' if d==SignalDirection.LONG else 'DN'}_lvl{lvl:.1f}_ATR{a:.1f}",
        )

    def mark_entered(self, ts=None):
        self._last_entry_ts = ts or datetime.now(tz=timezone.utc)


# -----------------------------------------------------------------------
# Runner orchestrateur
# -----------------------------------------------------------------------

class PortfolioRunner:
    """Orchestre les 3 strategies validees.

    Recoit les bougies M5 via add_candle (depuis dispatcher_task de main.py).
    Maintient un buffer separe par strategie (a son instrument).
    A chaque nouvelle bougie M15 complete (close), evalue les strategies
    concernees et push le TradeSetup dans signal_queue si valide.
    """
    BUFFER_MAX = 2400  # 24h x 60min / 15min x N jours

    def __init__(self, risk_managers: Dict[str, RiskManager],
                 instrument_filter: Optional[List[str]] = None):
        """
        risk_managers : dict instrument -> RiskManager
        instrument_filter : si fourni, ne considere que ces instruments
                            (default = active toutes les strategies)
        """
        self._rm = risk_managers
        self._buffers: Dict[str, Deque[OHLCVCandle]] = {
            "XAUUSD": deque(maxlen=self.BUFFER_MAX),
            "CAC40":  deque(maxlen=self.BUFFER_MAX),
            "DAX":    deque(maxlen=self.BUFFER_MAX),
        }
        # Tracking de la derniere bougie evaluee par (instrument, tf_minutes)
        self._last_close_ts: Dict[tuple, Optional[datetime]] = {}
        # 2026-06-02 : REMPLACE S5/S3/S8 (edge non reproductible) par 2 breakout
        # valides OOS. Entree au NIVEAU via monitor_breakouts (prix live), pas au
        # close. DAX off (drawdown trop gros pour le capital). Backtest replace>add.
        self._strategies: Dict[str, List[StrategyBase]] = {
            # 06/06 : LIVE = COPIEUR TELEGRAM SEUL. Les 2 breakouts sont en pause
            # (ils perdaient en reel). Decommenter pour les reactiver.
            # "XAUUSD": [BreakoutStrategy("BRK_XAU_M15", "XAUUSD", 15, 20, 13, 21, 6)],
            # "CAC40":  [BreakoutStrategy("BRK_CAC_M5",  "CAC40",   5, 30,  7, 16, 12)],
        }
        if instrument_filter:
            self._strategies = {k: v for k, v in self._strategies.items()
                               if k in instrument_filter}
        # Scalper 10s CAC40 (NON backtest, LIVE direct, max 3 trades/j, filet IG).
        # Gere entierement par scalper_loop (PAS dans _strategies -> manage_positions
        # l'ignore). session jusqu'a 16h UTC (18h Paris).
        self._scalp = {
            "enabled": False,                          # 02/06 : ARRETE jusqu'a nouvel ordre
            "name": "SCALP_CAC", "instrument": "CAC40",
            "session_start": 7, "session_end": 16,   # UTC ; 16h UTC = 18h Paris
            "max_trades_day": 30, "sl_pts": 20.0,     # 0.5lot x 20 = 10 EUR < plafond RM 12.14
            "consec": 3,                               # 3 bougies 10s consecutives
        }
        self._signals_emitted = 0
        self._signals_blocked_rm = 0
        self._signals_total = 0

    def add_candle(self, candle: OHLCVCandle) -> None:
        """Appele par dispatcher_task pour chaque bougie M5 recue."""
        # Identifier l'instrument depuis l'epic
        from instruments import INSTRUMENTS
        inst_name = None
        for name, inst in INSTRUMENTS.items():
            if inst.epic == candle.epic:
                inst_name = name
                break
        if inst_name is None or inst_name not in self._buffers:
            return
        self._buffers[inst_name].append(candle)

    async def run(self, signal_queues) -> None:
        """Boucle principale : evalue les strategies a chaque cloture M15.
        Cadence : check toutes les 30s.

        Args:
            signal_queues : soit une asyncio.Queue unique (legacy), soit
                            un Dict[instrument_name, asyncio.Queue] (recommande)
                            pour router les signaux vers la bonne queue.
        """
        logger.info(f"[Portfolio] Runner demarre | strats : "
                    f"{ {k: [s.name for s in v] for k, v in self._strategies.items()} }")
        while True:
            try:
                await self._tick(signal_queues)
            except Exception as e:
                logger.error(f"[Portfolio] erreur tick : {e}")
            await asyncio.sleep(30)

    async def _tick(self, signal_queues, now: Optional[datetime] = None) -> None:
        if now is None:
            now = datetime.now(tz=timezone.utc)
        is_dict = isinstance(signal_queues, dict)
        for inst_name, strats in self._strategies.items():
            buf = self._buffers[inst_name]
            if len(buf) < 60:
                continue
            # Cache par tf_minutes pour eviter de re-resampler
            bars_by_tf: Dict[int, List[Bar]] = {}

            for strat in strats:
                tf_min = strat.tf_minutes
                # Construire bougies du bon TF
                if tf_min == 5:
                    if 5 not in bars_by_tf:
                        # Bougies M5 directes depuis le buffer (deja en M5)
                        bars_by_tf[5] = [
                            Bar(ts=c.timestamp, open=c.open, high=c.high,
                                low=c.low, close=c.close, volume=c.volume or 0)
                            for c in buf
                        ]
                    bars_tf = bars_by_tf[5]
                elif tf_min == 15:
                    if 15 not in bars_by_tf:
                        bars_by_tf[15] = m5_to_m15(buf)
                    bars_tf = bars_by_tf[15]
                else:
                    logger.warning(f"[Portfolio] tf_minutes {tf_min} non supporte pour {strat.name}")
                    continue

                if not bars_tf:
                    continue
                # Le buffer M5 ne contient que des bougies CLOSES (le feed ne
                # yield que is_complete), et m5_to_m15 retire deja le slot M15
                # partiel -> bars_tf = uniquement des bougies closes. On evalue
                # donc directement la derniere close (deterministe, sans dependre
                # de datetime.now()). Le dedup _last_close_ts evite de re-evaluer
                # la meme bougie a chaque tick de 30s.
                bars_for_eval = bars_tf

                key = (inst_name, tf_min, strat.name)
                last_close_ts = bars_for_eval[-1].ts
                if self._last_close_ts.get(key) == last_close_ts:
                    continue
                self._last_close_ts[key] = last_close_ts

                try:
                    setup = strat.evaluate(bars_for_eval)
                    if setup and setup.is_tradeable:
                        self._signals_total += 1
                        rm = self._rm.get(strat.instrument)
                        if rm is None:
                            logger.warning(f"[Portfolio] {strat.name}: pas de RM pour {strat.instrument}")
                            continue
                        decision = rm.validate_signal(
                            direction       = setup.direction.value,
                            entry           = setup.entry,
                            sl              = setup.stop_loss,
                            tp              = setup.take_profit,
                            atr             = 0.0,
                            spread          = 0.0,
                            setup_name      = setup.setup_name,
                            instrument      = strat.instrument,
                            min_rr_override = 1.0,
                        )
                        if not decision.approved:
                            self._signals_blocked_rm += 1
                            logger.info(f"[Portfolio] {strat.name} RM rejette: {decision.reason}")
                            continue
                        # Propager la taille calculee par le RM, sinon l'ordre part
                        # avec size=0 (TradeSetup.size defaut) -> sizing non controle.
                        setup.size = decision.size
                        self._signals_emitted += 1
                        logger.info(f"[Portfolio] Signal emis : {setup}")
                        target_q = (signal_queues.get(strat.instrument)
                                    if is_dict else signal_queues)
                        if target_q is None:
                            logger.warning(f"[Portfolio] pas de queue pour {strat.instrument}")
                            continue
                        await target_q.put(setup)
                except Exception as e:
                    logger.error(f"[Portfolio] {strat.name} erreur eval : {e}")

    def get_status(self) -> dict:
        return {
            "strategies": {k: [s.name for s in v] for k, v in self._strategies.items()},
            "buffer_sizes": {k: len(v) for k, v in self._buffers.items()},
            "signals_total": self._signals_total,
            "signals_emitted": self._signals_emitted,
            "signals_blocked_rm": self._signals_blocked_rm,
            "last_close": {f"{k[0]}_M{k[1]}_{k[2]}": str(v) if v else None
                          for k, v in self._last_close_ts.items()},
        }

    # -------------------------------------------------------------------
    # Gestion ACTIVE des positions ouvertes (decision de fermeture moteur)
    # -------------------------------------------------------------------
    def _strat_by_name(self, name: str):
        for strats in self._strategies.values():
            for s in strats:
                if s.name == name:
                    return s
        return None

    def _bars_for(self, instrument: str, tf_minutes: int, cache: dict):
        key = (instrument, tf_minutes)
        if key in cache:
            return cache[key]
        buf = self._buffers.get(instrument)
        if not buf:
            bars = []
        elif tf_minutes == 15:
            bars = m5_to_m15(buf)
        else:
            bars = [Bar(ts=c.timestamp, open=c.open, high=c.high,
                        low=c.low, close=c.close, volume=c.volume or 0)
                    for c in buf]
        cache[key] = bars
        return bars

    async def manage_positions(self, executor, feed) -> None:
        """Boucle de gestion active : pour chaque position ouverte, la strategie
        proprietaire decide de la fermer (SL theorique / TP / trailing / retournement)
        a partir du prix LIVE. Cadence 2s pour la reactivite intra-bougie.
        Le filet IG (SL large) reste en place comme securite anti-crash."""
        logger.info("[Portfolio] Gestion active des positions demarree (tick 2s)")
        while True:
            try:
                await self._manage_tick(executor, feed)
            except Exception as e:
                logger.error(f"[Portfolio] manage erreur : {e}")
            await asyncio.sleep(2)

    async def _manage_tick(self, executor, feed) -> None:
        positions = executor.open_positions()
        if not positions:
            return
        bars_cache: Dict = {}
        for pos in positions:
            # Positions non gerees (ouvertes par l'ancien code, sans theo_sl) : on
            # laisse le filet IG SL/TP s'en occuper.
            if not getattr(pos, "setup_name", "") or not getattr(pos, "theo_sl", 0):
                continue
            strat = self._strat_by_name(pos.setup_name)
            if strat is None:
                continue
            price = feed.get_price(pos.epic) if feed else None
            if price is None or price <= 0:
                continue
            bars = self._bars_for(pos.instrument, strat.tf_minutes, bars_cache)
            reason = strat.should_exit(pos, price, bars)
            if reason:
                logger.info(f"[Portfolio] SORTIE {pos.setup_name} {pos.instrument} "
                            f"{pos.direction} -> {reason} @ {price:.2f} "
                            f"(theo_sl={pos.theo_sl} theo_tp={pos.theo_tp})")
                await executor.close_position(pos.deal_id, reason)

    # -------------------------------------------------------------------
    # Entree breakout au NIVEAU (prix live) — remplace l'entree au close
    # -------------------------------------------------------------------
    async def monitor_breakouts(self, executor, feed, signal_queues) -> None:
        from instruments import INSTRUMENTS
        logger.info("[Portfolio] Monitor breakout demarre (entree au niveau, tick 2s)")
        while True:
            try:
                bars_cache: Dict = {}
                open_setups = {p.setup_name for p in executor.open_positions()}
                for inst, strats in self._strategies.items():
                    inst_obj = INSTRUMENTS.get(inst)
                    if inst_obj is None:
                        continue
                    price = feed.get_price(inst_obj.epic) if feed else None
                    if not price or price <= 0:
                        continue
                    for strat in strats:
                        if not isinstance(strat, BreakoutStrategy):
                            continue
                        # MAJ du prix precedent a CHAQUE tick (meme position ouverte)
                        # pour une detection de cassure fraiche fiable.
                        prev = strat._last_price
                        strat._last_price = price
                        if strat.name in open_setups:
                            continue  # 1 position par strat
                        bars = self._bars_for(inst, strat.tf_minutes, bars_cache)
                        setup = strat.check_entry(bars, price, prev)
                        if setup is None:
                            continue
                        rm = self._rm.get(inst)
                        if rm is None:
                            continue
                        dec = rm.validate_signal(
                            direction=setup.direction.value, entry=setup.entry,
                            sl=setup.stop_loss, tp=setup.take_profit, atr=0.0,
                            spread=0.0, setup_name=setup.setup_name,
                            instrument=inst, min_rr_override=1.0)
                        strat.mark_entered()           # cooldown (evite le spam 2s)
                        if not dec.approved:
                            logger.info(f"[Portfolio] breakout {strat.name} RM rejette: {dec.reason}")
                            continue
                        setup.size = dec.size
                        self._signals_emitted += 1
                        logger.info(f"[Portfolio] BREAKOUT entree {strat.name} @ {price:.2f} : {setup}")
                        q = (signal_queues.get(inst) if isinstance(signal_queues, dict)
                             else signal_queues)
                        if q is not None:
                            await q.put(setup)
            except Exception as e:
                logger.error(f"[Portfolio] monitor_breakouts erreur : {e}")
            await asyncio.sleep(2)

    # -------------------------------------------------------------------
    # Scalper 10 secondes (CAC40) — NON backteste, gere entierement ici
    # -------------------------------------------------------------------
    async def scalper_loop(self, executor, feed, signal_queues) -> None:
        from instruments import INSTRUMENTS
        cfg = self._scalp; inst = cfg["instrument"]
        inst_obj = INSTRUMENTS.get(inst)
        # IMPORTANT : ne JAMAIS 'return' ici -> main.py fait asyncio.wait(FIRST_COMPLETED),
        # une tache qui se termine declenche l'arret du process (crash-loop Docker).
        # Tache desactivee = veille infinie.
        if not cfg.get("enabled", True) or inst_obj is None:
            reason = "DESACTIVE (enabled=False)" if not cfg.get("enabled", True) else "instrument inconnu"
            logger.info(f"[Scalp] Scalper {reason} — en veille (aucune entree)")
            while True:
                await asyncio.sleep(3600)
        epic = inst_obj.epic; consec = cfg["consec"]
        logger.info(f"[Scalp] Scalper 10s sur {inst} | max {cfg['max_trades_day']}/j | "
                    f"session {cfg['session_start']}-{cfg['session_end']}h UTC | filet {cfg['sl_pts']}pts")
        prev = None; up = 0; dn = 0; trades_day = 0; cur_day = None
        while True:
            await asyncio.sleep(10)
            try:
                price = feed.get_price(epic) if feed else None
                now = datetime.now(tz=timezone.utc)
                if now.date() != cur_day:
                    cur_day = now.date(); trades_day = 0
                if not price or price <= 0:
                    prev = None; up = dn = 0; continue
                if prev is None:
                    prev = price; continue
                d = 1 if price > prev else (-1 if price < prev else 0)
                ba = feed.get_bid_ask(epic) if feed else None
                prev = price
                if d == 1: up += 1; dn = 0
                elif d == -1: dn += 1; up = 0
                mine = [p for p in executor.open_positions() if p.setup_name == cfg["name"]]
                if mine:
                    pos = mine[0]
                    adverse = ((price - pos.entry_level) if pos.direction == "SHORT"
                               else (pos.entry_level - price))
                    if adverse >= cfg["sl_pts"]:
                        logger.info(f"[Scalp] SORTIE hard-stop {cfg['sl_pts']}pts @ {price:.2f}")
                        await executor.close_position(pos.deal_id, "SCALP_SL")
                    elif (pos.direction == "LONG" and dn >= consec) or \
                         (pos.direction == "SHORT" and up >= consec):
                        logger.info(f"[Scalp] SORTIE {pos.direction} (3 signaux inverses) @ {price:.2f}")
                        await executor.close_position(pos.deal_id, "SCALP_REVERSAL")
                    continue
                # --- entree ---
                if trades_day >= cfg["max_trades_day"]:
                    continue
                if not (cfg["session_start"] <= now.hour < cfg["session_end"]):
                    continue
                # entree au moment EXACT ou le compteur atteint 3 (1 tentative par
                # serie -> evite le spam si rejet ; apres entree, le check 'mine'
                # bloque toute reentree)
                sd = (SignalDirection.LONG if up == consec else
                      SignalDirection.SHORT if dn == consec else None)
                if sd is None:
                    continue
                slp = cfg["sl_pts"]; entry = price
                if sd == SignalDirection.LONG:
                    sl = entry - slp; tp = entry + slp * 3
                else:
                    sl = entry + slp; tp = entry - slp * 3
                rm = self._rm.get(inst)
                if rm is None:
                    continue
                dec = rm.validate_signal(direction=sd.value, entry=entry, sl=sl, tp=tp,
                                         atr=0.0, spread=0.0, setup_name=cfg["name"],
                                         instrument=inst, min_rr_override=1.0)
                if not dec.approved:
                    logger.info(f"[Scalp] RM rejette: {dec.reason}"); continue
                setup = TradeSetup(direction=sd, entry=entry, stop_loss=sl, take_profit=tp,
                                   risk_pts=slp, reward_pts=slp * 3, rr_ratio=3.0,
                                   size=dec.size, setup_name=cfg["name"],
                                   reason=f"SCALP_3x10s_{sd.value}")
                trades_day += 1
                logger.info(f"[Scalp] ENTREE {sd.value} #{trades_day}/{cfg['max_trades_day']} "
                            f"@ {price:.2f} (spread={ba})")
                q = (signal_queues.get(inst) if isinstance(signal_queues, dict) else signal_queues)
                if q is not None:
                    await q.put(setup)
            except Exception as e:
                logger.error(f"[Scalp] erreur : {e}")


def build_portfolio_runner(config, risk_managers, instrument_filter=None) -> PortfolioRunner:
    """Factory pour main.py."""
    return PortfolioRunner(risk_managers=risk_managers,
                          instrument_filter=instrument_filter)
