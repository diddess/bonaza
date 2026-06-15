"""
data_loader.py - Chargement et preparation des donnees pour le backtester Bonaza
================================================================================
Convertit les DataFrames OHLCV en liste de OHLCVCandle compatibles
avec IndicatorEngine et les setups Kasper.

Fournit aussi des fonctions utilitaires pour vectorbt.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

import numpy as np
import pandas as pd
from loguru import logger


# -----------------------------------------------------------------------
# Import conditionnel data_feed (peut etre absent en tests isolés)
# -----------------------------------------------------------------------
try:
    from data_feed import OHLCVCandle
    CANDLE_OK = True
except ImportError:
    OHLCVCandle = None
    CANDLE_OK   = False

DATA_DIR = Path(__file__).parent.parent / "data" / "historical"


# -----------------------------------------------------------------------
# Chargement depuis Parquet
# -----------------------------------------------------------------------

def load_ohlcv(
    instrument: str,
    tf:         str,
    start:      Optional[datetime] = None,
    end:        Optional[datetime] = None,
    session_filter: Optional[str]  = None,
) -> pd.DataFrame:
    """
    Charge les donnees OHLCV depuis le fichier Parquet genere par download_historical.py.

    Args:
        instrument     : "XAUUSD" ou "DAX"
        tf             : "M1", "M5", "H1", etc.
        start/end      : filtre optionnel sur la periode
        session_filter : "DAX" (09:00-17:00 Paris) ou "XAUUSD" (14:00-22:00 Paris)
                         Filtre les bars hors session pour le backtesting.

    Returns:
        DataFrame indexe par DatetimeIndex UTC avec colonnes :
        open, high, low, close, volume
    """
    path = DATA_DIR / f"{instrument}_{tf}.parquet"

    if not path.exists():
        raise FileNotFoundError(
            f"\nFichier manquant : {path}\n\n"
            f"Telechargement :\n"
            f"  python src/download_historical.py "
            f"--source dukascopy --instrument {instrument} --tf {tf} --years 2\n"
        )

    df = pd.read_parquet(path)

    # Normaliser l'index
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index.name = "timestamp"
    df = df.sort_index()

    # Filtres temporels
    if start:
        ts = pd.Timestamp(start, tz="UTC") if isinstance(start, datetime) else start
        df = df[df.index >= ts]
    if end:
        te = pd.Timestamp(end, tz="UTC") if isinstance(end, datetime) else end
        df = df[df.index <= te]

    # Filtre session
    if session_filter:
        df = _apply_session_filter(df, session_filter)

    # Nettoyage
    df = _clean_ohlcv(df)

    logger.info(
        "Donnees chargees",
        instrument=instrument, tf=tf,
        bars=len(df),
        start=str(df.index[0]) if len(df) > 0 else "vide",
        end=str(df.index[-1])   if len(df) > 0 else "vide",
    )
    return df[["open", "high", "low", "close", "volume"]]


def _apply_session_filter(df: pd.DataFrame, session: str) -> pd.DataFrame:
    """Filtre les bars hors session de trading."""
    sessions = {
        "DAX":    (8, 17),    # 09:00-17:00 Paris ~ 07:00-16:00 UTC (hiver)
        "XAUUSD": (13, 22),   # 14:00-22:00 Paris ~ 13:00-21:00 UTC (hiver)
        "LONDON": (7, 16),
        "NY":     (13, 21),
    }
    cfg = sessions.get(session.upper())
    if not cfg:
        logger.warning(f"Session {session!r} inconnue. Pas de filtre applique.")
        return df

    hour_utc_start, hour_utc_end = cfg
    mask = (df.index.hour >= hour_utc_start) & (df.index.hour < hour_utc_end)
    filtered = df[mask]
    logger.debug(f"Session {session} : {len(df)} -> {len(filtered)} bars")
    return filtered


def _clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Nettoie les donnees OHLCV : NaN, zeros, incoherences."""
    before = len(df)

    # Supprimer les lignes avec NaN sur les prix principaux
    df = df.dropna(subset=["open", "high", "low", "close"])

    # Supprimer les prix nuls ou negatifs
    price_cols = ["open", "high", "low", "close"]
    df = df[(df[price_cols] > 0).all(axis=1)]

    # Corriger les incoherences high/low
    bad_hl = df["high"] < df["low"]
    if bad_hl.sum() > 0:
        logger.warning(f"{bad_hl.sum()} bars avec high < low corriges")
        df.loc[bad_hl, ["high", "low"]] = df.loc[bad_hl, ["low", "high"]].values

    # Volume : remplacer NaN par 0
    df["volume"] = df["volume"].fillna(0.0)

    after = len(df)
    if before != after:
        logger.info(f"Nettoyage : {before - after} bars supprimes ({before} -> {after})")

    return df


# -----------------------------------------------------------------------
# Conversion DataFrame -> liste de OHLCVCandle
# -----------------------------------------------------------------------

def df_to_candles(
    df:         pd.DataFrame,
    instrument: str = "",
    tf:         str = "",
) -> List:
    """
    Convertit un DataFrame OHLCV en liste de OHLCVCandle.
    Utile pour tester l'IndicatorEngine et les setups Kasper
    sur des donnees historiques.

    Note : les candles sont marquees is_complete=True.

    Returns:
        List[OHLCVCandle] si data_feed.py est disponible,
        List[SimpleCandle] (namedtuple) sinon.
    """
    if not CANDLE_OK:
        # Fallback : utiliser un SimpleNamespace si OHLCVCandle absent
        from types import SimpleNamespace
        candles = []
        for ts, row in df.iterrows():
            c = SimpleNamespace(
                epic        = instrument,
                scale       = tf,
                timestamp   = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                open        = float(row["open"]),
                high        = float(row["high"]),
                low         = float(row["low"]),
                close       = float(row["close"]),
                volume      = float(row.get("volume", 0.0)),
                is_complete = True,
            )
            candles.append(c)
        return candles

    candles = []
    for ts, row in df.iterrows():
        ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else datetime(
            ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second,
            tzinfo=timezone.utc
        )
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)

        c = OHLCVCandle(
            epic        = instrument,
            scale       = tf,
            timestamp   = ts_dt,
            open        = float(row["open"]),
            high        = float(row["high"]),
            low         = float(row["low"]),
            close       = float(row["close"]),
            volume      = float(row.get("volume", 0.0)),
            is_complete = True,
        )
        candles.append(c)
    return candles


def iter_candles_batched(
    df:         pd.DataFrame,
    instrument: str = "",
    tf:         str = "",
    batch_size: int = 1000,
) -> Iterator[List]:
    """
    Iterateur de bougies par batch.
    Plus efficace memoire pour les grands datasets.
    """
    for i in range(0, len(df), batch_size):
        chunk = df.iloc[i:i + batch_size]
        yield df_to_candles(chunk, instrument, tf)


# -----------------------------------------------------------------------
# Preparation pour vectorbt
# -----------------------------------------------------------------------

def prepare_for_vectorbt(
    df:           pd.DataFrame,
    price_col:    str  = "close",
    include_ohlcv: bool = True,
) -> dict:
    """
    Prepare les donnees pour vectorbt.

    Returns:
        dict avec :
          - "close"  : Series pour backtesting rapide
          - "open"   : Series
          - "high"   : Series
          - "low"    : Series
          - "volume" : Series
          - "ohlcv"  : DataFrame complet (si include_ohlcv=True)

    Usage vectorbt :
        data = prepare_for_vectorbt(df)
        portfolio = vbt.Portfolio.from_signals(
            data["close"],
            entries  = entries_series,
            exits    = exits_series,
            sl_stop  = sl_series,
            tp_stop  = tp_series,
        )
    """
    result = {
        "close":  df["close"],
        "open":   df["open"],
        "high":   df["high"],
        "low":    df["low"],
        "volume": df["volume"],
    }
    if include_ohlcv:
        result["ohlcv"] = df[["open", "high", "low", "close", "volume"]]
    return result


# -----------------------------------------------------------------------
# Statistiques rapides sur les donnees chargees
# -----------------------------------------------------------------------

def data_quality_report(df: pd.DataFrame, name: str = "") -> dict:
    """
    Analyse la qualite des donnees OHLCV.
    Retourne un rapport avec les metriques cles.
    """
    if df.empty:
        return {"error": "DataFrame vide"}

    # Gaps detectes (bars manquants)
    freq_guess = _guess_frequency(df)
    expected   = pd.date_range(df.index[0], df.index[-1], freq=freq_guess)
    gaps       = len(expected) - len(df)
    gap_pct    = gaps / len(expected) * 100 if len(expected) > 0 else 0

    report = {
        "name":          name,
        "bars":          len(df),
        "start":         str(df.index[0].date()),
        "end":           str(df.index[-1].date()),
        "freq_detected": freq_guess,
        "gaps":          gaps,
        "gap_pct":       round(gap_pct, 2),
        "price_min":     round(float(df["close"].min()), 4),
        "price_max":     round(float(df["close"].max()), 4),
        "volume_mean":   round(float(df["volume"].mean()), 2),
        "nan_rows":      int(df[["open","high","low","close"]].isna().any(axis=1).sum()),
        "zero_price":    int((df[["open","high","low","close"]] <= 0).any(axis=1).sum()),
        "bad_hl":        int((df["high"] < df["low"]).sum()),
    }

    if report["nan_rows"] > 0:
        logger.warning(f"{name} : {report['nan_rows']} lignes avec NaN")
    if report["bad_hl"] > 0:
        logger.warning(f"{name} : {report['bad_hl']} lignes avec high < low")
    if gap_pct > 5.0:
        logger.warning(f"{name} : {gap_pct:.1f}% de gaps detectes")

    return report


def _guess_frequency(df: pd.DataFrame) -> str:
    """Detecte la frequence dominante du DataFrame."""
    if len(df) < 2:
        return "unknown"
    diffs = df.index.to_series().diff().dropna()
    median_diff = diffs.median().total_seconds()

    freq_map = [
        (60,    "1min"),
        (300,   "5min"),
        (900,   "15min"),
        (1800,  "30min"),
        (3600,  "1H"),
        (14400, "4H"),
        (86400, "1D"),
    ]
    for seconds, label in freq_map:
        if abs(median_diff - seconds) < seconds * 0.1:
            return label
    return f"{int(median_diff)}s"


def print_quality_report(df: pd.DataFrame, name: str = "") -> None:
    """Affiche le rapport qualite dans la console."""
    r = data_quality_report(df, name)
    print(f"\n=== Rapport qualite : {r.get('name','?')} ===")
    print(f"  Bars        : {r['bars']:,}")
    print(f"  Periode     : {r['start']} -> {r['end']}")
    print(f"  Frequence   : {r['freq_detected']}")
    print(f"  Gaps        : {r['gaps']:,} ({r['gap_pct']}%)")
    print(f"  Prix min/max: {r['price_min']} / {r['price_max']}")
    print(f"  NaN         : {r['nan_rows']}")
    print(f"  High<Low    : {r['bad_hl']}")
    quality = "OK" if r["nan_rows"] == 0 and r["bad_hl"] == 0 and r["gap_pct"] < 10 else "ATTENTION"
    print(f"  Qualite     : {quality}")
    print()
