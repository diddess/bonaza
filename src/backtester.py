"""
backtester.py - M07 : Backtesting Bonaza avec vectorbt
=======================================================
Generateurs de signaux :
  B    : SETUP_B proxy V1 (reference)
  Bv2  : SETUP_B V2 (ADX + ATR + cooldown)
  Bv3  : SETUP_B V3 (OTE Fibonacci - recommande)
  ALL / ALLv2 / ALLv3 / ALLv4
  Bv4  : SETUP_B V4 (Kerner + Fraicheur OTE + TP rapide - analogie embouteillage)
  C    : SETUP_C V1 (reference)
  Cv2  : SETUP_C V2 (filtres qualite)

Note technique vbt 1.0.0 :
  SizeType.Percent ne supporte pas la reversal de position.
  Fix : deux portfolios separes (LONG-only + SHORT-only) combines via
  _combine_long_short() qui calcule les stats sur l'equity combinee.

POINT DE RETOUR GARANTI (Bv3) :
  python src/backtester.py --setup Bv3 --instrument XAUUSD --tf M5 --session --sl 1.0 --tp 3.0
  Sharpe 0.65 / Max DD 0.62% / Win 36.1% / PF 1.15 / trades 330

OBJECTIF Bv4 (analogie embouteillage) :
  python src/backtester.py --setup Bv4 --instrument XAUUSD --tf M5 --session
  Cible : PF > 1.20 / Sharpe > 0.65 avec moins de trades mais de meilleure qualite.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import argrelextrema
import pandas as pd
import talib
import vectorbt as vbt
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent))

BACKTEST_DIR = Path(__file__).parent.parent / "data" / "backtest"
BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR = Path(__file__).parent.parent / "data" / "historical"

VALIDATION = {
    "sharpe_min":        1.0,
    "sortino_min":       1.0,
    "max_dd_pct_max":   15.0,
    "min_trades":       200,
    "win_rate_min":     35.0,
    "profit_factor_min": 1.2,
}

FEES = {
    "XAUUSD": 0.0003,
    "DAX":    0.0002,
    "CAC40":  0.0002,
    "DEFAULT": 0.0003,
}

SESSION_FILTERS = {
    "XAUUSD": (13, 21),
    "DAX":    (7,  16),
    "CAC40":  (7,  16),
}

POSITION_SIZE     = 0.10
POSITION_SIZETYPE = "percent"


@dataclass
class BacktestMetrics:
    setup_name:   str
    instrument:   str
    tf:           str
    period_start: str
    period_end:   str
    run_at:       str
    total_return_pct:  float
    sharpe_ratio:      float
    sortino_ratio:     float
    calmar_ratio:      float
    omega_ratio:       float
    profit_factor:     float
    expectancy:        float
    max_drawdown_pct:  float
    max_dd_duration:   str
    total_trades:      int
    win_rate_pct:      float
    avg_win_pct:       float
    avg_loss_pct:      float
    best_trade_pct:    float
    worst_trade_pct:   float
    avg_win_duration:  str
    avg_loss_duration: str
    validated:         bool
    validation_issues: List[str]
    sl_atr_mult:      float
    tp_atr_mult:      float
    rr_ratio:         float

    def to_dict(self) -> dict:
        return asdict(self)

    def print_report(self) -> None:
        status = "VALIDE" if self.validated else "ECHEC VALIDATION"
        bar = "=" * 55
        print(f"\n{bar}")
        print(f"  BACKTEST {self.setup_name} | {self.instrument} {self.tf}")
        print(f"  {self.period_start} -> {self.period_end}")
        print(f"  Statut : {status}")
        print(bar)
        print(f"\n  Performance (sizing : 10% capital/trade)")
        print(f"    Return total  : {self.total_return_pct:+.2f} %")
        print(f"    Sharpe Ratio  : {self.sharpe_ratio:.3f}  "
              f"{'OK' if self.sharpe_ratio >= VALIDATION['sharpe_min'] else 'KO (< '+str(VALIDATION['sharpe_min'])+')'}")
        print(f"    Sortino Ratio : {self.sortino_ratio:.3f}  "
              f"{'OK' if self.sortino_ratio >= VALIDATION['sortino_min'] else 'KO'}")
        print(f"    Profit Factor : {self.profit_factor:.3f}  "
              f"{'OK' if self.profit_factor >= VALIDATION['profit_factor_min'] else 'KO'}")
        print(f"    Expectancy    : {self.expectancy:.4f}")
        print(f"\n  Risque")
        print(f"    Max Drawdown  : {self.max_drawdown_pct:.2f} %  "
              f"{'OK' if self.max_drawdown_pct <= VALIDATION['max_dd_pct_max'] else 'KO (> '+str(VALIDATION['max_dd_pct_max'])+'%)'}")
        print(f"    DD Duration   : {self.max_dd_duration}")
        print(f"\n  Trades")
        print(f"    Total trades  : {self.total_trades}  "
              f"{'OK' if self.total_trades >= VALIDATION['min_trades'] else 'KO (< '+str(VALIDATION['min_trades'])+')'}")
        print(f"    Win Rate      : {self.win_rate_pct:.1f} %  "
              f"{'OK' if self.win_rate_pct >= VALIDATION['win_rate_min'] else 'KO'}")
        print(f"    Avg Win       : {self.avg_win_pct:+.2f} %")
        print(f"    Avg Loss      : {self.avg_loss_pct:+.2f} %")
        print(f"    Best Trade    : {self.best_trade_pct:+.2f} %")
        print(f"    Worst Trade   : {self.worst_trade_pct:+.2f} %")
        print(f"    R:R effectif  : {self.rr_ratio:.2f}")
        if not self.validated:
            print(f"\n  Points de defaillance :")
            for issue in self.validation_issues:
                print(f"    - {issue}")
        print(f"\n  Parametres : SL={self.sl_atr_mult}xATR | TP={self.tp_atr_mult}xATR")
        print(bar)


def load_data(instrument: str, tf: str, session_filter: bool = True) -> pd.DataFrame:
    path = DATA_DIR / f"{instrument}_{tf}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"Donnees manquantes : {path}\n"
            f"python src/download_historical.py --source dukascopy "
            f"--instrument {instrument} --tf {tf} --years 2"
        )
    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("UTC")
    df = df.sort_index()
    if session_filter and instrument in SESSION_FILTERS:
        h_start, h_end = SESSION_FILTERS[instrument]
        df = df[(df.index.hour >= h_start) & (df.index.hour < h_end)]
        logger.info(f"Filtre session {instrument} ({h_start}h-{h_end}h UTC) : {len(df):,} bars")
    df = df.dropna(subset=["open","high","low","close"])
    df = df[(df[["open","high","low","close"]] > 0).all(axis=1)]
    df["volume"] = df["volume"].fillna(0.0)
    logger.info(f"Donnees : {instrument} {tf} | {len(df):,} bars | "
                f"{df.index[0].date()} -> {df.index[-1].date()}")
    return df


def diagnose_signals(el, es, df, label=""):
    n=len(df); n_days=max(1,(df.index[-1]-df.index[0]).days)
    total=int(el.sum())+int(es.sum()); tpd=total/n_days
    print(f"\n  Signaux {label}:")
    print(f"    LONG:{int(el.sum()):>6}  SHORT:{int(es.sum()):>6}  TOTAL:{total:>6}")
    print(f"    Freq: {total/n*100:.2f}% | {tpd:.1f}/jour")
    if tpd>5: print(f"    ALERTE : surtrading ({tpd:.1f}/jour)")
    elif tpd<0.3: print(f"    ALERTE : sous-trading ({tpd:.2f}/jour)")
    else: print(f"    INFO   : frequence raisonnable ({tpd:.1f}/jour)")


# -----------------------------------------------------------------------
# Utilitaires combines long+short
# -----------------------------------------------------------------------

def _max_dd_duration(dd: pd.Series) -> pd.Timedelta:
    try:
        in_dd=dd<0; groups=(in_dd!=in_dd.shift()).cumsum()
        durs=dd[in_dd].groupby(groups[in_dd]).apply(lambda g: g.index[-1]-g.index[0])
        return durs.max() if len(durs)>0 else pd.Timedelta(0)
    except Exception:
        return pd.Timedelta(0)


def _combine_long_short(
    pf_long: vbt.Portfolio,
    pf_short: vbt.Portfolio,
    init_cash: float,
    tf: str,
) -> tuple:
    """
    Combine deux portfolios LONG-only et SHORT-only en stats unifiees.
    Contournement : vbt 1.0.0 SizeType.Percent ne supporte pas la reversal.
    """
    t_l = pf_long.trades.records_readable.copy()
    t_s = pf_short.trades.records_readable.copy()
    all_t = pd.concat([t_l, t_s], ignore_index=True)

    n_tot  = len(all_t)
    wins   = int((all_t["PnL"] > 0).sum()) if n_tot > 0 else 0
    losses = n_tot - wins
    wr     = wins / n_tot * 100 if n_tot > 0 else 0.

    w_pnl = all_t.loc[all_t["PnL"]>0, "PnL"]
    l_pnl = all_t.loc[all_t["PnL"]<0, "PnL"]
    pf_f  = w_pnl.sum() / abs(l_pnl.sum()) if len(l_pnl)>0 else float("inf")

    avg_win  = all_t.loc[all_t["PnL"]>0,"Return"].mean()*100 if wins>0   else 0.
    avg_loss = all_t.loc[all_t["PnL"]<0,"Return"].mean()*100 if losses>0 else 0.
    best_t   = float(all_t["Return"].max()*100) if n_tot>0 else 0.
    worst_t  = float(all_t["Return"].min()*100) if n_tot>0 else 0.
    expect   = float(all_t["PnL"].mean()) if n_tot>0 else 0.

    eq_l = pf_long.value()
    eq_s = pf_short.value()
    eq   = (eq_l - init_cash) + (eq_s - init_cash) + init_cash

    n_bpy = {"M1":252*540,"M5":252*96,"M15":252*32,
              "H1":252*8,"H4":252*2,"D1":252}.get(tf, 252*96)
    ret   = eq.pct_change().dropna()
    if ret.std() > 0:
        sharpe  = float(ret.mean() / ret.std() * np.sqrt(n_bpy))
        neg_std = ret[ret<0].std()
        sortino = float(ret.mean()/neg_std*np.sqrt(n_bpy)) if neg_std>0 else 9999.
    else:
        sharpe = sortino = 0.

    dd     = (eq / eq.cummax()) - 1
    max_dd = abs(float(dd.min() * 100))
    tot_r  = float((eq.iloc[-1] - init_cash) / init_cash * 100)
    calmar = tot_r / max_dd if max_dd > 0 else 9999.

    stats = {
        "Sharpe Ratio":           sharpe,
        "Sortino Ratio":          sortino,
        "Calmar Ratio":           min(calmar, 9999.),
        "Omega Ratio":            0.,
        "Profit Factor":          pf_f,
        "Expectancy":             expect,
        "Max Drawdown [%]":       max_dd,
        "Max Drawdown Duration":  str(_max_dd_duration(dd)),
        "Total Closed Trades":    n_tot,
        "Win Rate [%]":           wr,
        "Avg Winning Trade [%]":  avg_win,
        "Avg Losing Trade [%]":   avg_loss,
        "Best Trade [%]":         best_t,
        "Worst Trade [%]":        worst_t,
        "Total Return [%]":       tot_r,
        "Avg Winning Trade Duration": "N/A",
        "Avg Losing Trade Duration":  "N/A",
    }
    return stats, all_t


# -----------------------------------------------------------------------
# Generateurs V1 / V2
# -----------------------------------------------------------------------

def generate_signals_setup_b(df, sl_mult=1.5, tp_mult=3.0, ema_fast=20, ema_slow=50,
    rsi_period=14, atr_period=14, rsi_long_min=45., rsi_long_max=65.,
    rsi_short_min=35., rsi_short_max=55.):
    close=df["close"].values; high=df["high"].values; low=df["low"].values
    ef=talib.EMA(close,ema_fast); es=talib.EMA(close,ema_slow)
    rsi=talib.RSI(close,rsi_period); atr=talib.ATR(high,low,close,atr_period)
    _,_,mh=talib.MACD(close,12,26,9)
    el=(ef>es)&(rsi>=rsi_long_min)&(rsi<=rsi_long_max)&(mh>0)
    ess=(ef<es)&(rsi>=rsi_short_min)&(rsi<=rsi_short_max)&(mh<0)
    el=np.nan_to_num(el,nan=False).astype(bool)&~np.roll(el,1)
    ess=np.nan_to_num(ess,nan=False).astype(bool)&~np.roll(ess,1)
    with np.errstate(divide="ignore",invalid="ignore"):
        sl_p=np.nan_to_num(np.where(close>0,sl_mult*atr/close,0.015),nan=0.015)
        tp_p=np.nan_to_num(np.where(close>0,tp_mult*atr/close,0.030),nan=0.030)
    idx=df.index
    return pd.Series(el,idx).fillna(False).astype(bool),pd.Series(ess,idx).fillna(False).astype(bool),pd.Series(sl_p,idx),pd.Series(tp_p,idx)

def generate_signals_setup_c(df, sl_mult=1.5, tp_mult=3.0, ema_fast=20, ema_slow=50,
    rsi_period=14, atr_period=14):
    close=df["close"].values; high=df["high"].values; low=df["low"].values; vol=df["volume"].values
    ef=talib.EMA(close,ema_fast); es=talib.EMA(close,ema_slow)
    rsi=talib.RSI(close,rsi_period); atr=talib.ATR(high,low,close,atr_period); vma=talib.SMA(vol,20)
    near=np.abs(close-ef)<atr*0.5
    el=near&(ef>es)&(rsi>=40)&(rsi<=65)&(vol>vma*1.1)
    ess=near&(ef<es)&(rsi>=35)&(rsi<=60)&(vol>vma*1.1)
    el=np.nan_to_num(el,nan=False).astype(bool)&~np.roll(el,1)
    ess=np.nan_to_num(ess,nan=False).astype(bool)&~np.roll(ess,1)
    with np.errstate(divide="ignore",invalid="ignore"):
        sl_p=np.nan_to_num(np.where(close>0,sl_mult*atr/close,0.015),nan=0.015)
        tp_p=np.nan_to_num(np.where(close>0,tp_mult*atr/close,0.030),nan=0.030)
    idx=df.index
    return pd.Series(el,idx).fillna(False).astype(bool),pd.Series(ess,idx).fillna(False).astype(bool),pd.Series(sl_p,idx),pd.Series(tp_p,idx)

def _apply_cooldown(arr, min_bars):
    result=arr.copy().astype(bool); last=-min_bars-1
    for i in range(len(result)):
        if result[i]:
            if i-last<min_bars: result[i]=False
            else: last=i
    return result

def generate_signals_setup_b_v2(df, sl_mult=1.5, tp_mult=3.0, ema_fast=20, ema_slow=50,
    rsi_period=14, atr_period=14, adx_period=14, adx_min=20., atr_ratio_min=0.8,
    cooldown_bars=24, rsi_long_min=45., rsi_long_max=65., rsi_short_min=35., rsi_short_max=55.):
    close=df["close"].values; high=df["high"].values; low=df["low"].values
    ef=talib.EMA(close,ema_fast); es=talib.EMA(close,ema_slow)
    rsi=talib.RSI(close,rsi_period); atr=talib.ATR(high,low,close,atr_period)
    atr_avg=talib.SMA(atr,50); adx=talib.ADX(high,low,close,adx_period)
    _,_,mh=talib.MACD(close,12,26,9)
    mh_up=mh>np.roll(mh,1); mh_dn=mh<np.roll(mh,1)
    adx_ok=adx>adx_min; atr_ok=atr>(atr_avg*atr_ratio_min)
    warmup=np.arange(len(close))>60
    el=(ef>es)&(rsi>=rsi_long_min)&(rsi<=rsi_long_max)&(mh>0)&mh_up&adx_ok&atr_ok&(close>ef)&warmup
    ess=(ef<es)&(rsi>=rsi_short_min)&(rsi<=rsi_short_max)&(mh<0)&mh_dn&adx_ok&atr_ok&(close<ef)&warmup
    el=np.nan_to_num(el,nan=False).astype(bool)&~np.roll(el,1)
    ess=np.nan_to_num(ess,nan=False).astype(bool)&~np.roll(ess,1)
    el=_apply_cooldown(el,cooldown_bars); ess=_apply_cooldown(ess,cooldown_bars)
    with np.errstate(divide="ignore",invalid="ignore"):
        sl_p=np.nan_to_num(np.where(close>0,sl_mult*atr/close,0.015),nan=0.015)
        tp_p=np.nan_to_num(np.where(close>0,tp_mult*atr/close,0.030),nan=0.030)
    idx=df.index
    return pd.Series(el,idx).fillna(False).astype(bool),pd.Series(ess,idx).fillna(False).astype(bool),pd.Series(sl_p,idx),pd.Series(tp_p,idx)

def generate_signals_setup_c_v2(df, sl_mult=1.5, tp_mult=3.0, ema_fast=20, ema_slow=50,
    atr_period=14, adx_period=14, adx_min=18., cooldown_bars=24):
    close=df["close"].values; high=df["high"].values; low=df["low"].values; vol=df["volume"].values
    ef=talib.EMA(close,ema_fast); es=talib.EMA(close,ema_slow); rsi=talib.RSI(close,14)
    atr=talib.ATR(high,low,close,atr_period); atr_avg=talib.SMA(atr,50)
    adx=talib.ADX(high,low,close,adx_period); vma=talib.SMA(vol,20)
    _,_,mh=talib.MACD(close,12,26,9)
    adx_ok=adx>adx_min; atr_ok=atr>(atr_avg*0.8); vol_ok=vol>vma*1.2
    near=np.abs(close-ef)<atr*0.4; warmup=np.arange(len(close))>60
    el=near&(ef>es)&(rsi>40)&(rsi<62)&adx_ok&atr_ok&vol_ok&(mh>0)&warmup
    ess=near&(ef<es)&(rsi>38)&(rsi<60)&adx_ok&atr_ok&vol_ok&(mh<0)&warmup
    el=np.nan_to_num(el,nan=False).astype(bool)&~np.roll(el,1)
    ess=np.nan_to_num(ess,nan=False).astype(bool)&~np.roll(ess,1)
    el=_apply_cooldown(el,cooldown_bars); ess=_apply_cooldown(ess,cooldown_bars)
    with np.errstate(divide="ignore",invalid="ignore"):
        sl_p=np.nan_to_num(np.where(close>0,sl_mult*atr/close,0.015),nan=0.015)
        tp_p=np.nan_to_num(np.where(close>0,tp_mult*atr/close,0.030),nan=0.030)
    idx=df.index
    return pd.Series(el,idx).fillna(False).astype(bool),pd.Series(ess,idx).fillna(False).astype(bool),pd.Series(sl_p,idx),pd.Series(tp_p,idx)


# -----------------------------------------------------------------------
# V3 : Detection OTE Fibonacci [0.618-0.786]  *** POINT DE RETOUR ***
# -----------------------------------------------------------------------

def generate_signals_setup_b_v3(
    df: pd.DataFrame,
    sl_mult: float = 1.0,
    tp_mult: float = 3.0,
    swing_order: int = 15,
    adx_min: float = 18.0,
    cooldown_bars: int = 24,
    atr_period: int = 14,
    ma_h1_period: int = 2400,
    ote_low: float = 0.618,
    ote_high: float = 0.786,
) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """
    SETUP_B V3 : Detection OTE Fibonacci fidele au setup Kasper.
    POINT DE RETOUR GARANTI.

    Resultats XAUUSD M5 (SL=1.0 TP=3.0) :
      Sharpe 0.65 / Sortino 0.31 / Max DD 0.62% / Win 36.1% / PF 1.15 / trades 330

    Ne pas modifier ce generateur. Bv4 est l'evolution experimentale.
    """
    close  = df["close"].values
    high   = df["high"].values
    low    = df["low"].values
    n      = len(close)

    atr    = talib.ATR(high, low, close, atr_period)
    adx    = talib.ADX(high, low, close, 14)
    ma_h1  = talib.SMA(close, min(ma_h1_period, n - 1))

    sh_idx = argrelextrema(high, np.greater, order=swing_order)[0]
    sl_idx = argrelextrema(low,  np.less,    order=swing_order)[0]

    el_arr = np.zeros(n, dtype=bool)
    es_arr = np.zeros(n, dtype=bool)
    last_long  = -cooldown_bars - 1
    last_short = -cooldown_bars - 1
    warmup = min(ma_h1_period, n - 1) + swing_order + 10

    for i in range(warmup, n - swing_order):
        if np.isnan(adx[i]) or np.isnan(ma_h1[i]) or np.isnan(atr[i]):
            continue
        if adx[i] < adx_min:
            continue

        prev_sh = sh_idx[sh_idx < i - swing_order]
        prev_sl = sl_idx[sl_idx < i - swing_order]
        if len(prev_sh) < 1 or len(prev_sl) < 1:
            continue

        last_sh_i = prev_sh[-1]; last_sl_i = prev_sl[-1]
        sh_v = high[last_sh_i]; sl_v = low[last_sl_i]
        spread = sh_v - sl_v
        if spread < atr[i] * 0.5:
            continue

        price = close[i]

        if price > ma_h1[i] and last_sl_i < last_sh_i:
            ote_lo = sl_v + ote_low  * spread
            ote_hi = sl_v + ote_high * spread
            if ote_lo <= price <= ote_hi:
                if i - last_long >= cooldown_bars:
                    el_arr[i] = True; last_long = i

        elif price < ma_h1[i] and last_sh_i < last_sl_i:
            ote_hi_s = sh_v - ote_low  * spread
            ote_lo_s = sh_v - ote_high * spread
            if ote_lo_s <= price <= ote_hi_s:
                if i - last_short >= cooldown_bars:
                    es_arr[i] = True; last_short = i

    with np.errstate(divide="ignore", invalid="ignore"):
        sl_pct = np.nan_to_num(np.where(close>0, sl_mult*atr/close, 0.015), nan=0.015)
        tp_pct = np.nan_to_num(np.where(close>0, tp_mult*atr/close, 0.030), nan=0.030)

    idx = df.index
    return (pd.Series(el_arr, index=idx).astype(bool),
            pd.Series(es_arr, index=idx).astype(bool),
            pd.Series(sl_pct, index=idx),
            pd.Series(tp_pct, index=idx))


# -----------------------------------------------------------------------
# V4 : OTE Fibonacci + Filtre Kerner + Score Fraicheur (analogie embouteillage)
# -----------------------------------------------------------------------
#
# Mappings analogie -> trading :
#   Flux Libre    (ADX >= 22)           -> voie fluide, changement rentable
#   Flux Synchronise (18 <= ADX < 22)   -> voie range, changement inutile (BLOQUE)
#   Phase J / Bouchon (ATR spike)       -> embouteillage total (BLOQUE)
#   Score fraicheur OTE                 -> facteur politesse MOBIL :
#                                          swing deja reteste = voie embouteillee
#   TP rapide 2.0xATR                   -> "valider le gain" = securiser avant retour
#   SL 0.8xATR                          -> filet de securite proche

def generate_signals_setup_b_v4(
    df: pd.DataFrame,
    sl_mult: float = 0.8,         # SL serre : securiser rapidement
    tp_mult: float = 2.0,         # TP rapide : valider le gain avant que le marche revienne
    # --- Filtres Kerner ---
    adx_libre_min: float = 22.0,  # Flux Libre = ADX >= 22
                                   # 18-22 = Flux Synchronise (range) = BLOQUE
    atr_spike_mult: float = 2.0,  # Bloquer si ATR > 2x moy = Phase J (spike)
    atr_avg_period: int = 50,
    # --- OTE params (identiques a Bv3) ---
    swing_order: int = 15,
    cooldown_bars: int = 24,
    atr_period: int = 14,
    ma_h1_period: int = 2400,
    ote_low: float = 0.618,
    ote_high: float = 0.786,
    # --- Fraicheur OTE (score MOBIL) ---
    ote_freshness_bars: int = 36, # 3h de cooldown OTE (36 bars M5)
                                   # Zone deja visitee = voie embouteillee -> BLOQUE
) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """
    SETUP_B V4 : OTE Fibonacci + 3 filtres inspires de la theorie Kerner + MOBIL.

    Ameliorations par rapport a Bv3 :

    1. FILTRE FLUX LIBRE (Kerner) :
       ADX < 22 = Flux Synchronise -> changement de voie a 20-25 km/h, benefice nul
       -> BLOQUE. Etude M1 Sydney : 60% des changements de voie a vitesse nulle sont
       dans cette plage. Bv3 autorisait ADX >= 18 (trop permissif).

    2. FILTRE PHASE J / BOUCHON :
       ATR > 2x ATR_moy_50 = spike de volatilite = Phase J (bouchon mobile).
       Vitesse quasi nulle, risque d'accident maximum -> BLOQUE.
       Correspond aux 5-10% d'accidents causes par changement de voie en bouchon.

    3. SCORE FRAICHEUR OTE (analogie facteur politesse MOBIL p) :
       Si la zone OTE [0.618-0.786] a deja ete visitee dans les 36 dernieres barres,
       elle est "exploitee" (voie deja embouteillee apres un changement precedent).
       Un 2eme retest sans cooldown = false signal = BLOQUE.
       Ce filtre elimine les entrees dans des zones "deja connues" du marche.

    4. TP RAPIDE (valider le gain avant retour) :
       TP=2.0xATR vs 3.0xATR pour Bv3. Principe : "j'ai depasse N vehicules,
       je suis dans la bonne voie, je consolide." Le marche revient souvent avant
       3.0xATR mais atteint 2.0xATR plus souvent.

    Baseline : Bv3 SL=1.0 TP=3.0 -> Sharpe 0.65 / PF 1.15 / Win 36.1% / trades 330
    Cible Bv4 : PF > 1.20 avec Win rate > 38% (grace aux filtres de qualite)
    """
    close  = df["close"].values
    high   = df["high"].values
    low    = df["low"].values
    n      = len(close)

    atr     = talib.ATR(high, low, close, atr_period)
    atr_avg = talib.SMA(atr, atr_avg_period)
    adx     = talib.ADX(high, low, close, 14)
    ma_h1   = talib.SMA(close, min(ma_h1_period, n - 1))

    sh_idx = argrelextrema(high, np.greater, order=swing_order)[0]
    sl_idx = argrelextrema(low,  np.less,    order=swing_order)[0]

    el_arr = np.zeros(n, dtype=bool)
    es_arr = np.zeros(n, dtype=bool)
    last_long  = -cooldown_bars - 1
    last_short = -cooldown_bars - 1
    last_ote_long  = -ote_freshness_bars - 1   # Derniere visite zone OTE long
    last_ote_short = -ote_freshness_bars - 1   # Derniere visite zone OTE short
    warmup = min(ma_h1_period, n - 1) + swing_order + 10

    for i in range(warmup, n - swing_order):
        if np.isnan(adx[i]) or np.isnan(ma_h1[i]) or np.isnan(atr[i]):
            continue
        if np.isnan(atr_avg[i]):
            continue

        # --- Filtre Kerner 1 : Flux Libre uniquement ---
        # 18-22 = Flux Synchronise = voie range = changement inutile
        if adx[i] < adx_libre_min:
            continue

        # --- Filtre Kerner 2 : Phase J / Bouchon ---
        # Spike ATR = vitesse chaotique = risque d'accident max
        if atr[i] > atr_avg[i] * atr_spike_mult:
            continue

        prev_sh = sh_idx[sh_idx < i - swing_order]
        prev_sl = sl_idx[sl_idx < i - swing_order]
        if len(prev_sh) < 1 or len(prev_sl) < 1:
            continue

        last_sh_i = prev_sh[-1]; last_sl_i = prev_sl[-1]
        sh_v = high[last_sh_i]; sl_v = low[last_sl_i]
        spread = sh_v - sl_v
        if spread < atr[i] * 0.5:
            continue

        price = close[i]

        # --- LONG OTE ---
        if price > ma_h1[i] and last_sl_i < last_sh_i:
            ote_lo = sl_v + ote_low  * spread
            ote_hi = sl_v + ote_high * spread
            if ote_lo <= price <= ote_hi:
                # --- Filtre fraicheur MOBIL (score p) ---
                # Zone visitee recemment = voie deja embouteillee apres un changement
                if i - last_ote_long < ote_freshness_bars:
                    last_ote_long = i   # Marquer visite sans entrer
                    continue
                last_ote_long = i
                if i - last_long >= cooldown_bars:
                    el_arr[i] = True
                    last_long  = i

        # --- SHORT OTE ---
        elif price < ma_h1[i] and last_sh_i < last_sl_i:
            ote_hi_s = sh_v - ote_low  * spread
            ote_lo_s = sh_v - ote_high * spread
            if ote_lo_s <= price <= ote_hi_s:
                if i - last_ote_short < ote_freshness_bars:
                    last_ote_short = i
                    continue
                last_ote_short = i
                if i - last_short >= cooldown_bars:
                    es_arr[i] = True
                    last_short = i

    with np.errstate(divide="ignore", invalid="ignore"):
        sl_pct = np.nan_to_num(np.where(close>0, sl_mult*atr/close, 0.010), nan=0.010)
        tp_pct = np.nan_to_num(np.where(close>0, tp_mult*atr/close, 0.020), nan=0.020)

    idx = df.index
    return (pd.Series(el_arr, index=idx).astype(bool),
            pd.Series(es_arr, index=idx).astype(bool),
            pd.Series(sl_pct, index=idx),
            pd.Series(tp_pct, index=idx))


SIGNAL_GENERATORS = {
    "B":    generate_signals_setup_b,
    "Bv2":  generate_signals_setup_b_v2,
    "Bv3":  generate_signals_setup_b_v3,
    "Bv4":  generate_signals_setup_b_v4,
    "C":    generate_signals_setup_c,
    "Cv2":  generate_signals_setup_c_v2,
}


# -----------------------------------------------------------------------
# Backtest principal
# -----------------------------------------------------------------------

def run_backtest(
    instrument: str, tf: str, setup: str = "Bv3",
    sl_atr_mult: float = 1.0, tp_atr_mult: float = 3.0,
    init_cash: float = 10_000.0, session_filter: bool = True,
    save_report: bool = True, **signal_kwargs,
) -> BacktestMetrics:
    setup_name = f"SETUP_{setup}"
    logger.info(f"Backtest {setup_name} | {instrument} {tf} | "
                f"SL={sl_atr_mult}xATR TP={tp_atr_mult}xATR | capital={init_cash}")
    t0 = time.time()

    df = load_data(instrument, tf, session_filter=session_filter)
    if len(df) < 200:
        raise ValueError(f"Pas assez de donnees : {len(df)} bars.")

    gen_fn = SIGNAL_GENERATORS.get(setup)
    if not gen_fn:
        raise ValueError(f"Setup {setup!r} inconnu. Utiliser : {list(SIGNAL_GENERATORS)}")

    entries_long, entries_short, sl_pct, tp_pct = gen_fn(
        df, sl_mult=sl_atr_mult, tp_mult=tp_atr_mult, **signal_kwargs)

    conflict = entries_long & entries_short
    if conflict.any():
        entries_short = entries_short & ~entries_long

    n_long  = int(entries_long.sum())
    n_short = int(entries_short.sum())
    logger.info(f"Signaux : {n_long} LONG + {n_short} SHORT = {n_long+n_short} total")
    diagnose_signals(entries_long, entries_short, df, label=f"{setup_name} {instrument} {tf}")

    if n_long + n_short == 0:
        raise ValueError("Aucun signal genere.")

    fees  = FEES.get(instrument, FEES["DEFAULT"])
    close = df["close"]

    pf_long = vbt.Portfolio.from_signals(
        close     = close,
        entries   = entries_long,
        exits     = pd.Series(False, index=close.index),
        sl_stop   = sl_pct, tp_stop = tp_pct,
        size      = POSITION_SIZE, size_type = POSITION_SIZETYPE,
        init_cash = init_cash, fees = fees, freq = _infer_freq(tf),
    )
    pf_short = vbt.Portfolio.from_signals(
        close         = close,
        short_entries = entries_short,
        short_exits   = pd.Series(False, index=close.index),
        sl_stop   = sl_pct, tp_stop = tp_pct,
        size      = POSITION_SIZE, size_type = POSITION_SIZETYPE,
        init_cash = init_cash, fees = fees, freq = _infer_freq(tf),
    )

    elapsed = time.time() - t0
    logger.info(f"Backtest termine en {elapsed:.1f}s")

    stats, trades_df = _combine_long_short(pf_long, pf_short, init_cash, tf)
    pf = pf_long

    def _f(k, d=0.0):
        v = stats.get(k, d)
        return float(v) if v is not None and str(v) != "nan" else d

    sharpe   = _f("Sharpe Ratio"); sortino = _f("Sortino Ratio")
    calmar   = min(_f("Calmar Ratio"), 9999.)
    omega    = _f("Omega Ratio");  pf_f    = _f("Profit Factor")
    expect   = _f("Expectancy");   max_dd  = _f("Max Drawdown [%]")
    tot_ret  = _f("Total Return [%]"); win_rate = _f("Win Rate [%]")
    avg_win  = _f("Avg Winning Trade [%]"); avg_loss = _f("Avg Losing Trade [%]")
    best_t   = _f("Best Trade [%]"); worst_t = _f("Worst Trade [%]")
    n_trades = int(_f("Total Closed Trades"))
    rr_eff   = abs(avg_win / avg_loss) if avg_loss != 0 else 0.

    issues = []
    if sharpe   < VALIDATION["sharpe_min"]:        issues.append(f"Sharpe {sharpe:.2f} < {VALIDATION['sharpe_min']}")
    if max_dd   > VALIDATION["max_dd_pct_max"]:    issues.append(f"Max DD {max_dd:.2f}% > {VALIDATION['max_dd_pct_max']}%")
    if n_trades < VALIDATION["min_trades"]:         issues.append(f"Trades {n_trades} < {VALIDATION['min_trades']}")
    if win_rate < VALIDATION["win_rate_min"]:       issues.append(f"Win rate {win_rate:.1f}% < {VALIDATION['win_rate_min']}%")
    if pf_f     < VALIDATION["profit_factor_min"]:  issues.append(f"Profit Factor {pf_f:.2f} < {VALIDATION['profit_factor_min']}")

    m = BacktestMetrics(
        setup_name=setup_name, instrument=instrument, tf=tf,
        period_start=str(df.index[0].date()), period_end=str(df.index[-1].date()),
        run_at=datetime.now(tz=timezone.utc).isoformat(),
        total_return_pct=round(tot_ret,4), sharpe_ratio=round(sharpe,4),
        sortino_ratio=round(sortino,4), calmar_ratio=round(calmar,4),
        omega_ratio=round(omega,4), profit_factor=round(pf_f,4),
        expectancy=round(expect,6), max_drawdown_pct=round(max_dd,4),
        max_dd_duration=str(stats.get("Max Drawdown Duration","N/A")),
        total_trades=n_trades, win_rate_pct=round(win_rate,2),
        avg_win_pct=round(avg_win,4), avg_loss_pct=round(avg_loss,4),
        best_trade_pct=round(best_t,4), worst_trade_pct=round(worst_t,4),
        avg_win_duration=str(stats.get("Avg Winning Trade Duration","N/A")),
        avg_loss_duration=str(stats.get("Avg Losing Trade Duration","N/A")),
        validated=len(issues)==0, validation_issues=issues,
        sl_atr_mult=sl_atr_mult, tp_atr_mult=tp_atr_mult, rr_ratio=round(rr_eff,3),
    )

    if save_report:
        _export_report(m, pf, trades_df, close)

    return m


# -----------------------------------------------------------------------
# Optimisation
# -----------------------------------------------------------------------

def optimize_sl_tp(instrument, tf, setup="Bv4", sl_range=None, tp_range=None,
                   session_filter=True):
    sl_range = sl_range or [0.6, 0.8, 1.0, 1.2, 1.5]
    tp_range = tp_range or [1.5, 2.0, 2.5, 3.0, 3.5]
    logger.info(f"Optimisation {setup} | {instrument} {tf} | {len(sl_range)*len(tp_range)} combinaisons")

    df     = load_data(instrument, tf, session_filter=session_filter)
    gen_fn = SIGNAL_GENERATORS[setup]
    fees   = FEES.get(instrument, FEES["DEFAULT"])
    results = []

    for sl in sl_range:
        for tp in tp_range:
            if tp <= sl: continue
            try:
                el, es, sl_pct, tp_pct = gen_fn(df, sl_mult=sl, tp_mult=tp)
                if el.sum() + es.sum() == 0: continue
                conflict = el & es
                if conflict.any(): es = es & ~el
                pf_l = vbt.Portfolio.from_signals(
                    close=df["close"], entries=el,
                    exits=pd.Series(False, index=df["close"].index),
                    sl_stop=sl_pct, tp_stop=tp_pct,
                    size=POSITION_SIZE, size_type=POSITION_SIZETYPE,
                    init_cash=10_000.0, fees=fees, freq=_infer_freq(tf),
                )
                pf_s = vbt.Portfolio.from_signals(
                    close=df["close"], short_entries=es,
                    short_exits=pd.Series(False, index=df["close"].index),
                    sl_stop=sl_pct, tp_stop=tp_pct,
                    size=POSITION_SIZE, size_type=POSITION_SIZETYPE,
                    init_cash=10_000.0, fees=fees, freq=_infer_freq(tf),
                )
                s, _ = _combine_long_short(pf_l, pf_s, 10_000.0, tf)
                def g(k): return round(float(s.get(k,0) or 0), 3)
                results.append({
                    "sl_mult":sl,"tp_mult":tp,"rr_target":round(tp/sl,2),
                    "sharpe":g("Sharpe Ratio"),"sortino":g("Sortino Ratio"),
                    "max_dd_pct":g("Max Drawdown [%]"),"total_return":g("Total Return [%]"),
                    "win_rate":g("Win Rate [%]"),"profit_factor":g("Profit Factor"),
                    "n_trades":int(s.get("Total Closed Trades",0) or 0),
                })
                logger.debug(f"SL={sl} TP={tp} | Sharpe={results[-1]['sharpe']:.2f} "
                             f"DD={results[-1]['max_dd_pct']:.2f}% trades={results[-1]['n_trades']}")
            except Exception as e:
                logger.warning(f"SL={sl} TP={tp} erreur : {e}")

    df_res = pd.DataFrame(results)
    if not df_res.empty:
        df_res = df_res.sort_values("sharpe", ascending=False)
        ts  = datetime.now().strftime("%Y%m%d_%H%M")
        out = BACKTEST_DIR / f"optim_{setup}_{instrument}_{tf}_{ts}.csv"
        df_res.to_csv(out, index=False)
        logger.info(f"Resultats sauvegardes : {out}")
        print(f"\n=== Top 10 combinaisons (trie par Sharpe) ===")
        print(df_res.head(10).to_string(index=False))
    return df_res


# -----------------------------------------------------------------------
# Export / utils
# -----------------------------------------------------------------------

def _export_report(m, pf, trades_df, close):
    ts   = datetime.now().strftime("%Y%m%d_%H%M")
    name = f"{m.setup_name}_{m.instrument}_{m.tf}_{ts}"
    json_path = BACKTEST_DIR / f"{name}_metrics.json"
    with open(json_path,"w",encoding="utf-8") as f:
        json.dump(m.to_dict(), f, indent=2, ensure_ascii=False)
    trades_df.to_csv(BACKTEST_DIR / f"{name}_trades.csv", index=False)
    equity = pf.value()
    pd.DataFrame({"timestamp":equity.index,"equity":equity.values.round(4),
                  "drawdown":pf.drawdown().values.round(6)}).to_csv(
        BACKTEST_DIR / f"{name}_equity.csv", index=False)
    logger.info(f"Rapport : {json_path.name}")

def _infer_freq(tf):
    return {"M1":"1min","M5":"5min","M15":"15min","H1":"1h","H4":"4h","D1":"1D"}.get(tf,"5min")

def list_available_data():
    print("\n=== Donnees disponibles ===")
    for f in sorted(DATA_DIR.glob("*.parquet")):
        try:
            df=pd.read_parquet(f)
            if "timestamp" in df.columns: df=df.set_index("timestamp")
            p=f.stem.split("_")
            print(f"  {p[0]:8s} {p[1]:5s} | {len(df):>8,} bars | "
                  f"{df.index[0].date()} -> {df.index[-1].date()}")
        except Exception as e: print(f"  {f.name}: erreur ({e})")
    print()


# -----------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(description="Backtester Bonaza",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
POINT DE RETOUR (Bv3 valide) :
  python src/backtester.py --setup Bv3 --instrument XAUUSD --tf M5 --session --sl 1.0 --tp 3.0

EXPERIMENTATION (Bv4 analogie embouteillage) :
  python src/backtester.py --setup Bv4 --instrument XAUUSD --tf M5 --session
  python src/backtester.py --setup Bv4 --instrument XAUUSD --tf M5 --session --optimize
  python src/backtester.py --setup ALLv4 --instrument XAUUSD --tf M5 --session

COMPARAISON directe :
  python src/backtester.py --setup Bv3 --instrument XAUUSD --tf M5 --session --sl 1.0 --tp 3.0
  python src/backtester.py --setup Bv4 --instrument XAUUSD --tf M5 --session
        """)
    p.add_argument("--setup",      default="Bv4",
                   choices=["A","B","Bv2","Bv3","Bv4","C","Cv2","ALL","ALLv2","ALLv3","ALLv4"])
    p.add_argument("--instrument", default="XAUUSD",
                   choices=["XAUUSD","DAX","CAC40","EURUSD","USDJPY"])
    p.add_argument("--tf",         default="M5")
    p.add_argument("--sl",         type=float, default=0.8)
    p.add_argument("--tp",         type=float, default=2.0)
    p.add_argument("--capital",    type=float, default=10_000.0)
    p.add_argument("--session",    action="store_true")
    p.add_argument("--no-session", action="store_true")
    p.add_argument("--optimize",   action="store_true")
    p.add_argument("--list-data",  action="store_true")
    return p.parse_args()


def main():
    args = _parse_args()
    if args.list_data: list_available_data(); return
    session = not args.no_session

    if args.optimize:
        optimize_sl_tp(args.instrument, args.tf,
                       args.setup if args.setup not in ("ALL","ALLv2","ALLv3","ALLv4") else "Bv4",
                       session_filter=session)
        return

    setups = (["B","C"]     if args.setup=="ALL"    else
              ["Bv2","Cv2"] if args.setup=="ALLv2"  else
              ["Bv3","Cv2"] if args.setup=="ALLv3"  else
              ["Bv4","Cv2"] if args.setup=="ALLv4"  else [args.setup])

    all_metrics = []
    for s in setups:
        try:
            m = run_backtest(args.instrument, args.tf, s,
                             sl_atr_mult=args.sl, tp_atr_mult=args.tp,
                             init_cash=args.capital, session_filter=session)
            m.print_report()
            all_metrics.append(m)
        except Exception as e:
            logger.error(f"Erreur SETUP_{s} : {e}")
            import traceback; traceback.print_exc()

    if len(all_metrics) > 1:
        print("\n=== COMPARATIF ===")
        print(pd.DataFrame([{"Setup":m.setup_name,"Sharpe":m.sharpe_ratio,
                              "Sortino":m.sortino_ratio,"Max DD %":m.max_drawdown_pct,
                              "Win %":m.win_rate_pct,"PF":m.profit_factor,
                              "Trades":m.total_trades,"Valide":"OUI" if m.validated else "NON"}
                             for m in all_metrics]).to_string(index=False))
    return all_metrics

if __name__ == "__main__":
    main()
