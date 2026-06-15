"""
dashboard.py - IHM web Bonaza (Streamlit + Plotly)
====================================================
Vue d'ensemble visuelle du systeme Bonaza : bougies XAUUSD/DAX/CAC40 avec
indicateurs techniques, etat du compte IG, decisions de l'agent IA, logs.

Lancement :
    .\\bonaza_shell.bat
    streamlit run src/dashboard.py

Le navigateur s'ouvre automatiquement sur http://localhost:8501

Sources de donnees :
  - Bougies : Parquet historique data/historical/<INSTRUMENT>_M5.parquet
  - Signaux/trades : SQLite data/bonaza.db
  - Decisions IA : tail des logs JSON loguru
  - Compte IG : appel REST optionnel (toggle 'Live IG' dans la sidebar)
    [ATTENTION] : si main.py tourne, n'ACTIVER PAS 'Live IG' (conflit session)
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time as time_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import talib
from plotly.subplots import make_subplots

# Permettre import depuis src/
sys.path.insert(0, os.path.dirname(__file__))

ROOT      = Path(__file__).parent.parent
DATA_DIR  = ROOT / "data"
HIST_DIR  = DATA_DIR / "historical"
DB_PATH   = DATA_DIR / "bonaza.db"
LOG_DIR   = ROOT / "logs"
REPORTS   = DATA_DIR / "reports"

INSTRUMENTS = {
    "XAUUSD": {"emoji": "🥇", "epic": "CS.D.CFEGOLD.CFE.IP", "decimals": 2},
    "DAX":    {"emoji": "🇩🇪", "epic": "IX.D.DAX.IFMM.IP",    "decimals": 1},
    "CAC40":  {"emoji": "🇫🇷", "epic": "IX.D.CAC.IMF.IP",     "decimals": 1},
}


# =======================================================================
# Helpers donnees
# =======================================================================

@st.cache_data(ttl=30)
def load_parquet(instrument: str, n_bars: int = 200) -> Optional[pd.DataFrame]:
    """Charge les N dernieres bougies M5 depuis le Parquet historique."""
    p = HIST_DIR / f"{instrument}_M5.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index().tail(n_bars)


@st.cache_data(ttl=30)
def load_signals(limit: int = 50) -> pd.DataFrame:
    """Lit les derniers signaux depuis SQLite."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT ts, mode, direction, entry, stop_loss, take_profit, "
            "rr_ratio, setup_name, instrument, reason "
            f"FROM signals ORDER BY id DESC LIMIT {int(limit)}",
            conn,
        )
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


@st.cache_data(ttl=30)
def load_trades(limit: int = 50) -> pd.DataFrame:
    """Lit les derniers trades fermes depuis SQLite."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT position_id, ts_open, ts_close, direction, size, "
            "entry_price, sl_price, tp_price, exit_price, exit_reason, "
            "pnl_eur, duration_min, status "
            f"FROM trades ORDER BY id DESC LIMIT {int(limit)}",
            conn,
        )
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


@st.cache_data(ttl=20)
def load_all_trades_unified(instrument: Optional[str] = None,
                            limit: int = 200) -> pd.DataFrame:
    """Trades CLOSED + OPEN unifies. Instrument via JOIN signals.id = trades.signal_id.

    Retourne DataFrame avec colonnes : id, position_id, signal_id, ts_open, ts_close,
    direction, size, entry_price, sl_price, tp_price, exit_price, exit_reason,
    pnl_eur, duration_min, status, instrument.
    """
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    try:
        q = (
            "SELECT t.id, t.position_id, t.signal_id, t.ts_open, t.ts_close, "
            "t.direction, t.size, t.entry_price, t.sl_price, t.tp_price, "
            "t.exit_price, t.exit_reason, t.pnl_eur, t.duration_min, t.status, "
            "COALESCE(s.instrument, 'XAUUSD') AS instrument "
            "FROM trades t LEFT JOIN signals s ON s.id = t.signal_id "
        )
        params: List = []
        if instrument:
            q += "WHERE COALESCE(s.instrument, 'XAUUSD') = ? "
            params.append(instrument)
        q += f"ORDER BY t.id DESC LIMIT {int(limit)}"
        df = pd.read_sql_query(q, conn, params=params)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


@st.cache_data(ttl=15)
def tail_log_ai(n_recent: int = 200) -> List[dict]:
    """Tail le log du jour, extrait les entrees AI (decisions et signaux)."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"bonaza_{today}.log"
    if not log_path.exists():
        # Fallback : log d'hier si pas encore de log aujourd'hui
        yest = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        log_path = LOG_DIR / f"bonaza_{yest}.log"
        if not log_path.exists():
            return []
    items = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    j = json.loads(line)
                    msg = j["record"]["message"]
                    if "[AI]" in msg or "PAPER" in msg:
                        items.append({
                            "ts":    j["record"]["time"]["repr"][:19],
                            "level": j["record"]["level"]["name"],
                            "msg":   msg,
                        })
                except Exception:
                    continue
    except Exception:
        pass
    return items[-n_recent:]


@st.cache_data(ttl=10)
def tail_log_all(n: int = 200) -> List[dict]:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"bonaza_{today}.log"
    if not log_path.exists():
        return []
    items = []
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-n:]:
            try:
                j = json.loads(line)
                items.append({
                    "ts":    j["record"]["time"]["repr"][:19],
                    "level": j["record"]["level"]["name"],
                    "msg":   j["record"]["message"],
                })
            except Exception:
                continue
    except Exception:
        pass
    return items


# =======================================================================
# Indicateurs techniques
# =======================================================================

def compute_indicators(df: pd.DataFrame) -> dict:
    close = df["close"].values.astype(np.float64)
    high  = df["high"].values.astype(np.float64)
    low   = df["low"].values.astype(np.float64)
    n     = len(close)
    out = {}
    out["ema20"] = talib.EMA(close, min(20, n - 1))
    out["ema50"] = talib.EMA(close, min(50, n - 1))
    out["sma200"] = talib.SMA(close, min(200, n - 1))
    out["rsi14"]  = talib.RSI(close, min(14, n - 1))
    macd, sig, hist = talib.MACD(close, 12, 26, 9)
    out["macd"]      = macd
    out["macd_sig"]  = sig
    out["macd_hist"] = hist
    bb_u, bb_m, bb_l = talib.BBANDS(close, min(20, n - 1), 2.0, 2.0)
    out["bb_u"]   = bb_u
    out["bb_m"]   = bb_m
    out["bb_l"]   = bb_l
    out["atr14"]  = talib.ATR(high, low, close, min(14, n - 1))
    out["adx14"]  = talib.ADX(high, low, close, min(14, n - 1))
    return out


# =======================================================================
# Charts Plotly
# =======================================================================

def chart_candlestick(df: pd.DataFrame, ind: dict, title: str, show_ind: bool) -> go.Figure:
    rows = 3 if show_ind else 1
    row_heights = [0.55, 0.20, 0.25] if show_ind else [1.0]
    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        row_heights=row_heights, vertical_spacing=0.03,
        subplot_titles=[title, "RSI(14)", "MACD"] if show_ind else [title],
    )

    # Candlesticks
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name="OHLC",
    ), row=1, col=1)

    if show_ind:
        # EMA / BB overlay
        fig.add_trace(go.Scatter(x=df.index, y=ind["ema20"],
                                 name="EMA20", line=dict(width=1, color="orange")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=ind["ema50"],
                                 name="EMA50", line=dict(width=1, color="purple")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=ind["sma200"],
                                 name="SMA200", line=dict(width=1.5, color="red", dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=ind["bb_u"],
                                 name="BB upper", line=dict(width=1, color="lightgray", dash="dash"),
                                 showlegend=False), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=ind["bb_l"],
                                 name="BB lower", line=dict(width=1, color="lightgray", dash="dash"),
                                 showlegend=False, fill="tonexty", fillcolor="rgba(200,200,200,0.1)"), row=1, col=1)

        # RSI
        fig.add_trace(go.Scatter(x=df.index, y=ind["rsi14"],
                                 name="RSI", line=dict(color="blue")), row=2, col=1)
        fig.add_hline(y=70, line_dash="dash", line_color="red", row=2, col=1)
        fig.add_hline(y=30, line_dash="dash", line_color="green", row=2, col=1)

        # MACD
        fig.add_trace(go.Bar(x=df.index, y=ind["macd_hist"], name="MACD hist",
                             marker_color=["green" if h > 0 else "red" for h in ind["macd_hist"]]), row=3, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=ind["macd"], name="MACD",
                                 line=dict(color="blue")), row=3, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=ind["macd_sig"], name="signal",
                                 line=dict(color="orange")), row=3, col=1)

    fig.update_layout(
        height=750 if show_ind else 500,
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", y=1.02, x=0),
        hovermode="x unified",
    )
    return fig


def add_trade_markers(fig: go.Figure, trades: pd.DataFrame,
                      x_min, x_max, decimals: int) -> int:
    """Ajoute sur la 1re ligne du fig :
      - fleche entry (triangle-up vert LONG / triangle-down rouge SHORT)
      - ligne pointillee entry -> exit (vert gain / rouge perte) si CLOSED
      - lignes horizontales SL (rouge) et TP (vert) si OPEN, jusqu'au bord droit
      - x sortie (vert / rouge) si CLOSED

    Filtre les trades dont ts_open est dans [x_min, x_max].
    Retourne le nb de trades affiches.
    """
    if trades is None or trades.empty:
        return 0

    t = trades.copy()
    t["ts_open_dt"]  = pd.to_datetime(t["ts_open"],  utc=True, errors="coerce")
    t["ts_close_dt"] = pd.to_datetime(t["ts_close"], utc=True, errors="coerce")
    t = t[(t["ts_open_dt"] >= x_min) & (t["ts_open_dt"] <= x_max)]
    if t.empty:
        return 0

    long_x,  long_y,  long_text  = [], [], []
    short_x, short_y, short_text = [], [], []
    exit_w_x, exit_w_y = [], []   # sorties gagnantes
    exit_l_x, exit_l_y = [], []   # sorties perdantes / neutres

    for _, r in t.iterrows():
        is_open = (r["status"] == "OPEN")
        pnl = r["pnl_eur"] if pd.notna(r["pnl_eur"]) else 0.0

        hover = (
            f"#{int(r['id'])} {r['direction']} {r['status']}<br>"
            f"entry={r['entry_price']:.{decimals}f}<br>"
            f"SL={r['sl_price']:.{decimals}f} | TP={r['tp_price']:.{decimals}f}<br>"
            f"size={r['size']}<br>"
        )
        if is_open:
            hover += "EN COURS"
        else:
            exit_val = r["exit_price"] if pd.notna(r["exit_price"]) else r["entry_price"]
            hover += (f"exit={exit_val:.{decimals}f}<br>"
                      f"pnl={pnl:+.2f} EUR<br>"
                      f"raison={r['exit_reason'] or '?'}")

        if r["direction"] == "LONG":
            long_x.append(r["ts_open_dt"]); long_y.append(r["entry_price"])
            long_text.append(hover)
        else:
            short_x.append(r["ts_open_dt"]); short_y.append(r["entry_price"])
            short_text.append(hover)

        if not is_open and pd.notna(r["ts_close_dt"]) and pd.notna(r["exit_price"]):
            color_line = "#27ae60" if pnl > 0 else "#c0392b" if pnl < 0 else "#7f8c8d"
            fig.add_shape(
                type="line",
                x0=r["ts_open_dt"], x1=r["ts_close_dt"],
                y0=r["entry_price"], y1=r["exit_price"],
                line=dict(color=color_line, width=1.2, dash="dot"),
                row=1, col=1,
            )
            if pnl > 0:
                exit_w_x.append(r["ts_close_dt"]); exit_w_y.append(r["exit_price"])
            else:
                exit_l_x.append(r["ts_close_dt"]); exit_l_y.append(r["exit_price"])
        elif is_open:
            fig.add_shape(
                type="line",
                x0=r["ts_open_dt"], x1=x_max,
                y0=r["sl_price"], y1=r["sl_price"],
                line=dict(color="#c0392b", width=1, dash="dash"),
                row=1, col=1,
            )
            fig.add_shape(
                type="line",
                x0=r["ts_open_dt"], x1=x_max,
                y0=r["tp_price"], y1=r["tp_price"],
                line=dict(color="#27ae60", width=1, dash="dash"),
                row=1, col=1,
            )

    if long_x:
        fig.add_trace(go.Scatter(
            x=long_x, y=long_y, mode="markers",
            marker=dict(symbol="triangle-up", size=12, color="#2ecc71",
                        line=dict(width=1, color="black")),
            name="Entrée LONG", text=long_text,
            hovertemplate="%{text}<extra></extra>",
        ), row=1, col=1)
    if short_x:
        fig.add_trace(go.Scatter(
            x=short_x, y=short_y, mode="markers",
            marker=dict(symbol="triangle-down", size=12, color="#e74c3c",
                        line=dict(width=1, color="black")),
            name="Entrée SHORT", text=short_text,
            hovertemplate="%{text}<extra></extra>",
        ), row=1, col=1)
    if exit_w_x:
        fig.add_trace(go.Scatter(
            x=exit_w_x, y=exit_w_y, mode="markers",
            marker=dict(symbol="x", size=10, color="#27ae60", line=dict(width=2)),
            name="Sortie gain",
        ), row=1, col=1)
    if exit_l_x:
        fig.add_trace(go.Scatter(
            x=exit_l_x, y=exit_l_y, mode="markers",
            marker=dict(symbol="x", size=10, color="#c0392b", line=dict(width=2)),
            name="Sortie perte/neutre",
        ), row=1, col=1)

    return len(t)


# =======================================================================
# Live IG (optionnel)
# =======================================================================

@st.cache_resource
def ig_client():
    """Cree une session IG REST. Mise en cache pour ne pas refaire a chaque refresh."""
    try:
        from config import config
        from trading_ig import IGService
        ig = IGService(
            username=config.ig.identifier, password=config.ig.password,
            api_key=config.ig.api_key, acc_type=config.ig.account_type,
            acc_number=config.ig.account_id or None,
        )
        ig.create_session(version="3")
        return ig
    except Exception as e:
        st.error(f"Connexion IG echouee : {e}")
        return None


def ig_account_snapshot(ig) -> dict:
    if ig is None:
        return {}
    try:
        accts = ig.fetch_accounts()
        rows = accts.to_dict("records") if hasattr(accts, "to_dict") else []
        from config import config
        for row in rows:
            if row.get("accountId") == config.ig.account_id:
                return {
                    "accountId":  row.get("accountId"),
                    "balance":    row.get("balance"),
                    "deposit":    row.get("deposit"),
                    "profitLoss": row.get("profitLoss"),
                    "available":  row.get("available"),
                }
        return {}
    except Exception as e:
        return {"error": str(e)}


def ig_open_positions(ig) -> pd.DataFrame:
    if ig is None:
        return pd.DataFrame()
    try:
        r = ig.fetch_open_positions()
        if hasattr(r, "to_dict"):
            return pd.DataFrame(r.to_dict("records"))
        return pd.DataFrame(r.get("positions", []))
    except Exception:
        return pd.DataFrame()


# =======================================================================
# UI Streamlit
# =======================================================================

st.set_page_config(
    page_title="Bonaza Dashboard",
    page_icon="📈",
    layout="wide",
)

st.title("📈 Bonaza — Dashboard de trading")
st.caption(f"Heure UTC : `{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}` "
           f"| Heure Martinique : `{datetime.now(tz=timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}`")

# --- Sidebar -----------------------------------------------------------
with st.sidebar:
    st.header("⚙ Paramètres")
    n_bars      = st.slider("Bougies à afficher", 50, 500, 200, step=50)
    show_ind    = st.checkbox("Indicateurs RSI/MACD/BB", value=True)
    show_trades = st.checkbox("🎯 Marqueurs trades sur le chart", value=True,
                              help="Entrées (▲▼), sorties (×), lignes SL/TP pour les positions OPEN")
    live_ig     = st.checkbox("🔴 Live IG (compte+positions)",
                              value=False,
                              help="ATTENTION : ne pas activer si main.py tourne (conflit de session)")
    if st.button("🔄 Rafraîchir maintenant"):
        st.cache_data.clear()
    st.divider()

    # Etat marchés
    st.subheader("🕐 État des marchés")
    try:
        from ig_rules import RULES
        now = datetime.now(tz=timezone.utc)
        for inst_name, r in RULES.items():
            mh = r.market_hours
            if mh is None:
                continue
            ok = mh.is_open_now()
            emoji_state = "🟢" if ok else "🔴"
            st.write(f"{emoji_state} **{inst_name}** ({'ouvert' if ok else 'fermé'})")
            nxt = mh.next_close_after(now)
            if nxt:
                st.caption(f"  → prochain close : {nxt.strftime('%a %H:%M UTC')}")
    except Exception as e:
        st.warning(f"ig_rules indisponible : {e}")

# --- Tabs --------------------------------------------------------------
tab_xauusd, tab_dax, tab_cac, tab_acc, tab_ai, tab_bot, tab_logs = st.tabs([
    "🥇 XAUUSD", "🇩🇪 DAX", "🇫🇷 CAC40",
    "💼 Compte", "🤖 Agent IA", "📱 Bot Telegram", "📋 Logs",
])


# --- Onglet par instrument ---
def render_instrument(tab, inst_name: str):
    with tab:
        meta = INSTRUMENTS[inst_name]
        st.subheader(f"{meta['emoji']} {inst_name} ({meta['epic']})")
        df = load_parquet(inst_name, n_bars)
        if df is None or df.empty:
            st.warning(f"Pas de Parquet pour {inst_name}")
            return

        # KPIs
        last = df.iloc[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Dernier close", f"{last['close']:.{meta['decimals']}f}")
        c2.metric("High période",   f"{df['high'].max():.{meta['decimals']}f}")
        c3.metric("Low période",    f"{df['low'].min():.{meta['decimals']}f}")
        delta = float(df['close'].iloc[-1] - df['close'].iloc[0])
        pct   = delta / df['close'].iloc[0] * 100
        c4.metric("Variation période", f"{delta:+.{meta['decimals']}f}", f"{pct:+.2f}%")

        # Chart
        ind = compute_indicators(df)
        fig = chart_candlestick(df, ind, f"{inst_name} M5", show_ind)

        # Marqueurs trades (entries + exits + lignes SL/TP)
        trades_inst = load_all_trades_unified(instrument=inst_name, limit=300)
        n_drawn = 0
        if show_trades and not trades_inst.empty:
            n_drawn = add_trade_markers(
                fig, trades_inst, df.index[0], df.index[-1], meta["decimals"]
            )

        st.plotly_chart(fig, use_container_width=True)
        if show_trades:
            n_open = int((trades_inst["status"] == "OPEN").sum()) if not trades_inst.empty else 0
            st.caption(
                f"🎯 {n_drawn} trade(s) affiché(s) dans la fenêtre · "
                f"{n_open} position(s) OPEN au total · "
                f"▲▼ entrée, × sortie, --- SL/TP pour OPEN."
            )

        # Tableau unifie tous les trades (OPEN + CLOSED)
        if not trades_inst.empty:
            st.subheader(f"📋 Tous les trades {inst_name} (OPEN + CLOSED)")

            show_only_open = st.checkbox(
                f"Afficher uniquement les positions OPEN ({inst_name})",
                value=False, key=f"only_open_{inst_name}",
            )
            df_show = trades_inst.copy()
            if show_only_open:
                df_show = df_show[df_show["status"] == "OPEN"]

            # Format colonnes
            for col in ("entry_price", "sl_price", "tp_price", "exit_price"):
                df_show[col] = df_show[col].apply(
                    lambda x, d=meta["decimals"]: f"{x:.{d}f}" if pd.notna(x) else "—"
                )
            df_show["pnl_eur"] = df_show["pnl_eur"].apply(
                lambda x: f"{x:+.2f}" if pd.notna(x) else "—"
            )
            df_show["duration_min"] = df_show["duration_min"].apply(
                lambda x: f"{x:.1f}" if pd.notna(x) else "—"
            )
            # short_id readable (8 derniers chars du position_id)
            df_show["short_id"] = df_show["position_id"].apply(
                lambda s: (s or "")[-8:]
            )
            cols_show = ["id", "status", "ts_open", "ts_close", "direction", "size",
                         "entry_price", "sl_price", "tp_price", "exit_price",
                         "exit_reason", "pnl_eur", "duration_min", "short_id"]
            cols_show = [c for c in cols_show if c in df_show.columns]

            # Style : surligner les OPEN en orange clair
            def _highlight_open(row):
                if row.get("status") == "OPEN":
                    return ["background-color: #fff3cd"] * len(row)
                return [""] * len(row)

            try:
                styled = df_show[cols_show].style.apply(_highlight_open, axis=1)
                st.dataframe(styled, use_container_width=True, hide_index=True, height=380)
            except Exception:
                st.dataframe(df_show[cols_show], use_container_width=True,
                             hide_index=True, height=380)

        # Signaux récents pour cet instrument
        sigs = load_signals(limit=30)
        if not sigs.empty:
            sigs_inst = sigs[sigs["instrument"] == inst_name].head(10)
            if not sigs_inst.empty:
                st.subheader("📨 Derniers signaux")
                st.dataframe(sigs_inst, use_container_width=True, hide_index=True)
        st.caption(f"⚠ Bougies depuis Parquet historique (dernière maj : {df.index[-1]}). "
                   f"Pour live, lancer main.py et activer 'Live IG'.")


render_instrument(tab_xauusd, "XAUUSD")
render_instrument(tab_dax,    "DAX")
render_instrument(tab_cac,    "CAC40")


# --- Onglet Compte ---
with tab_acc:
    st.subheader("💼 État du compte IG")
    if live_ig:
        ig = ig_client()
        acc = ig_account_snapshot(ig)
        if "error" in acc:
            st.error(f"Erreur : {acc['error']}")
        elif acc:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Balance EUR",    f"{acc.get('balance', 0):.2f}")
            c2.metric("Available EUR",  f"{acc.get('available', 0):.2f}")
            c3.metric("P&L flottant",   f"{acc.get('profitLoss', 0):+.2f}")
            c4.metric("Deposit / marge", f"{acc.get('deposit', 0):.2f}")

        st.divider()
        st.subheader("📦 Positions ouvertes")
        pos_df = ig_open_positions(ig)
        if pos_df.empty:
            st.info("Aucune position ouverte.")
        else:
            cols = [c for c in ["dealId", "epic", "direction", "size",
                                "level", "openLevel", "stopLevel", "limitLevel",
                                "bid", "offer", "createdDate"] if c in pos_df.columns]
            st.dataframe(pos_df[cols] if cols else pos_df,
                         use_container_width=True, hide_index=True)
    else:
        st.info("Active 'Live IG' dans la sidebar pour voir l'état du compte. "
                "ATTENTION : ne pas faire si main.py tourne (conflit de session).")

    st.divider()
    st.subheader("📊 Tous les trades — tous instruments (SQLite)")
    all_trades = load_all_trades_unified(instrument=None, limit=200)
    if all_trades.empty:
        st.caption("Aucun trade enregistré en SQLite.")
    else:
        n_open   = int((all_trades["status"] == "OPEN").sum())
        n_closed = int((all_trades["status"] == "CLOSED").sum())
        pnl_tot  = float(all_trades.loc[all_trades["status"] == "CLOSED", "pnl_eur"].fillna(0).sum())
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("Total trades",    len(all_trades))
        cc2.metric("OPEN (en cours)", n_open)
        cc3.metric("CLOSED",          n_closed)
        cc4.metric("P&L cumulé EUR",  f"{pnl_tot:+.2f}")

        df_show = all_trades.copy()
        df_show["short_id"] = df_show["position_id"].apply(lambda s: (s or "")[-8:])

        def _highlight_open(row):
            if row.get("status") == "OPEN":
                return ["background-color: #fff3cd"] * len(row)
            return [""] * len(row)

        cols = ["id", "instrument", "status", "ts_open", "ts_close", "direction",
                "size", "entry_price", "sl_price", "tp_price", "exit_price",
                "exit_reason", "pnl_eur", "duration_min", "short_id"]
        cols = [c for c in cols if c in df_show.columns]
        try:
            styled = df_show[cols].style.apply(_highlight_open, axis=1)
            st.dataframe(styled, use_container_width=True, hide_index=True, height=420)
        except Exception:
            st.dataframe(df_show[cols], use_container_width=True,
                         hide_index=True, height=420)


# --- Onglet Agent IA ---
with tab_ai:
    st.subheader("🤖 Décisions de l'agent IA Claude")
    ai_lines = tail_log_ai(n_recent=300)
    if not ai_lines:
        st.info("Aucune ligne AI dans le log du jour. L'agent tourne-t-il ?")
    else:
        # Stats
        decisions = {"WAIT": 0, "BUY": 0, "SELL": 0}
        for it in ai_lines:
            if "[AI] Decision=" in it["msg"]:
                d = it["msg"].split("Decision=")[1].split()[0]
                if d in decisions:
                    decisions[d] += 1
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Calls Claude", sum(decisions.values()))
        c2.metric("WAIT", decisions["WAIT"])
        c3.metric("BUY",  decisions["BUY"])
        c4.metric("SELL", decisions["SELL"])

        st.divider()
        st.subheader("📜 Dernières décisions (most recent first)")
        df_ai = pd.DataFrame(list(reversed(ai_lines)))
        if not df_ai.empty:
            st.dataframe(df_ai, use_container_width=True, hide_index=True, height=600)


# --- Onglet Bot Telegram ---
with tab_bot:
    st.subheader("📱 Bot Telegram admin")

    # Modele Claude courant (fichier hot-swap ecrit par /model)
    model_file = DATA_DIR / "current_model.txt"
    if model_file.exists():
        try:
            current_model = model_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            current_model = f"(erreur lecture : {e})"
    else:
        current_model = os.getenv("ANTHROPIC_MODEL", "(non defini)")

    # Status global (ecrit par main.py toutes les 30s)
    status_file = DATA_DIR / "status.json"
    status: dict = {}
    status_age_s: Optional[float] = None
    if status_file.exists():
        try:
            status = json.loads(status_file.read_text(encoding="utf-8"))
            status_age_s = time_mod.time() - status_file.stat().st_mtime
        except Exception as e:
            st.warning(f"Lecture status.json : {e}")

    # KPIs en haut
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🤖 Modèle Claude", current_model.replace("claude-", ""))
    bot_token_set = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
    c2.metric("🔑 Bot token", "configuré" if bot_token_set else "absent")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    c3.metric("👤 Chat autorisé", chat_id if chat_id else "non défini")
    if status_age_s is not None:
        freshness = ("🟢 frais" if status_age_s < 90
                     else "🟡 vieux" if status_age_s < 600
                     else "🔴 obsolète")
        c4.metric("📡 main.py", f"{freshness}", f"il y a {int(status_age_s)}s")
    else:
        c4.metric("📡 main.py", "—", "status.json absent")

    st.divider()

    # Commandes disponibles
    st.markdown("### 🛠 Commandes disponibles")
    commands = [
        ("/status",    "Synthèse engines (bars, signaux, RM equity/DD, agent IA)"),
        ("/signals N", "Les N derniers signaux émis (défaut 10, max 50)"),
        ("/trades N",  "Les N derniers trades fermés avec P&L (défaut 10)"),
        ("/agent",     "Détail agent IA Claude (dernière décision, tokens, coût)"),
        ("/model",     "Affiche modèle actif + liste modèles disponibles"),
        ("/model haiku|sonnet|opus", "Change le modèle à chaud (effet sous 60s)"),
        ("/logs N",    "Les N dernières lignes log (défaut 20)"),
        ("/help",      "Aide"),
    ]
    st.table(pd.DataFrame(commands, columns=["Commande", "Description"]))

    st.caption("⚠ Sécurité : seul le chat ID autorisé peut envoyer des commandes. "
               "Toute tentative depuis un autre chat est rejetée et loggée.")

    st.divider()

    # Statut moteur depuis status.json
    if status:
        st.markdown("### 📊 Dernier status.json (snapshot moteur)")
        # Affichage compact des sections les plus utiles
        col1, col2 = st.columns(2)
        with col1:
            rm = status.get("risk_manager") or {}
            if rm:
                st.markdown("**Risk Manager**")
                st.write(f"- Equity : `{rm.get('equity', '?'):.2f}` EUR" if isinstance(rm.get('equity'), (int, float)) else f"- Equity : `{rm.get('equity', '?')}`")
                st.write(f"- DD jour : `{rm.get('daily_drawdown_pct', 0):.2f}%`")
                st.write(f"- Trades jour : `{rm.get('daily_trades', 0)}`")
            ai_s = status.get("ai_agent") or {}
            if ai_s:
                st.markdown("**Agent IA**")
                st.write(f"- Modèle : `{ai_s.get('model', '?')}`")
                st.write(f"- Décisions totales : `{ai_s.get('total_decisions', 0)}`")
                st.write(f"- Coût session : `{ai_s.get('total_cost_usd', 0):.4f}` USD")
        with col2:
            engs = status.get("engines") or {}
            if engs:
                st.markdown("**Engines (bars traitées)**")
                for inst, info in engs.items():
                    bars = info.get("bars_processed", 0) if isinstance(info, dict) else "?"
                    sigs = info.get("signals_emitted", 0) if isinstance(info, dict) else "?"
                    st.write(f"- **{inst}** : {bars} bars, {sigs} signaux")
            pos = status.get("open_positions") or []
            if pos:
                st.markdown(f"**Positions ouvertes : {len(pos)}**")

        st.markdown("---")
        with st.expander("🔍 Voir status.json complet"):
            st.json(status)
    else:
        st.info("status.json absent ou illisible. Le container `bonaza_main` "
                "doit tourner pour qu'il soit écrit (toutes les 30s).")

    st.divider()
    st.markdown("### 💡 Pour utiliser le bot")
    st.markdown(
        "1. Ouvre Telegram sur ton téléphone.\n"
        "2. Cherche le bot par son nom (token configuré côté serveur).\n"
        "3. Envoie `/help` pour découvrir les commandes.\n"
        "4. Toute commande arrive sur le container `bonaza_bot` (long polling).\n"
        "5. La réponse vient en ~1-3s selon la commande."
    )


# --- Onglet Logs ---
with tab_logs:
    st.subheader("📋 Logs du jour")
    level_filter = st.multiselect(
        "Filtre niveau",
        ["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"],
        default=["INFO", "WARNING", "ERROR"],
    )
    text_filter = st.text_input("Filtre texte (substring)", "")
    n_show = st.slider("Nb lignes", 50, 1000, 200, step=50)
    lines = tail_log_all(n=n_show * 3)
    rows = []
    for it in lines:
        if it["level"] not in level_filter:
            continue
        if text_filter and text_filter.lower() not in it["msg"].lower():
            continue
        rows.append(it)
    rows = list(reversed(rows))[:n_show]
    if not rows:
        st.info("Aucune ligne ne correspond aux filtres.")
    else:
        st.dataframe(pd.DataFrame(rows),
                     use_container_width=True, hide_index=True, height=700)


# --- Footer ---
st.divider()
st.caption(
    "Bonaza Dashboard v1 | Données : Parquet historique + SQLite + logs JSON | "
    "Live IG optionnel (toggle sidebar). Refresh manuel via bouton sidebar."
)
