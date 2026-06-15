"""
backtest_scalp_m1.py - Backtest evenementiel d'un scalping M1 sous contraintes.
==============================================================================
Strategie de base (fidele au scalper_10s live, transposee en M1) :
  - 3 bougies M1 CONSECUTIVES haussieres (close>open) -> signal LONG
  - 3 bougies M1 CONSECUTIVES baissieres              -> signal SHORT
  - SL/TP en ATR (R:R ~3) ; fallback en points fixes si l'ATR n'est pas encore
    disponible (warmup).

Contraintes ajoutees (cf demande) :
  1. TOUJOURS dans le sens de la TENDANCE M5 (structure reconstruite depuis le M1).
  2. CONTRE la tendance autorise UNIQUEMENT sur une VRAIE CASSURE du swing
     (retournement CHoCH : cloture au-dela du plus haut/bas) recente, et qui
     N'EST PAS un liquidity grab (le grab = faux-break/piege -> exclu).

Realisme "indicateurs pas toujours disponibles a la decision" :
  - Indicateurs M1 (RSI/ATR/EMA) calcules causalement (TA-Lib) -> NaN pendant le
    warmup ; les filtres correspondants sont alors IGNORES (on trade avec ce qui
    est disponible).
  - La structure M5 n'est connue qu'apres formation de swings ; tant qu'elle est
    inconnue, trend = "range" (pas de filtre tendance impossible a satisfaire).

ZERO LOOK-AHEAD :
  - Decision a la cloture de la barre M1 i (donnees <= i) ; entree au close de i.
  - SL/TP verifies sur les barres i+1.. (high/low intrabar).
  - Une M5 n'alimente la structure qu'a l'arrivee de la 1ere M1 du bucket suivant
    -> a la barre i (bucket B) la structure ne reflete que les M5 <= B-1.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd
import talib

from data_feed import OHLCVCandle
from indicators import compute_from_arrays, BUFFER_DEFAULT
from market_structure import StructureAnalyzer, MarketState

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "historical")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")


# ---------------------------------------------------------------------------
# Parametres
# ---------------------------------------------------------------------------
@dataclass
class ScalpParams:
    base_tf: str = "S10"              # timeframe du signal momentum (S10 par defaut)
    base_seconds: int = 10            # duree d'une bougie de base (garde de contiguite)
    momentum_bars: int = 3            # bougies consecutives meme sens
    sl_atr_mult: float = 1.0          # applique a l'ATR M5 (decision via M5)
    tp_atr_mult: float = 3.0
    sl_fixed_pts: float = 20.0        # fallback si ATR M5 indisponible (cf scalper_10s)
    tp_fixed_pts: float = 60.0
    spread_pts: float = 1.5           # cout aller-retour (DAX mini)
    eur_per_pt: float = 1.0
    cooldown_bars: int = 6            # apres une sortie (en bougies de base)
    rsi_max_long: float = 80.0        # filtre anti-chasse sur RSI M5 (ignore si None)
    rsi_min_short: float = 20.0
    ct_recency_m5: int = 3            # fraicheur max (en M5) d'un CHoCH pour contre-tendance
    apply_constraints: bool = True    # False = strategie de base seule (baseline)
    with_trend_only: bool = False     # True = uniquement dans le sens M5 (coupe contre-tendance ET range)
    session_start: int = 7            # heure UTC incluse
    session_end: int = 16             # heure UTC exclue
    session_filter: bool = True


@dataclass
class Trade:
    entry_ts: str
    exit_ts: str
    side: str            # 'long' | 'short'
    entry: float
    exit: float
    pnl_pts: float
    reason: str          # 'TP' | 'SL' | 'EOD'
    with_trend: bool
    counter_trend: bool
    trend_m5: str
    indicators_ready: bool   # tous les indicateurs M1 cles dispo a l'entree


@dataclass
class Result:
    label: str
    instrument: str
    tf: str
    bars: int
    period: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_pts: float = 0.0
    net_pts: float = 0.0
    net_eur: float = 0.0
    profit_factor: float = 0.0
    avg_win_pts: float = 0.0
    avg_loss_pts: float = 0.0
    expectancy_pts: float = 0.0
    max_dd_pts: float = 0.0
    sharpe_per_trade: float = 0.0
    with_trend_trades: int = 0
    with_trend_winrate: float = 0.0
    counter_trend_trades: int = 0
    counter_trend_winrate: float = 0.0
    trades_without_full_indicators: int = 0
    base_signals: int = 0            # signaux momentum bruts
    blocked_by_trend: int = 0        # rejetes par la contrainte de tendance
    blocked_by_rsi: int = 0
    _eur_per_pt: float = 1.0

    def finalize(self, trades: List[Trade]) -> None:
        self.trades = len(trades)
        wins = [t for t in trades if t.pnl_pts > 0]
        losses = [t for t in trades if t.pnl_pts <= 0]
        self.wins, self.losses = len(wins), len(losses)
        self.win_rate = 100.0 * self.wins / self.trades if self.trades else 0.0
        self.net_pts = sum(t.pnl_pts for t in trades)
        self.gross_pts = self.net_pts
        self.net_eur = self.net_pts * self._eur_per_pt
        gain = sum(t.pnl_pts for t in wins)
        loss = -sum(t.pnl_pts for t in losses)
        self.profit_factor = (gain / loss) if loss > 0 else float("inf") if gain > 0 else 0.0
        self.avg_win_pts = gain / len(wins) if wins else 0.0
        self.avg_loss_pts = -loss / len(losses) if losses else 0.0
        self.expectancy_pts = self.net_pts / self.trades if self.trades else 0.0
        # drawdown sur courbe d'equity (points cumules)
        eq = 0.0
        peak = 0.0
        max_dd = 0.0
        rets = []
        for t in trades:
            eq += t.pnl_pts
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
            rets.append(t.pnl_pts)
        self.max_dd_pts = max_dd
        if len(rets) > 1:
            arr = np.array(rets, dtype=np.float64)
            sd = arr.std(ddof=1)
            self.sharpe_per_trade = float(arr.mean() / sd * math.sqrt(len(arr))) if sd > 0 else 0.0
        wt = [t for t in trades if t.with_trend]
        ct = [t for t in trades if t.counter_trend]
        self.with_trend_trades = len(wt)
        self.with_trend_winrate = 100.0 * sum(1 for t in wt if t.pnl_pts > 0) / len(wt) if wt else 0.0
        self.counter_trend_trades = len(ct)
        self.counter_trend_winrate = 100.0 * sum(1 for t in ct if t.pnl_pts > 0) / len(ct) if ct else 0.0
        self.trades_without_full_indicators = sum(1 for t in trades if not t.indicators_ready)

    def print_report(self) -> None:
        print(f"\n===== {self.label} | {self.instrument} {self.tf} | {self.period} =====")
        print(f"barres={self.bars} | signaux momentum bruts={self.base_signals}")
        print(f"  bloques par tendance={self.blocked_by_trend} | bloques par RSI={self.blocked_by_rsi}")
        print(f"trades={self.trades} | win={self.win_rate:.1f}% ({self.wins}W/{self.losses}L)")
        print(f"net={self.net_pts:.1f} pts ({self.net_eur:.1f} EUR) | PF={self.profit_factor:.2f} | "
              f"expectancy={self.expectancy_pts:.2f} pts/trade")
        print(f"avg win={self.avg_win_pts:.1f} | avg loss={self.avg_loss_pts:.1f} | "
              f"maxDD={self.max_dd_pts:.1f} pts | Sharpe/trade={self.sharpe_per_trade:.2f}")
        print(f"avec-tendance: {self.with_trend_trades} trades, win {self.with_trend_winrate:.1f}%")
        print(f"contre-tendance: {self.counter_trend_trades} trades, win {self.counter_trend_winrate:.1f}%")
        print(f"trades pris AVEC indicateurs incomplets (realisme warmup): "
              f"{self.trades_without_full_indicators}")


# ---------------------------------------------------------------------------
# Donnees
# ---------------------------------------------------------------------------
def load_base(instrument: str, p: ScalpParams) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, f"{instrument}_{p.base_tf}.parquet")
    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    if "volume" in df.columns:
        df["volume"] = df["volume"].fillna(0.0)
    else:
        df["volume"] = 0.0
    if p.session_filter:
        h = df.index.hour
        df = df[(h >= p.session_start) & (h < p.session_end)]
    return df[["open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# Moteur evenementiel
# ---------------------------------------------------------------------------
def run(instrument: str, p: ScalpParams, label: str) -> tuple[Result, List[Trade]]:
    df = load_base(instrument, p)
    n = len(df)
    o = df["open"].to_numpy(np.float64)
    h = df["high"].to_numpy(np.float64)
    l = df["low"].to_numpy(np.float64)
    c = df["close"].to_numpy(np.float64)
    vol = df["volume"].to_numpy(np.float64)
    epoch = df.index.values.astype("datetime64[s]").astype(np.int64)
    pyts = df.index.to_pydatetime()

    # DECISION VIA M5 : les indicateurs (ATR/RSI) viennent de la derniere M5
    # CLOTUREE (point-in-time), pas du S10. Ils sont indisponibles (None) tant que
    # la M5 n'a pas assez d'historique -> SL/TP bascule en points fixes, filtre
    # RSI ignore (realisme "indicateur pas dispo a la decision").
    # Structure M5 reconstruite depuis les bougies de base (point-in-time).
    ms = MarketState()
    analyzer = StructureAnalyzer(instrument, "M5", ms.get(instrument, "M5"),
                                 store=None, pivot=3)
    m5_count = 0
    last_choch_dir = None        # 'bullish' | 'bearish'
    last_choch_m5 = -10**9
    # M5 en cours de formation
    cur_bucket: Optional[int] = None
    m5_o = m5_h = m5_l = m5_c = 0.0
    m5_vol = 0.0
    m5_ts = None
    m5_closes: List[float] = []
    m5_highs: List[float] = []
    m5_lows: List[float] = []
    m5_vols: List[float] = []

    def close_m5():
        nonlocal m5_count, last_choch_dir, last_choch_m5
        m5_closes.append(m5_c); m5_highs.append(m5_h)
        m5_lows.append(m5_l); m5_vols.append(m5_vol)
        # indicateurs M5 point-in-time sur fenetre glissante (comme le buffer live)
        w = BUFFER_DEFAULT
        cl = np.array(m5_closes[-w:], np.float64)
        hi = np.array(m5_highs[-w:], np.float64)
        lo = np.array(m5_lows[-w:], np.float64)
        vo = np.array(m5_vols[-w:], np.float64)
        iset = compute_from_arrays(cl, hi, lo, vo)
        def _v(x):
            return None if (isinstance(x, float) and math.isnan(x)) else x
        ind = {"rsi": _v(iset.rsi), "atr": _v(iset.atr),
               "ema_fast": _v(iset.ema_fast), "ema_slow": _v(iset.ema_slow)}
        candle = OHLCVCandle(epic=instrument, scale="M5", timestamp=m5_ts,
                             open=m5_o, high=m5_h, low=m5_l, close=m5_c,
                             volume=m5_vol, is_complete=True)
        events = analyzer.on_closed_bar(candle, ind)
        m5_count += 1
        for ev in events:
            if ev["type"] == "reversal":
                last_choch_dir = ev["direction"]
                last_choch_m5 = m5_count

    res = Result(label=label, instrument=instrument, tf=f"{p.base_tf}+M5", bars=n,
                 period=f"{df.index.min()} -> {df.index.max()}")
    res._eur_per_pt = p.eur_per_pt
    trades: List[Trade] = []

    pos = None           # dict si position ouverte
    cooldown = 0

    for i in range(n):
        bucket = int(epoch[i] // 300)
        # 1) cloture de la M5 precedente a l'arrivee de la 1ere M1 du nouveau bucket
        if cur_bucket is None:
            cur_bucket = bucket
            m5_o = o[i]; m5_h = h[i]; m5_l = l[i]; m5_c = c[i]
            m5_vol = vol[i]; m5_ts = pyts[i].replace(second=0, microsecond=0)
        elif bucket != cur_bucket:
            close_m5()
            cur_bucket = bucket
            m5_o = o[i]; m5_h = h[i]; m5_l = l[i]; m5_c = c[i]
            m5_vol = vol[i]
            # timestamp aligne sur le debut du bucket
            m5_ts = datetime.fromtimestamp(bucket * 300, tz=timezone.utc)
        else:
            if h[i] > m5_h: m5_h = h[i]
            if l[i] < m5_l: m5_l = l[i]
            m5_c = c[i]
            m5_vol += vol[i]

        # 2) gestion d'une position ouverte : SL/TP intrabar sur CETTE barre
        if pos is not None:
            exited = False
            if pos["side"] == "long":
                if l[i] <= pos["sl"]:
                    _close_trade(pos, pos["sl"], "SL", pyts[i], trades, p); exited = True
                elif h[i] >= pos["tp"]:
                    _close_trade(pos, pos["tp"], "TP", pyts[i], trades, p); exited = True
            else:
                if h[i] >= pos["sl"]:
                    _close_trade(pos, pos["sl"], "SL", pyts[i], trades, p); exited = True
                elif l[i] <= pos["tp"]:
                    _close_trade(pos, pos["tp"], "TP", pyts[i], trades, p); exited = True
            if exited:
                pos = None
                cooldown = p.cooldown_bars
            else:
                continue  # tant qu'on est en position on ne re-rentre pas

        if cooldown > 0:
            cooldown -= 1
            continue

        # 3) signal momentum sur la barre i (donnees <= i, zero look-ahead)
        if i < p.momentum_bars:
            continue
        j0 = i - p.momentum_bars + 1
        # garde de contiguite : les N bougies doivent etre reellement consecutives
        # (pas a cheval sur un gap de session / overnight).
        if epoch[i] - epoch[j0] != (p.momentum_bars - 1) * p.base_seconds:
            continue
        up = all(c[j] > o[j] for j in range(j0, i + 1))
        dn = all(c[j] < o[j] for j in range(j0, i + 1))
        if not (up or dn):
            continue
        res.base_signals += 1
        side = "long" if up else "short"

        trend = analyzer.state.trend           # 'bull' | 'bear' | 'range'
        m5_ind = analyzer.state.last_indicators   # ATR/RSI M5 (None si indispo)
        with_trend = (side == "long" and trend == "bull") or (side == "short" and trend == "bear")
        against = (side == "long" and trend == "bear") or (side == "short" and trend == "bull")

        if p.apply_constraints:
            if p.with_trend_only:
                # uniquement dans le sens de la tendance M5 : coupe le
                # contre-tendance ET le range (pas de tendance claire a suivre).
                if not with_trend:
                    res.blocked_by_trend += 1
                    continue
            elif against:
                # contre-tendance : exige un CHoCH recent dans le sens du trade,
                # ET pas un faux-break (le CHoCH = vraie cassure, mutuellement
                # exclusif d'un liquidity grab par construction).
                want = "bullish" if side == "long" else "bearish"
                fresh = (m5_count - last_choch_m5) <= p.ct_recency_m5
                if not (last_choch_dir == want and fresh):
                    res.blocked_by_trend += 1
                    continue
            elif not with_trend:
                # trend == range : pas de tendance a suivre -> on autorise le
                # momentum de base (aucun conflit de tendance).
                pass
            # filtre RSI anti-chasse sur le RSI M5, applique UNIQUEMENT si dispo.
            r = m5_ind.get("rsi")
            if r is not None:
                if side == "long" and r > p.rsi_max_long:
                    res.blocked_by_rsi += 1; continue
                if side == "short" and r < p.rsi_min_short:
                    res.blocked_by_rsi += 1; continue

        # SL/TP : ATR M5 si dispo, sinon points fixes (indicateur indisponible)
        a_m5 = m5_ind.get("atr")
        if a_m5 is None or a_m5 <= 0:
            sl_d, tp_d = p.sl_fixed_pts, p.tp_fixed_pts
        else:
            sl_d, tp_d = p.sl_atr_mult * a_m5, p.tp_atr_mult * a_m5
        entry = c[i]
        if side == "long":
            sl, tp = entry - sl_d, entry + tp_d
        else:
            sl, tp = entry + sl_d, entry - tp_d

        ind_ready = (m5_ind.get("atr") is not None and m5_ind.get("rsi") is not None)
        pos = {
            "side": side, "entry": entry, "sl": sl, "tp": tp,
            "entry_ts": pyts[i], "with_trend": with_trend,
            "counter_trend": against and p.apply_constraints,
            "trend_m5": trend, "indicators_ready": ind_ready,
        }

    res.finalize(trades)
    return res, trades


def _close_trade(pos: dict, exit_price: float, reason: str, exit_ts,
                 trades: List[Trade], p: ScalpParams) -> None:
    if pos["side"] == "long":
        pnl = (exit_price - pos["entry"])
    else:
        pnl = (pos["entry"] - exit_price)
    pnl -= p.spread_pts                      # cout aller-retour
    trades.append(Trade(
        entry_ts=pos["entry_ts"].isoformat(), exit_ts=exit_ts.isoformat(),
        side=pos["side"], entry=pos["entry"], exit=exit_price,
        pnl_pts=pnl, reason=reason,
        with_trend=pos["with_trend"], counter_trend=pos["counter_trend"],
        trend_m5=pos["trend_m5"], indicators_ready=pos["indicators_ready"],
    ))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="DAX")
    ap.add_argument("--no-session", action="store_true", help="desactive le filtre de session")
    ap.add_argument("--sl", type=float, default=1.0)
    ap.add_argument("--tp", type=float, default=3.0)
    ap.add_argument("--spread", type=float, default=1.5)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    base = ScalpParams(sl_atr_mult=args.sl, tp_atr_mult=args.tp, spread_pts=args.spread,
                       session_filter=not args.no_session)

    # Baseline : momentum seul (sans contraintes)
    p_base = ScalpParams(**{**asdict(base), "apply_constraints": False})
    res_base, tr_base = run(args.instrument, p_base, "BASELINE momentum 3 bougies")
    res_base.print_report()

    # Avec contraintes tendance M5 + CHoCH non-grab (contre-tendance autorise)
    p_cons = ScalpParams(**{**asdict(base), "apply_constraints": True})
    res_cons, tr_cons = run(args.instrument, p_cons, "CONTRAINTES tendance M5 + CHoCH non-grab")
    res_cons.print_report()

    # Avec-tendance UNIQUEMENT (coupe contre-tendance + range)
    p_wt = ScalpParams(**{**asdict(base), "apply_constraints": True, "with_trend_only": True})
    res_wt, tr_wt = run(args.instrument, p_wt, "AVEC-TENDANCE UNIQUEMENT (sens M5)")
    res_wt.print_report()

    if args.save:
        os.makedirs(OUT_DIR, exist_ok=True)
        stamp = res_cons.period.split(" ")[0]
        for res, trs, tag in ((res_base, tr_base, "baseline"), (res_cons, tr_cons, "constrained"),
                              (res_wt, tr_wt, "withtrend")):
            base_path = os.path.join(OUT_DIR, f"scalp_m1_{args.instrument}_{tag}")
            with open(base_path + "_metrics.json", "w") as f:
                json.dump(asdict(res), f, indent=2, default=str)
            pd.DataFrame([asdict(t) for t in trs]).to_csv(base_path + "_trades.csv", index=False)
        print(f"\nrapports ecrits dans {OUT_DIR}")


if __name__ == "__main__":
    main()
