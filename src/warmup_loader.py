"""
warmup_loader.py - Pre-chargement du buffer Bv3 depuis les donnees historiques
===============================================================================
Charge les N dernieres bougies du fichier Parquet Dukascopy dans le buffer
du StrategyEngine avant le demarrage du feed live.

Resultat : le moteur Bv3 est operationnel immediatement au lieu d'attendre
25 jours de session (2400 bougies x 8h/j).

Usage dans main.py :
    engine, rm = build_engine(config, capital=CAPITAL)
    await warmup_from_parquet(engine)   # <- ajouter cette ligne
    feed = IGDataFeed(...)
    await feed.start()
"""
from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from data_feed import OHLCVCandle
from strategy_engine import StrategyEngine, BUFFER_SIZE, BUFFER_MIN_WARMUP
from instruments import INSTRUMENTS

DATA_DIR = Path(__file__).parent.parent / "data" / "historical"


def _parquet_to_candles(
    instrument: str,
    tf:         str,
    n_bars:     int,
    session_filter: bool = True,
) -> list[OHLCVCandle]:
    """
    Charge les n_bars dernieres bougies depuis le fichier Parquet.
    Applique le filtre session si demande (13h-21h UTC pour XAUUSD).
    """
    path = DATA_DIR / f"{instrument}_{tf}.parquet"
    if not path.exists():
        logger.warning(f"Fichier historique manquant : {path}")
        return []

    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df.sort_index()

    # Filtre session XAUUSD 13h-21h UTC
    if session_filter and instrument == "XAUUSD":
        df = df[(df.index.hour >= 13) & (df.index.hour < 21)]

    # Prendre les n_bars dernieres bougies
    df = df.tail(n_bars)

    # Epic correct par instrument : sinon le PortfolioRunner route les bougies
    # de warmup par candle.epic et envoie CAC40/DAX dans le buffer XAUUSD.
    _inst = INSTRUMENTS.get(instrument)
    epic = _inst.epic if _inst is not None else "CS.D.CFEGOLD.CFE.IP"

    candles = []
    for ts, row in df.iterrows():
        try:
            c = OHLCVCandle(
                epic        = epic,
                scale       = "5MINUTE",
                timestamp   = ts.to_pydatetime().replace(tzinfo=timezone.utc),
                open        = float(row.get("open",  row.get("BID_OPEN",  0))),
                high        = float(row.get("high",  row.get("BID_HIGH",  0))),
                low         = float(row.get("low",   row.get("BID_LOW",   0))),
                close       = float(row.get("close", row.get("BID_CLOSE", 0))),
                volume      = float(row.get("volume", 0)),
                bid_close   = float(row.get("close", 0)),
                ask_close   = float(row.get("close", 0)),
                is_complete = True,
            )
            if c.close > 0:
                candles.append(c)
        except Exception as e:
            logger.debug(f"Bougie ignoree : {e}")

    return candles


async def warmup_from_parquet(
    engine:     StrategyEngine,
    instrument: str = "XAUUSD",
    tf:         str = "M5",
    n_bars:     Optional[int] = None,
    session_filter: bool = True,
) -> int:
    """
    Pre-charge le buffer du StrategyEngine depuis les donnees historiques.

    Args:
        engine     : StrategyEngine a pré-remplir
        instrument : "XAUUSD"
        tf         : "M5"
        n_bars     : nombre de barres a charger (defaut: BUFFER_SIZE = 2600)
        session_filter : appliquer le filtre session (13h-21h UTC)

    Returns:
        Nombre de bougies chargees
    """
    n = n_bars or BUFFER_SIZE
    logger.info(f"Warmup depuis Parquet : {instrument} {tf} | {n} barres demandees")

    candles = _parquet_to_candles(instrument, tf, n, session_filter)
    if not candles:
        logger.warning("Aucune donnee historique disponible — warmup ignore")
        return 0

    # Injecter directement dans le buffer (sans passer par la detection Bv3)
    # On remplit le buffer mais on ne detecte pas les signaux sur les donnees passees
    for candle in candles:
        engine._buffer.append(candle)
        engine._state.bar_count += 1

    n_loaded = len(candles)
    warmup_pct = min(100, n_loaded / BUFFER_MIN_WARMUP * 100)

    logger.info(
        f"Warmup termine : {n_loaded} bougies chargees | "
        f"Buffer: {len(engine._buffer)}/{BUFFER_SIZE} | "
        f"Warmup: {warmup_pct:.0f}% ({'COMPLET' if n_loaded >= BUFFER_MIN_WARMUP else 'PARTIEL'})"
    )

    if n_loaded < BUFFER_MIN_WARMUP:
        logger.warning(
            f"Warmup partiel ({n_loaded}/{BUFFER_MIN_WARMUP} barres). "
            f"Les signaux seront suspendus jusqu'a accumulation de {BUFFER_MIN_WARMUP - n_loaded} barres supplementaires."
        )
    else:
        logger.success("Moteur Bv3 operationnel immediatement — pas d'attente requise")

    return n_loaded
