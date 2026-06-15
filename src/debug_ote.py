"""
debug_ote.py - Diagnostic temps reel de la zone OTE Bv3
=======================================================
Affiche toutes les 5 minutes la distance entre le prix et la zone OTE.
Usage : python src\debug_ote.py
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import talib
from datetime import datetime, timezone
from collections import deque
from scipy.signal import argrelextrema

from config import config
from data_feed import IGDataFeed, EPICS, SCALES
from instruments import INSTRUMENTS
from warmup_loader import warmup_from_parquet
from data_feed import OHLCVCandle

INSTRUMENT = "XAUUSD"
inst = INSTRUMENTS[INSTRUMENT]

def analyse_ote(buffer):
    if len(buffer) < 100:
        return None
    buf   = list(buffer)
    n     = len(buf)
    i     = n - 1

    close = np.array([c.close for c in buf], dtype=np.float64)
    high  = np.array([c.high  for c in buf], dtype=np.float64)
    low   = np.array([c.low   for c in buf], dtype=np.float64)

    atr   = talib.ATR(high, low, close, 14)[-1]
    adx   = talib.ADX(high, low, close, 14)[-1]
    ma200 = talib.SMA(close, min(inst.ma_period, n-1))[-1]
    price = close[-1]

    if np.isnan(atr) or np.isnan(adx) or np.isnan(ma200):
        return None

    ma_threshold = ma200 - inst.ma_tolerance * atr

    sh_idx = argrelextrema(high, np.greater, order=inst.swing_order)[0]
    sl_idx = argrelextrema(low,  np.less,    order=inst.swing_order)[0]
    conf_sh = sh_idx[sh_idx < i - inst.swing_order]
    conf_sl = sl_idx[sl_idx < i - inst.swing_order]

    if len(conf_sh) < 1 or len(conf_sl) < 1:
        return None

    last_sh_i = conf_sh[-1]
    last_sl_i = conf_sl[-1]
    sh_v = high[last_sh_i]
    sl_v = low[last_sl_i]
    spread = sh_v - sl_v

    ote_lo = sl_v + 0.618 * spread
    ote_hi = sl_v + 0.786 * spread

    if price > ote_hi:
        dist_str = f"+{price - ote_hi:.2f} AU-DESSUS"
        pct = (price - ote_hi) / spread * 100
    elif price < ote_lo:
        dist_str = f"-{ote_lo - price:.2f} EN-DESSOUS"
        pct = -(ote_lo - price) / spread * 100
    else:
        dist_str = "*** DANS LA ZONE OTE ***"
        pct = 0

    trend = "LONG ✅" if price > ma_threshold else f"SHORT ❌ (prix {price:.2f} < MA-tol {ma_threshold:.2f})"
    adx_ok = "✅" if adx >= inst.adx_min else f"❌ ADX={adx:.1f} < {inst.adx_min}"
    session_ok = inst.session_start <= datetime.now(timezone.utc).hour < inst.session_end

    return {
        "ts":       datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "price":    price,
        "ma200":    ma200,
        "trend":    trend,
        "adx":      adx,
        "adx_ok":   adx_ok,
        "sh_v":     sh_v,
        "sl_v":     sl_v,
        "ote_lo":   ote_lo,
        "ote_hi":   ote_hi,
        "dist":     dist_str,
        "dist_pct": pct,
        "atr":      atr,
        "buf":      len(buffer),
        "session":  "ACTIVE ✅" if session_ok else f"HORS SESSION (active {inst.session_start}h-{inst.session_end}h UTC)",
    }


async def run():
    buf = deque(maxlen=2600)

    class FakeEngine:
        def __init__(self):
            self._buffer = buf
            class S:
                bar_count = 0
            self._state = S()

    eng = FakeEngine()
    print("Chargement Parquet...")
    n = await warmup_from_parquet(eng, instrument=INSTRUMENT, tf="M5")
    print(f"{n} barres historiques chargees.\n")

    feed = IGDataFeed(
        config        = config,
        subscriptions = [{"epic": inst.epic, "scale": "5MINUTE"}],
    )

    print(f"=== DIAGNOSTIC OTE {INSTRUMENT} EN TEMPS REEL ===")
    print(f"EPIC    : {inst.epic}")
    print(f"Session : {inst.session_start}h-{inst.session_end}h UTC | {inst.session_start+2}h-{inst.session_end+2}h Paris")
    print(f"OTE     : Fibonacci [0.618 - 0.786]")
    print(f"ADX min : {inst.adx_min}")
    print("=" * 55)
    print("Attente de la prochaine bougie M5 (max 5 min)...\n")

    bar_count = 0

    async def feed_task():
        nonlocal bar_count
        await feed.start()
        async for candle in feed.iter_candles():
            if candle.epic != inst.epic:
                continue
            buf.append(candle)
            bar_count += 1

            r = analyse_ote(buf)
            if r is None:
                print(f"{datetime.now(timezone.utc).strftime('%H:%M UTC')} | buffer insuffisant")
                continue

            print(f"\n{'='*55}")
            print(f"  {r['ts']} | bar #{bar_count}")
            print(f"  Prix          : {r['price']:.2f} EUR")
            print(f"  MA200 H1      : {r['ma200']:.2f} EUR  | Trend: {r['trend']}")
            print(f"  ADX           : {r['adx']:.1f}           | {r['adx_ok']}")
            print(f"  Swing High    : {r['sh_v']:.2f} | Low: {r['sl_v']:.2f}")
            print(f"  OTE zone      : [{r['ote_lo']:.2f} - {r['ote_hi']:.2f}]")
            print(f"  ATR           : {r['atr']:.2f}")
            print(f"  Distance OTE  : {r['dist']}")
            print(f"  Session       : {r['session']}")

            all_ok = (
                r['price'] > (r['ma200'] - inst.ma_tolerance * r['atr']) and
                r['adx'] >= inst.adx_min and
                r['ote_lo'] <= r['price'] <= r['ote_hi'] and
                inst.session_start <= datetime.now(timezone.utc).hour < inst.session_end
            )
            if all_ok:
                print(f"\n  *** SIGNAL POTENTIEL — Toutes les conditions reunies ! ***")

    try:
        await feed_task()
    except KeyboardInterrupt:
        pass
    finally:
        await feed.stop()
        print("\nDiagnostic arrete.")


if __name__ == "__main__":
    asyncio.run(run())
