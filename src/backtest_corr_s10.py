"""
backtest_corr_s10.py - Backtest scalp DAX S10 (with-trend M5 + filtre correlation CAC),
single-leg vs MULTI-JAMBES (TP echelonnes + SL remonte apres TP1).
Tendances M5 DAX & CAC reconstruites point-in-time (zero look-ahead).
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
SESSION = (7, 16); SPREAD = 1.5

def load(inst):
    df = pd.read_parquet(f"{DATA}/{inst}_S10.parquet")
    if "timestamp" in df.columns: df = df.set_index("timestamp")
    df.index = pd.to_datetime(df.index, utc=True); df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df[(df[["open","high","low","close"]] > 0).all(axis=1)]
    h = df.index.hour; df = df[(h >= SESSION[0]) & (h < SESSION[1])]
    return df

def arrs(df):
    return (df["open"].to_numpy(np.float64), df["high"].to_numpy(np.float64),
            df["low"].to_numpy(np.float64), df["close"].to_numpy(np.float64),
            (df["volume"].to_numpy(np.float64) if "volume" in df else np.zeros(len(df))),
            df.index.values.astype("datetime64[s]").astype(np.int64))

def cac_trend_timeline(inst="CAC40"):
    df=load(inst); o,h,l,c,v,ep=arrs(df)
    ms=MarketState(); an=StructureAnalyzer(inst,"M5",ms.get(inst,"M5"),store=None,pivot=3)
    bld=_TFBuilder(300,inst,"M5"); E=[]; T=[]
    for i in range(len(c)):
        cl=bld.add(OHLCVCandle(epic=inst,scale="M5",timestamp=datetime.fromtimestamp(int(ep[i]),tz=timezone.utc),
                   open=o[i],high=h[i],low=l[i],close=c[i],volume=v[i],is_complete=True))
        if cl is not None: an.on_closed_bar(cl,None); E.append(int(ep[i])); T.append(an.state.trend)
    return E,T
def trend_at(E,T,t):
    j=bisect.bisect_right(E,t)-1; return T[j] if j>=0 else "range"

def replay(o,h,l,c,i0,long,entry,atr,cfg):
    """Retourne (pnl_points_position, exit_index). single ou multi-jambes."""
    n=len(c)
    if cfg["mode"]=="single":
        sl=entry-cfg["sl"]*atr if long else entry+cfg["sl"]*atr
        tp=entry+cfg["tp"]*atr if long else entry-cfg["tp"]*atr
        for i in range(i0,n):
            if long:
                if l[i]<=sl: return (sl-entry)-SPREAD,i
                if h[i]>=tp: return (tp-entry)-SPREAD,i
            else:
                if h[i]>=sl: return (entry-sl)-SPREAD,i
                if l[i]<=tp: return (entry-tp)-SPREAD,i
        last=c[-1]; return ((last-entry) if long else (entry-last))-SPREAD,n-1
    # ---- multi-jambes ----
    tps=cfg["tps"]; sz=1.0/len(tps); legs=[True]*len(tps)
    sl=entry-cfg["sl"]*atr if long else entry+cfg["sl"]*atr
    tp1=False; pnl=0.0
    for i in range(i0,n):
        # SL (toutes jambes ouvertes)
        if (l[i]<=sl) if long else (h[i]>=sl):
            g=(sl-entry) if long else (entry-sl)
            pnl+=sum((g-SPREAD)*sz for k in range(len(legs)) if legs[k])
            return pnl,i
        # TP par jambe
        for k in range(len(legs)):
            if not legs[k]: continue
            tppx=entry+tps[k]*atr if long else entry-tps[k]*atr
            if (h[i]>=tppx) if long else (l[i]<=tppx):
                g=(tppx-entry) if long else (entry-tppx)
                pnl+=(g-SPREAD)*sz; legs[k]=False
                if k==0 and not tp1:   # TP1 atteint -> SL remonte au-dessus de l'ouverture
                    tp1=True
                    sl=entry+cfg["lock"]*atr if long else entry-cfg["lock"]*atr
        if not any(legs): return pnl,i
    last=c[-1]
    for k in range(len(legs)):
        if legs[k]:
            g=(last-entry) if long else (entry-last); pnl+=(g-SPREAD)*sz
    return pnl,n-1

def run(cfg, cep, ctr, corr_filter=True, cooldown=6):
    df=load("DAX"); o,h,l,c,v,ep=arrs(df); n=len(c)
    ms=MarketState(); an=StructureAnalyzer("DAX","M5",ms.get("DAX","M5"),store=None,pivot=3)
    bld=_TFBuilder(300,"DAX","M5"); m5c=[]; m5h=[]; m5l=[]; atr=None
    trades=[]; next_free=-1
    for i in range(n):
        cl=bld.add(OHLCVCandle(epic="DAX",scale="M5",timestamp=datetime.fromtimestamp(int(ep[i]),tz=timezone.utc),
                   open=o[i],high=h[i],low=l[i],close=c[i],volume=v[i],is_complete=True))
        if cl is not None:
            m5c.append(cl.close); m5h.append(cl.high); m5l.append(cl.low)
            iset=compute_from_arrays(np.array(m5c[-BUFFER_DEFAULT:]),np.array(m5h[-BUFFER_DEFAULT:]),np.array(m5l[-BUFFER_DEFAULT:]),None)
            an.on_closed_bar(cl,{"atr":iset.atr})
            if iset.atr==iset.atr and iset.atr>0: atr=iset.atr
        if i<=next_free or i<2 or atr is None or atr<=0: continue
        if ep[i]-ep[i-2]!=20: continue
        up=c[i]>o[i] and c[i-1]>o[i-1] and c[i-2]>o[i-2]
        dn=c[i]<o[i] and c[i-1]<o[i-1] and c[i-2]<o[i-2]
        if not(up or dn): continue
        tr=an.state.trend
        if not((up and tr=="bull") or (dn and tr=="bear")): continue
        if corr_filter:
            ct=trend_at(cep,ctr,int(ep[i]))
            if (up and ct=="bear") or (dn and ct=="bull"): continue
        pnl,xi=replay(o,h,l,c,i+1,up,c[i],atr,cfg)
        trades.append(pnl); next_free=xi+cooldown
    a=np.array(trades)
    if not len(a): return "0 trade"
    w=a[a>0]; ls=a[a<=0]; pf=(w.sum()/-ls.sum()) if (len(ls) and ls.sum()<0) else float('inf')
    dd=peak=eq=0.0
    for x in a: eq+=x; peak=max(peak,eq); dd=max(dd,peak-eq)
    sh=(a.mean()/a.std(ddof=1)*np.sqrt(len(a))) if len(a)>1 and a.std(ddof=1)>0 else 0
    return f"trades={len(a)} win={100*len(w)/len(a):.1f}% net={a.sum():.0f}pts PF={pf:.2f} exp={a.mean():.2f} maxDD={dd:.0f} Sharpe={sh:.2f}"

print("Timeline tendance CAC M5..."); cep,ctr=cac_trend_timeline(); print(f"  {len(cep)} closes")
print("\n=== DAX S10, with-trend M5 + filtre correlation CAC ===")
print("VALIDE   single SL1.0/TP3.0          :", run({"mode":"single","sl":1.0,"tp":3.0}, cep,ctr))
print("PROPOSE  3-jambes TP3/4.5/6 SL2 lock1 :", run({"mode":"multi","sl":2.0,"tps":[3.0,4.5,6.0],"lock":1.0}, cep,ctr))
print("variante 3-jambes lock0.5            :", run({"mode":"multi","sl":2.0,"tps":[3.0,4.5,6.0],"lock":0.5}, cep,ctr))
print("variante 3-jambes lock0 (break-even) :", run({"mode":"multi","sl":2.0,"tps":[3.0,4.5,6.0],"lock":0.0}, cep,ctr))
print("LIVE     2-jambes TP3/6 SL2 lock0.5   :", run({"mode":"multi","sl":2.0,"tps":[3.0,6.0],"lock":0.5}, cep,ctr))
print("variante 2-jambes TP3/6 lock1.0       :", run({"mode":"multi","sl":2.0,"tps":[3.0,6.0],"lock":1.0}, cep,ctr))
print("variante 2-jambes TP3/6 lock0 (BE)    :", run({"mode":"multi","sl":2.0,"tps":[3.0,6.0],"lock":0.0}, cep,ctr))
print("ref      single SL2.0/TP6.0           :", run({"mode":"single","sl":2.0,"tp":6.0}, cep,ctr))
