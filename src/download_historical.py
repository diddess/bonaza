"""
download_historical.py - Telechargement donnees historiques Bonaza
====================================================================
Deux sources :
  1. Dukascopy (RECOMMANDE pour backtesting) : gratuit, sans quota,
     tick-level, recouvert par dukascopy-python.
     XAU/USD et DAX (E_DAAX) disponibles.

  2. IG Markets (complementaire) : memes donnees que le trading live,
     mais quota 10 000 pts/semaine. Utiliser uniquement pour
     les periodes recentes ou les resolutions H1/H4.

Usage :
    python src/download_historical.py --source dukascopy --instrument XAUUSD --tf M5 --years 2
    python src/download_historical.py --source dukascopy --instrument DAX --tf M1 --months 6
    python src/download_historical.py --source dukascopy --instrument CAC40 --tf M5 --years 2
    python src/download_historical.py --source ig --instrument XAUUSD --tf M5 --days 30
    python src/download_historical.py --summary
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

DATA_DIR = Path(__file__).parent.parent / "data" / "historical"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------
# Mapping instruments
# -----------------------------------------------------------------------

DUKASCOPY_SYMBOLS = {
    "XAUUSD":  "XAU/USD",    # Or - INSTRUMENT_FX_METALS_XAU_USD
    "DAX":     "E_DAAX",     # DAX 40 - INSTRUMENT_IDX_EUROPE_E_DAAX
    "CAC40":   "E_CAAC-40",  # CAC 40 - INSTRUMENT_IDX_EUROPE_E_CAAC_40
    "EURUSD":  "EUR/USD",
    "USDJPY":  "USD/JPY",
}

DUKASCOPY_TF = {
    "M1":  (1,  "m"),
    "M5":  (5,  "m"),
    "M15": (15, "m"),
    "H1":  (1,  "h"),
    "H4":  (4,  "h"),
    "D1":  (1,  "d"),
}

# Mapping direct TF -> string intervalle dukascopy_python
# Constantes verifiees dans dukascopy_python 4.0.1
_DUKASCOPY_INTERVAL_MAP_STR = {
    "S1":  "1SEC",
    "S10": "10SEC",
    "S30": "30SEC",
    "M1":  "1MIN",
    "M5":  "5MIN",
    "M15": "15MIN",
    "H1":  "1HOUR",
    "H4":  "4HOUR",
    "D1":  "1DAY",
}

IG_EPICS = {
    "XAUUSD": "CS.D.USCGOLD.CFD.IP",
    "DAX":    "IX.D.DAX.DAILY.IP",
    "CAC40":  "IX.D.CAC.DAILY.IP",
}

IG_RESOLUTIONS = {
    "M1":  "MINUTE",
    "M2":  "MINUTE_2",
    "M5":  "MINUTE_5",
    "M15": "MINUTE_15",
    "M30": "MINUTE_30",
    "H1":  "HOUR",
    "H4":  "HOUR_4",
    "D1":  "DAY",
}


# -----------------------------------------------------------------------
# Source 1 : Dukascopy
# -----------------------------------------------------------------------

def download_dukascopy(
    instrument: str,
    tf:         str,
    start:      datetime,
    end:        datetime,
    offer_side: str = "BID",
    output_path: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Telecharge des donnees historiques depuis Dukascopy.
    Necessite : pip install dukascopy-python

    Args:
        instrument : "XAUUSD", "DAX", "CAC40", etc.
        tf         : "M1", "M5", "M15", "H1", "H4", "D1"
        start/end  : periode
        offer_side : "BID" recommande pour backtest conservateur
        output_path: si fourni, sauvegarde en Parquet + CSV

    Returns:
        DataFrame colonnes : timestamp, open, high, low, close, volume
    """
    try:
        import dukascopy_python as dk
    except ImportError:
        raise ImportError(
            "dukascopy-python non installe.\n"
            "Installation : pip install dukascopy-python"
        )

    symbol = DUKASCOPY_SYMBOLS.get(instrument, instrument)

    interval_str = _DUKASCOPY_INTERVAL_MAP_STR.get(tf)
    if not interval_str:
        raise ValueError(
            f"Timeframe {tf!r} non supporte. "
            f"Utiliser : {list(_DUKASCOPY_INTERVAL_MAP_STR)}"
        )

    side = dk.OFFER_SIDE_ASK if offer_side.upper() == "ASK" else dk.OFFER_SIDE_BID

    logger.info(
        "Dukascopy : debut telechargement",
        instrument=instrument, symbol=symbol,
        tf=tf, interval=interval_str,
        start=start.isoformat(), end=end.isoformat(),
    )

    t0 = time.time()
    df_raw = dk.fetch(
        instrument = symbol,
        interval   = interval_str,
        offer_side = side,
        start      = start,
        end        = end,
    )
    elapsed = time.time() - t0

    logger.info(
        "Dukascopy : telechargement termine",
        rows=len(df_raw), elapsed_s=round(elapsed, 1),
    )

    df = _normalize_dukascopy(df_raw, instrument, tf)

    if output_path:
        _save(df, output_path)

    return df


def _normalize_dukascopy(df_raw: pd.DataFrame, instrument: str, tf: str) -> pd.DataFrame:
    """Normalise le DataFrame Dukascopy au format Bonaza OHLCV."""
    df = df_raw.copy()

    rename_map = {}
    for col in df.columns:
        cl = col.lower()
        if   "open"   in cl: rename_map[col] = "open"
        elif "high"   in cl: rename_map[col] = "high"
        elif "low"    in cl: rename_map[col] = "low"
        elif "close"  in cl: rename_map[col] = "close"
        elif "volume" in cl or "vol" in cl: rename_map[col] = "volume"

    df = df.rename(columns=rename_map)

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(
                f"Colonne {col!r} absente apres normalisation. "
                f"Colonnes disponibles : {list(df_raw.columns)}"
            )

    if "volume" not in df.columns:
        df["volume"] = 0.0

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    idx_name = df.index.name or "index"
    df = df.reset_index().rename(columns={idx_name: "timestamp"})

    if "timestamp" not in df.columns:
        df = df.rename(columns={df.columns[0]: "timestamp"})

    df["instrument"] = instrument
    df["tf"]         = tf

    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"])
    df = df[["timestamp", "open", "high", "low", "close", "volume", "instrument", "tf"]]

    logger.debug(
        f"Normalise : {len(df)} bars | "
        f"{df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}"
    )
    return df


def list_dukascopy_instruments(search: Optional[str] = None) -> None:
    """Affiche les instruments Dukascopy disponibles."""
    try:
        import dukascopy_python as dk
        from dukascopy_python import instruments as inst_module
        import inspect

        all_instruments = [
            v for k, v in inspect.getmembers(inst_module)
            if isinstance(v, str) and not k.startswith('_')
        ]
        matches = sorted(set(
            i for i in all_instruments
            if not search or search.lower() in i.lower()
        ))

        print(f"\nInstruments Dukascopy ({len(matches)} resultats) :")
        for i in matches:
            print(f"  {i}")

    except Exception as e:
        print(f"\nErreur liste instruments : {e}")
        print("\nInstruments configures pour Bonaza :")
        for k, v in DUKASCOPY_SYMBOLS.items():
            print(f"  {k:12s} -> {v}")


# -----------------------------------------------------------------------
# Source 2 : IG Markets
# -----------------------------------------------------------------------

def download_ig(
    instrument:   str,
    tf:           str,
    start:        datetime,
    end:          datetime,
    output_path:  Optional[Path] = None,
    sleep_between_requests: float = 1.0,
) -> pd.DataFrame:
    """Telecharge depuis IG Markets. QUOTA : 10 000 pts/semaine."""
    try:
        from trading_ig import IGService
    except ImportError:
        raise ImportError("pip install trading-ig munch")

    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from config import config

    if not config.ig.is_valid():
        raise RuntimeError("Credentials IG manquants dans .env")

    resolution = IG_RESOLUTIONS.get(tf)
    if not resolution:
        raise ValueError(f"TF {tf!r} non supporte. Utiliser : {list(IG_RESOLUTIONS)}")

    epic = IG_EPICS.get(instrument)
    if not epic:
        raise ValueError(f"Instrument {instrument!r} non configure : {list(IG_EPICS)}")

    ig = IGService(
        username = config.ig.identifier,
        password = config.ig.password,
        api_key  = config.ig.api_key,
        acc_type = config.ig.account_type,
    )
    ig.create_session()
    _check_ig_allowance(ig)

    chunks  = _split_date_range_ig(start, end, tf)
    all_dfs = []
    logger.info(f"IG Markets : {len(chunks)} tranches")

    for i, (cs, ce) in enumerate(chunks):
        try:
            result = ig.fetch_historical_prices_by_epic_and_date_range(
                epic       = epic,
                resolution = resolution,
                start_date = cs.strftime("%Y-%m-%d %H:%M:%S"),
                end_date   = ce.strftime("%Y-%m-%d %H:%M:%S"),
            )
            all_dfs.append(_normalize_ig(result, instrument, tf))
        except Exception as exc:
            logger.error(f"Tranche {i+1} erreur", error=str(exc))
            if "allowance" in str(exc).lower():
                logger.critical("QUOTA IG DEPASSE. Utiliser Dukascopy.")
                break
        time.sleep(sleep_between_requests)

    if not all_dfs:
        return pd.DataFrame()

    df = pd.concat(all_dfs, ignore_index=True)
    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"])

    if output_path:
        _save(df, output_path)

    return df


def _check_ig_allowance(ig) -> None:
    try:
        r = ig.fetch_historical_prices_by_epic_and_num_points(
            epic=IG_EPICS["DAX"], resolution="DAY", numpoints=1)
        if hasattr(r, "allowance"):
            rem   = r.allowance.get("remainingAllowance", 0)
            total = r.allowance.get("totalAllowance", 10000)
            hours = r.allowance.get("allowanceExpiry", 0) // 3600
            logger.info(f"IG quota : {rem}/{total} pts ({hours}h avant reset)")
            if rem < 500:
                logger.warning(f"Quota faible ({rem} pts). Privilegier Dukascopy.")
    except Exception:
        pass


def _normalize_ig(result, instrument: str, tf: str) -> pd.DataFrame:
    if hasattr(result, "prices") and isinstance(result.prices, pd.DataFrame):
        df_raw = result.prices
    elif isinstance(result, dict) and "prices" in result:
        df_raw = pd.DataFrame(result["prices"])
    else:
        df_raw = pd.DataFrame(result)

    if isinstance(df_raw.columns, pd.MultiIndex):
        df_raw.columns = ["_".join(str(c) for c in col).strip("_")
                          for col in df_raw.columns]

    rename = {}
    for col in df_raw.columns:
        cl = col.lower()
        if   "mid_open"  in cl or ("open"  in cl and "mid" in cl): rename[col] = "open"
        elif "mid_high"  in cl or ("high"  in cl and "mid" in cl): rename[col] = "high"
        elif "mid_low"   in cl or ("low"   in cl and "mid" in cl): rename[col] = "low"
        elif "mid_close" in cl or ("close" in cl and "mid" in cl): rename[col] = "close"
        elif "lasttraded" in cl or "volume" in cl:                 rename[col] = "volume"

    df = df_raw.rename(columns=rename)

    if "open" not in df.columns:
        for col in df_raw.columns:
            cl = col.lower()
            if   "bid_open"  in cl: rename[col] = "open"
            elif "bid_high"  in cl: rename[col] = "high"
            elif "bid_low"   in cl: rename[col] = "low"
            elif "bid_close" in cl: rename[col] = "close"
        df = df_raw.rename(columns=rename)

    if "volume" not in df.columns:
        df["volume"] = 0.0

    df.index = pd.to_datetime(df.index, utc=True)
    df = df.reset_index().rename(columns={df.index.name or "index": "timestamp"})
    df["instrument"] = instrument
    df["tf"]         = tf
    return df[["timestamp", "open", "high", "low", "close", "volume", "instrument", "tf"]]


def _split_date_range_ig(start: datetime, end: datetime, tf: str):
    bars_per_day = {"M1": 810, "M5": 162, "M15": 54, "H1": 14, "H4": 4, "D1": 1}
    days = max(1, int(8000 / bars_per_day.get(tf, 500)))
    chunks, current = [], start
    while current < end:
        chunk_end = min(current + timedelta(days=days), end)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(minutes=1)
    return chunks


# -----------------------------------------------------------------------
# Chargement
# -----------------------------------------------------------------------

def load_historical(
    instrument: str,
    tf:         str,
    start:      Optional[datetime] = None,
    end:        Optional[datetime] = None,
) -> pd.DataFrame:
    """Charge les donnees depuis le fichier Parquet sauvegarde."""
    path = _get_parquet_path(instrument, tf)
    if not path.exists():
        raise FileNotFoundError(
            f"Fichier manquant : {path}\n"
            f"Lancer : python src/download_historical.py "
            f"--source dukascopy --instrument {instrument} --tf {tf} --years 2"
        )
    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("UTC")
    df.index.name = "timestamp"
    df = df.sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="UTC")]
    logger.info(f"Charge : {instrument} {tf} | {len(df):,} bars")
    return df[["open", "high", "low", "close", "volume"]]


def get_data_summary() -> None:
    print("\n=== Donnees historiques Bonaza ===")
    parquets = list(DATA_DIR.glob("*.parquet"))
    if not parquets:
        print("  Aucune donnee. Lancer download_historical.py.")
        return
    for f in sorted(parquets):
        try:
            df = pd.read_parquet(f)
            if "timestamp" in df.columns:
                df = df.set_index("timestamp")
            print(f"  {f.name:40s} | {len(df):>8,} bars | "
                  f"{df.index[0].date()} -> {df.index[-1].date()}")
        except Exception as e:
            print(f"  {f.name}: erreur ({e})")
    print()


# -----------------------------------------------------------------------
# Utilitaires
# -----------------------------------------------------------------------

def _get_parquet_path(instrument: str, tf: str) -> Path:
    return DATA_DIR / f"{instrument}_{tf}.parquet"


def _save(df: pd.DataFrame, path: Optional[Path]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=True, compression="snappy")
    csv_path = path.with_suffix(".csv")
    df.to_csv(csv_path, index=True)
    size_mb = path.stat().st_size / 1_048_576
    logger.info(f"Sauvegarde : {path.name} ({len(df):,} bars, {size_mb:.1f} MB)")


# -----------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(
        description="Telechargement donnees historiques Bonaza",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python src/download_historical.py --source dukascopy --instrument XAUUSD --tf M5 --years 2
  python src/download_historical.py --source dukascopy --instrument DAX --tf M1 --months 6
  python src/download_historical.py --source dukascopy --instrument CAC40 --tf M5 --years 2
  python src/download_historical.py --summary
        """
    )
    p.add_argument("--source",      choices=["dukascopy","ig"], default="dukascopy")
    p.add_argument("--instrument",  choices=["XAUUSD","DAX","CAC40","EURUSD","USDJPY"],
                   default="XAUUSD")
    p.add_argument("--tf",          default="M5")
    p.add_argument("--years",       type=int, default=0)
    p.add_argument("--months",      type=int, default=0)
    p.add_argument("--days",        type=int, default=0)
    p.add_argument("--start",       help="YYYY-MM-DD")
    p.add_argument("--end",         help="YYYY-MM-DD")
    p.add_argument("--offer-side",  default="BID", choices=["BID","ASK"])
    p.add_argument("--summary",     action="store_true")
    p.add_argument("--list-instruments", nargs="?", const="", metavar="SEARCH")
    return p.parse_args()


def main():
    args = _parse_args()

    if args.summary:
        get_data_summary()
        return

    if args.list_instruments is not None:
        list_dukascopy_instruments(args.list_instruments or None)
        return

    end = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if args.end:
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    elif args.years:
        start = end - timedelta(days=args.years * 365)
    elif args.months:
        start = end - timedelta(days=args.months * 30)
    elif args.days:
        start = end - timedelta(days=args.days)
    else:
        start = end - timedelta(days=365)

    output_path = _get_parquet_path(args.instrument, args.tf)

    logger.info(
        f"Telechargement {args.source.upper()} | "
        f"{args.instrument} {args.tf} | "
        f"{start.date()} -> {end.date()}"
    )

    if args.source == "dukascopy":
        df = download_dukascopy(
            instrument  = args.instrument,
            tf          = args.tf,
            start       = start,
            end         = end,
            offer_side  = args.offer_side,
            output_path = output_path,
        )
    else:
        df = download_ig(
            instrument  = args.instrument,
            tf          = args.tf,
            start       = start,
            end         = end,
            output_path = output_path,
        )

    if not df.empty:
        print(f"\nResultat : {len(df):,} bars")
        print(f"  Periode : {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}")
        print(f"  Fichier : {output_path}")
    else:
        print("\nAucune donnee recuperee. Verifier les logs.")


if __name__ == "__main__":
    main()
