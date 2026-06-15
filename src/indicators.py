"""
indicators.py - M02 : Moteur d'indicateurs techniques Bonaza
=============================================================
Wrapper production TA-Lib.
Entree  : deque de OHLCVCandle (CandleBuffer)
Sortie  : IndicatorSet dataclass (snapshot du dernier bar)

Indicateurs calcules :
  - EMA 20 / EMA 50
  - RSI 14
  - MACD (12, 26, 9)  -> line / signal / histogram
  - ATR 14
  - Bollinger Bands (20, 2.0) -> upper / mid / lower / %B / bandwidth
  - Stochastique lent (5, 3, 3) -> slowK / slowD
  - SMA volume 20

Lookback reels mesures (TA-Lib 0.6.8) :
  EMA(20)=19  EMA(50)=49  RSI(14)=14  MACD=33  BB(20)=19  STOCH=8
  => buffer minimum : 50 bars ; recommande : 100 bars
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Deque

import numpy as np

try:
    import talib
    TALIB_OK = True
except ImportError:
    TALIB_OK = False

from loguru import logger

# Import conditionnel - data_feed peut ne pas etre disponible en tests isoles
try:
    from data_feed import OHLCVCandle
except ImportError:
    OHLCVCandle = None  # type: ignore


# -----------------------------------------------------------------------
# Constantes
# -----------------------------------------------------------------------

# Periodes
EMA_FAST     = 20
EMA_SLOW     = 50
RSI_PERIOD   = 14
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9
ATR_PERIOD   = 14
BB_PERIOD    = 20
BB_STDDEV    = 2.0
STOCH_FASTK  = 5
STOCH_SLOWK  = 3
STOCH_SLOWD  = 3
VOL_MA       = 20

# Taille du buffer
BUFFER_MIN    = 50    # minimum absolu pour EMA50
BUFFER_DEFAULT = 100  # recommande pour stabilite

# Seuils de signal (peuvent etre surcharges par la strategie)
RSI_OVERBOUGHT  = 70.0
RSI_OVERSOLD    = 30.0
STOCH_OVERBOUGHT = 80.0
STOCH_OVERSOLD   = 20.0


# -----------------------------------------------------------------------
# IndicatorSet - snapshot complet d'un bar
# -----------------------------------------------------------------------

@dataclass
class IndicatorSet:
    """
    Snapshot de tous les indicateurs pour le dernier bar du buffer.
    NaN = indicateur non encore calculable (warmup insuffisant).
    """
    # Metadonnees
    timestamp:  datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    epic:       str      = ""
    scale:      str      = ""
    bar_count:  int      = 0      # nombre de bars dans le buffer

    # Prix du dernier bar
    close:  float = math.nan
    high:   float = math.nan
    low:    float = math.nan
    volume: float = math.nan

    # EMA
    ema_fast: float = math.nan   # EMA 20
    ema_slow: float = math.nan   # EMA 50

    # RSI
    rsi: float = math.nan

    # MACD
    macd_line:   float = math.nan
    macd_signal: float = math.nan
    macd_hist:   float = math.nan

    # ATR
    atr: float = math.nan

    # Bollinger Bands
    bb_upper:     float = math.nan
    bb_mid:       float = math.nan
    bb_lower:     float = math.nan
    bb_percent_b: float = math.nan   # Position du prix dans les bandes [0..1]
    bb_bandwidth: float = math.nan   # (upper-lower)/mid * 100

    # Stochastique lent
    stoch_k: float = math.nan
    stoch_d: float = math.nan

    # Volume
    volume_ma: float = math.nan

    # ---------------------------------------------------------------
    # Proprietes derivees (ne dependent pas de TA-Lib)
    # ---------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        """True quand tous les indicateurs principaux sont calcules."""
        return not any(math.isnan(v) for v in [
            self.ema_fast, self.ema_slow, self.rsi,
            self.macd_line, self.atr, self.bb_upper,
        ])

    @property
    def ema_bullish(self) -> bool:
        """EMA rapide au-dessus de EMA lente = tendance haussiere."""
        return (not math.isnan(self.ema_fast) and
                not math.isnan(self.ema_slow) and
                self.ema_fast > self.ema_slow)

    @property
    def ema_bearish(self) -> bool:
        return (not math.isnan(self.ema_fast) and
                not math.isnan(self.ema_slow) and
                self.ema_fast < self.ema_slow)

    @property
    def rsi_overbought(self) -> bool:
        return not math.isnan(self.rsi) and self.rsi >= RSI_OVERBOUGHT

    @property
    def rsi_oversold(self) -> bool:
        return not math.isnan(self.rsi) and self.rsi <= RSI_OVERSOLD

    @property
    def rsi_neutral(self) -> bool:
        return (not math.isnan(self.rsi) and
                RSI_OVERSOLD < self.rsi < RSI_OVERBOUGHT)

    @property
    def macd_bullish(self) -> bool:
        """Histogramme MACD positif = momentum haussier."""
        return not math.isnan(self.macd_hist) and self.macd_hist > 0

    @property
    def macd_bearish(self) -> bool:
        return not math.isnan(self.macd_hist) and self.macd_hist < 0

    @property
    def price_above_ema_fast(self) -> bool:
        return (not math.isnan(self.close) and
                not math.isnan(self.ema_fast) and
                self.close > self.ema_fast)

    @property
    def price_above_bb_mid(self) -> bool:
        return (not math.isnan(self.close) and
                not math.isnan(self.bb_mid) and
                self.close > self.bb_mid)

    @property
    def stoch_overbought(self) -> bool:
        return not math.isnan(self.stoch_k) and self.stoch_k >= STOCH_OVERBOUGHT

    @property
    def stoch_oversold(self) -> bool:
        return not math.isnan(self.stoch_k) and self.stoch_k <= STOCH_OVERSOLD

    @property
    def above_average_volume(self) -> bool:
        return (not math.isnan(self.volume) and
                not math.isnan(self.volume_ma) and
                self.volume > self.volume_ma)

    def atr_pct(self, price: Optional[float] = None) -> float:
        """ATR en pourcentage du prix (volatilite relative)."""
        p = price or self.close
        if math.isnan(self.atr) or math.isnan(p) or p == 0:
            return math.nan
        return (self.atr / p) * 100.0

    def to_dict(self) -> dict:
        """Serialisation pour logging JSON."""
        return {
            "timestamp":    self.timestamp.isoformat(),
            "epic":         self.epic,
            "scale":        self.scale,
            "bar_count":    self.bar_count,
            "close":        round(self.close,    4) if not math.isnan(self.close)    else None,
            "ema_fast":     round(self.ema_fast, 4) if not math.isnan(self.ema_fast) else None,
            "ema_slow":     round(self.ema_slow, 4) if not math.isnan(self.ema_slow) else None,
            "rsi":          round(self.rsi,      2) if not math.isnan(self.rsi)      else None,
            "macd_line":    round(self.macd_line,   4) if not math.isnan(self.macd_line)   else None,
            "macd_signal":  round(self.macd_signal, 4) if not math.isnan(self.macd_signal) else None,
            "macd_hist":    round(self.macd_hist,   4) if not math.isnan(self.macd_hist)   else None,
            "atr":          round(self.atr,     4) if not math.isnan(self.atr)     else None,
            "bb_upper":     round(self.bb_upper,     4) if not math.isnan(self.bb_upper)     else None,
            "bb_mid":       round(self.bb_mid,       4) if not math.isnan(self.bb_mid)       else None,
            "bb_lower":     round(self.bb_lower,     4) if not math.isnan(self.bb_lower)     else None,
            "bb_pct_b":     round(self.bb_percent_b, 4) if not math.isnan(self.bb_percent_b) else None,
            "stoch_k":      round(self.stoch_k, 2) if not math.isnan(self.stoch_k) else None,
            "stoch_d":      round(self.stoch_d, 2) if not math.isnan(self.stoch_d) else None,
            "volume":       self.volume if not math.isnan(self.volume) else None,
            "volume_ma":    round(self.volume_ma, 2) if not math.isnan(self.volume_ma) else None,
            "is_ready":     self.is_ready,
            "ema_bullish":  self.ema_bullish,
            "rsi_zone":     ("overbought" if self.rsi_overbought
                             else "oversold" if self.rsi_oversold
                             else "neutral" if self.rsi_neutral else None),
        }

    def __repr__(self) -> str:
        if not self.is_ready:
            return (f"IndicatorSet[{self.epic} {self.scale}] "
                    f"WARMUP ({self.bar_count}/{BUFFER_MIN} bars)")
        trend = "BULL" if self.ema_bullish else "BEAR" if self.ema_bearish else "FLAT"
        rsi_z = "OB" if self.rsi_overbought else "OS" if self.rsi_oversold else "ok"
        return (
            f"IndicatorSet[{self.epic} {self.scale}] "
            f"C={self.close:.2f} "
            f"EMA{EMA_FAST}={self.ema_fast:.2f} EMA{EMA_SLOW}={self.ema_slow:.2f} [{trend}] "
            f"RSI={self.rsi:.1f}[{rsi_z}] "
            f"MACD={self.macd_hist:+.4f} "
            f"ATR={self.atr:.2f} "
            f"BB%B={self.bb_percent_b:.2f} "
            f"K={self.stoch_k:.1f}"
        )


# -----------------------------------------------------------------------
# CandleBuffer - buffer roulant de bougies
# -----------------------------------------------------------------------

class CandleBuffer:
    """
    Buffer roulant de OHLCVCandle.
    Maintient les N derniers bars et expose les arrays numpy pour TA-Lib.
    Thread-safe en lecture (les arrays sont recrees a chaque appel).
    """

    def __init__(self, maxlen: int = BUFFER_DEFAULT) -> None:
        if maxlen < BUFFER_MIN:
            raise ValueError(
                f"maxlen={maxlen} trop petit. "
                f"Minimum {BUFFER_MIN} bars requis pour EMA{EMA_SLOW}."
            )
        self._buf: Deque = deque(maxlen=maxlen)
        self.maxlen = maxlen

    def push(self, candle) -> None:
        """Ajoute une bougie complete. Ignore les bougies non completes."""
        if candle is None:
            return
        # Accepte OHLCVCandle ou tout objet avec les attributs OHLCV
        if hasattr(candle, 'is_complete') and not candle.is_complete:
            return
        self._buf.append(candle)

    def push_many(self, candles) -> None:
        """Ajoute une sequence de bougies (utile pour l'historique initial)."""
        for c in candles:
            self.push(c)

    @property
    def size(self) -> int:
        return len(self._buf)

    @property
    def is_ready(self) -> bool:
        """True quand le buffer contient assez de bars pour tous les indicateurs."""
        return len(self._buf) >= BUFFER_MIN

    def arrays(self) -> tuple[np.ndarray, ...]:
        """
        Retourne (close, high, low, volume) en float64.
        Toujours une copie fraiche (thread-safe en lecture).
        """
        closes  = np.array([c.close  for c in self._buf], dtype=np.float64)
        highs   = np.array([c.high   for c in self._buf], dtype=np.float64)
        lows    = np.array([c.low    for c in self._buf], dtype=np.float64)
        volumes = np.array([c.volume for c in self._buf], dtype=np.float64)
        return closes, highs, lows, volumes

    def latest(self):
        """Retourne la bougie la plus recente ou None si vide."""
        return self._buf[-1] if self._buf else None

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)


# -----------------------------------------------------------------------
# IndicatorEngine - calcule les indicateurs depuis un buffer
# -----------------------------------------------------------------------

class IndicatorEngine:
    """
    Moteur de calcul des indicateurs techniques.

    Usage :
        engine = IndicatorEngine()
        engine.buffer.push(candle)          # alimenter au fil des bougies
        iset = engine.compute()             # snapshot du dernier bar
        if iset.is_ready:
            print(iset.ema_bullish)
    """

    def __init__(self, maxlen: int = BUFFER_DEFAULT) -> None:
        if not TALIB_OK:
            raise RuntimeError(
                "TA-Lib non disponible. "
                "Installer via : pip install TA-Lib"
            )
        self.buffer = CandleBuffer(maxlen=maxlen)
        self._last: Optional[IndicatorSet] = None

    def push(self, candle) -> Optional[IndicatorSet]:
        """
        Ajoute une bougie et recalcule les indicateurs.
        Retourne l'IndicatorSet mis a jour, ou None si buffer insuffisant.
        Equivalent de buffer.push() + compute() en une seule operation.
        """
        self.buffer.push(candle)
        if not self.buffer.is_ready:
            return None
        self._last = self.compute()
        return self._last

    def compute(self) -> IndicatorSet:
        """
        Calcule tous les indicateurs sur le buffer courant.
        Toujours disponible meme si buffer < BUFFER_MIN (valeurs NaN).
        """
        if len(self.buffer) == 0:
            return IndicatorSet()

        close, high, low, volume = self.buffer.arrays()
        latest = self.buffer.latest()

        iset = IndicatorSet(
            timestamp = getattr(latest, 'timestamp', datetime.now(tz=timezone.utc)),
            epic      = getattr(latest, 'epic',  ""),
            scale     = getattr(latest, 'scale', ""),
            bar_count = len(self.buffer),
            close     = float(close[-1]),
            high      = float(high[-1]),
            low       = float(low[-1]),
            volume    = float(volume[-1]),
        )

        # --- EMA ---
        iset.ema_fast = _last_valid(talib.EMA(close, EMA_FAST))
        iset.ema_slow = _last_valid(talib.EMA(close, EMA_SLOW))

        # --- RSI ---
        iset.rsi = _last_valid(talib.RSI(close, RSI_PERIOD))

        # --- MACD ---
        macd_line, macd_sig, macd_hist = talib.MACD(
            close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        iset.macd_line   = _last_valid(macd_line)
        iset.macd_signal = _last_valid(macd_sig)
        iset.macd_hist   = _last_valid(macd_hist)

        # --- ATR ---
        iset.atr = _last_valid(talib.ATR(high, low, close, ATR_PERIOD))

        # --- Bollinger Bands ---
        bb_upper, bb_mid, bb_lower = talib.BBANDS(
            close, BB_PERIOD, BB_STDDEV, BB_STDDEV)
        iset.bb_upper = _last_valid(bb_upper)
        iset.bb_mid   = _last_valid(bb_mid)
        iset.bb_lower = _last_valid(bb_lower)

        if not any(math.isnan(v) for v in [
                iset.bb_upper, iset.bb_lower, iset.bb_mid, iset.close]):
            band_width = iset.bb_upper - iset.bb_lower
            if band_width > 0:
                iset.bb_percent_b = (iset.close - iset.bb_lower) / band_width
                iset.bb_bandwidth = (band_width / iset.bb_mid) * 100.0

        # --- Stochastique lent ---
        stoch_k, stoch_d = talib.STOCH(
            high, low, close,
            fastk_period  = STOCH_FASTK,
            slowk_period  = STOCH_SLOWK,
            slowk_matype  = 0,
            slowd_period  = STOCH_SLOWD,
            slowd_matype  = 0,
        )
        iset.stoch_k = _last_valid(stoch_k)
        iset.stoch_d = _last_valid(stoch_d)

        # --- Volume MA ---
        iset.volume_ma = _last_valid(talib.SMA(volume, VOL_MA))

        logger.debug("Indicateurs calcules", **{
            k: v for k, v in iset.to_dict().items()
            if k not in ("timestamp", "epic", "scale")
        })

        return iset

    @property
    def last(self) -> Optional[IndicatorSet]:
        """Dernier IndicatorSet calcule (peut etre None au demarrage)."""
        return self._last

    @property
    def is_ready(self) -> bool:
        return self.buffer.is_ready

    def reset(self) -> None:
        """Remet le buffer a zero (ex: reconnexion du feed)."""
        self.buffer.clear()
        self._last = None
        logger.info("IndicatorEngine reset")


# -----------------------------------------------------------------------
# Utilitaires
# -----------------------------------------------------------------------

def _last_valid(arr: np.ndarray) -> float:
    """
    Retourne la derniere valeur non-NaN d'un array TA-Lib.
    Retourne NaN si le tableau est vide ou entierement NaN.
    """
    if arr is None or len(arr) == 0:
        return math.nan
    val = arr[-1]
    if np.isnan(val):
        return math.nan
    return float(val)


def compute_from_arrays(
    close:  np.ndarray,
    high:   np.ndarray,
    low:    np.ndarray,
    volume: Optional[np.ndarray] = None,
) -> IndicatorSet:
    """
    Calcule les indicateurs directement depuis des arrays numpy.
    Utile pour le backtesting et les tests unitaires sans OHLCVCandle.

    Args:
        close, high, low : arrays float64 de meme longueur
        volume           : optionnel

    Returns:
        IndicatorSet avec les valeurs du dernier bar
    """
    if not TALIB_OK:
        raise RuntimeError("TA-Lib non disponible")

    n = len(close)
    if n == 0:
        return IndicatorSet()

    if volume is None:
        volume = np.zeros(n, dtype=np.float64)

    close  = np.asarray(close,  dtype=np.float64)
    high   = np.asarray(high,   dtype=np.float64)
    low    = np.asarray(low,    dtype=np.float64)
    volume = np.asarray(volume, dtype=np.float64)

    iset = IndicatorSet(
        bar_count = n,
        close     = float(close[-1]),
        high      = float(high[-1]),
        low       = float(low[-1]),
        volume    = float(volume[-1]),
    )

    iset.ema_fast = _last_valid(talib.EMA(close, EMA_FAST))
    iset.ema_slow = _last_valid(talib.EMA(close, EMA_SLOW))
    iset.rsi      = _last_valid(talib.RSI(close, RSI_PERIOD))

    ml, ms, mh = talib.MACD(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    iset.macd_line   = _last_valid(ml)
    iset.macd_signal = _last_valid(ms)
    iset.macd_hist   = _last_valid(mh)

    iset.atr = _last_valid(talib.ATR(high, low, close, ATR_PERIOD))

    bu, bm, bl = talib.BBANDS(close, BB_PERIOD, BB_STDDEV, BB_STDDEV)
    iset.bb_upper = _last_valid(bu)
    iset.bb_mid   = _last_valid(bm)
    iset.bb_lower = _last_valid(bl)

    if not any(math.isnan(v) for v in [
            iset.bb_upper, iset.bb_lower, iset.bb_mid]):
        bw = iset.bb_upper - iset.bb_lower
        if bw > 0:
            iset.bb_percent_b = (iset.close - iset.bb_lower) / bw
            iset.bb_bandwidth = (bw / iset.bb_mid) * 100.0

    sk, sd = talib.STOCH(high, low, close,
                         fastk_period=STOCH_FASTK,
                         slowk_period=STOCH_SLOWK, slowk_matype=0,
                         slowd_period=STOCH_SLOWD, slowd_matype=0)
    iset.stoch_k = _last_valid(sk)
    iset.stoch_d = _last_valid(sd)
    iset.volume_ma = _last_valid(talib.SMA(volume, VOL_MA))

    return iset
