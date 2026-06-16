"""
backtest_corr_s10.py - Valide les 3 ajustements scalp (filtre correlation CAC,
SL elargi, TP DAX -20%) sur historique S10 dual-instrument (DAX + CAC40).

Reconstruit en point-in-time (zero look-ahead) :
  - la tendance M5 du DAX (filtre with-trend + ATR M5 pour SL/TP) ;
  - la tendance M5 du CAC (filtre de correlation : pas de long DAX si CAC bear...).
Signal DAX = momentum 3xS10 contigus. SL/TP STATIQUES (on isole l'effet des reglages ;
le demo a en plus la gestion adaptative, non modelisee ici).
"""
from __future__ import annotations
import bisect, os
import numpy as np, pandas as pd
from datetime import datetime, timezone

from data_feed import OHLCVCandle
from indicators import compute_from_arrays, BUFFER_DEFAULT
from market_structure import StructureAnalyzer, MarketState
from mtf_service import _TFBuilder

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "historical")
SESSION = (7, 16)

def load(inst):
    df = pd.read_parquet(f"{DATA}/{inst}_S10.parquet")
    if "timestamp" in df.columns: df = df.set_index("timestamp")
    df.index = pd.to_datetime(df.index, utc=True); df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df[(df[["open","high","low","close"]] > 0).all(axis=1)]
    h = df.index.hour; df = df[(h >= SESSION[0]) & (h < SESSION[1])]
    return df

def s10_iter(df):
    o=df["open"].to_numpy(np.float64); h=df["high"].to_numpy(np.float64)
    l=df["low"].to_numpy(np.float64); c=df["close"].to_numpy(np.float64)
    v=(df["volume"].to_numpy(np.float64) if "volume" in df else np.zeros(len(df)))
    ep=df.index.values.astype("datetime64[s]").astype(np.int64)
    return o,h,l,c,v,ep

def cac_trend_timeline(inst="CAC40"):
    """Retourne (epochs[], trends[]) : tendance M5 CAC connue a chaque cloture M5 (point-in-time)."""
    df=load(inst); o,h,l,c,v,ep=s10_iter(df)
    ms=MarketState(); an=StructureAnalyzer(inst,"M5",ms.get(inst,"M5"),store=None,pivot=3)
    bld=_TFBuilder(300, inst, "M5")
    epochs=[]; trends=[]
    for i in range(len(c)):
        closed=bld.add(OHLCVCandle(epic=inst,scale="M5",
                 timestamp=datetime.fromtimestamp(int(ep[i]),tz=timezone.utc),
                 open=o[i],high=h[i],low=l[i],close=c[i],volume=v[i],is_complete=True))
        if closed is not None:
            an.on_closed_bar(closed, None)
            epochs.append(int(ep[i])); trends.append(an.state.trend)
    return epochs, trends

def trend_at(epochs, trends, t):
    j=bisect.bisect_right(epochs, t)-1
    return trends[j] if j>=0 else "range"

def run(sl_mult, tp_mult, corr_filter, cac_ep=None, cac_tr=None, spread=1.5, cooldown=6):
    df=load("DAX"); o,h,l,c,v,ep=s10_iter(df); n=len(c)
    ms=MarketState(); an=StructureAnalyzer("DAX","M5",ms.get("DAX","M5"),store=None,pivot=3)
    bld=_TFBuilder(300,"DAX","M5"); m5c=[]; m5h=[]; m5l=[]; atr=None
    pos=None; cd=0; trades=[]; base=0; blk_tr=0; blk_corr=0
    for i in range(n):
        # cloture M5 DAX -> structure + ATR
        closed=bld.add(OHLCVCandle(epic="DAX",scale="M5",
                 timestamp=datetime.fromtimestamp(int(ep[i]),tz=timezone.utc),
                 open=o[i],high=h[i],low=l[i],close=c[i],volume=v[i],is_complete=True))
        if closed is not None:
            m5c.append(closed.close); m5h.append(closed.high); m5l.append(closed.low)
            iset=compute_from_arrays(np.array(m5c[-BUFFER_DEFAULT:]),np.array(m5h[-BUFFER_DEFAULT:]),
                                     np.array(m5l[-BUFFER_DEFAULT:]),None)
            ind={"atr":iset.atr}; an.on_closed_bar(closed, ind)
            atr=iset.atr if (iset.atr==iset.atr and iset.atr>0) else atr  # garde si NaN
        # gestion position (SL/TP intrabar)
        if pos is not None:
            if pos[0]=='L':
                if l[i]<=pos[2]: trades.append(pos[2]-pos[1]-spread); pos=None
                elif h[i]>=pos[3]: trades.append(pos[3]-pos[1]-spread); pos=None
            else:
                if h[i]>=pos[2]: trades.append(pos[1]-pos[2]-spread); pos=None
                elif l[i]<=pos[3]: trades.append(pos[1]-pos[3]-spread); pos=None
            if pos is not None: continue
            cd=cooldown
        if cd>0: cd-=1; continue
        if i<2 or atr is None or atr<=0: continue
        # momentum 3xS10 contigus
        if ep[i]-ep[i-2]!=20: continue
        up=c[i]>o[i] and c[i-1]>o[i-1] and c[i-2]>o[i-2]
        dn=c[i]<o[i] and c[i-1]<o[i-1] and c[i-2]<o[i-2]
        if not(up or dn): continue
        base+=1
        trend=an.state.trend
        # with-trend M5
        if not((up and trend=="bull") or (dn and trend=="bear")): blk_tr+=1; continue
        # filtre correlation CAC
        if corr_filter:
            ct=trend_at(cac_ep,cac_tr,int(ep[i]))
            if (up and ct=="bear") or (dn and ct=="bull"): blk_corr+=1; continue
        e=c[i]
        if up: pos=('L',e,e-sl_mult*atr,e+tp_mult*atr)
        else:  pos=('S',e,e+sl_mult*atr,e-tp_mult*atr)
    a=np.array(trades)
    if not len(a): return "0 trade"
    w=a[a>0]; ls=a[a<=0]; pf=(w.sum()/-ls.sum()) if (len(ls) and ls.sum()<0) else float('inf')
    dd=0; peak=0; eq=0
    for x in a: eq+=x; peak=max(peak,eq); dd=max(dd,peak-eq)
    sh=(a.mean()/a.std(ddof=1)*np.sqrt(len(a))) if len(a)>1 and a.std(ddof=1)>0 else 0
    return (f"trades={len(a)} win={100*len(w)/len(a):.1f}% net={a.sum():.0f}pts PF={pf:.2f} "
            f"exp={a.mean():.2f} maxDD={dd:.0f} Sharpe={sh:.2f} | base={base} blkTrend={blk_tr} blkCorr={blk_corr}")

print("Construction timeline tendance CAC M5..."); cep,ctr=cac_trend_timeline()
print(f"  {len(cep)} cloture(s) M5 CAC")
print("\n=== DAX S10, with-trend M5 ===")
print("AVANT  (SL1.0/TP3.0, sans filtre corr) :", run(1.0,3.0,False,cep,ctr))
print("APRES  (SL1.5/TP2.4, +filtre corr CAC) :", run(1.5,2.4,True,cep,ctr))
print("--- isolation ---")
print("  +filtre corr seul (SL1.0/TP3.0)      :", run(1.0,3.0,True,cep,ctr))
print("  +SL1.5/TP2.4 seul (sans corr)        :", run(1.5,2.4,False,cep,ctr))
