"""
market_triggers.py — Détection d'événements marché qui méritent un boost
modèle (Haiku -> Opus) en runtime, en dehors du calendrier économique.

6 triggers, activables individuellement via env BOOST_TRIGGERS_ENABLED
(défaut : gap,spread,dd,pnl,news,range) :

  gap     : bougie M5 dont |close-open| > GAP_ATR_MULT * ATR(14)
  spread  : spread bid/ask > SPREAD_MAX (défaut $2 sur XAUUSD)
  dd      : drawdown intra-day > DD_BOOST_PCT (défaut 1.5%)
  pnl     : position ouverte avec |P&L latent| > PNL_BOOST_R * R
  news    : nouvel item FXStreet RSS matchant mots-clés or/USD/Fed
  range   : close au-dessus/sous Donchian DONCHIAN_N (défaut 50)
"""
from __future__ import annotations

import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Deque, Iterable

import numpy as np
import talib
from loguru import logger

# ---------- config (overridable via env) ---------------------------------

DEFAULT_ENABLED = "gap,spread,dd,pnl,news,range"
GAP_ATR_MULT       = float(os.getenv("GAP_ATR_MULT",     "2.0"))
SPREAD_MAX_XAUUSD  = float(os.getenv("SPREAD_MAX_XAU",   "2.0"))    # USD
DD_BOOST_PCT       = float(os.getenv("DD_BOOST_PCT",     "1.5"))    # %
PNL_BOOST_R        = float(os.getenv("PNL_BOOST_R",      "2.0"))    # x R
DONCHIAN_N         = int(  os.getenv("DONCHIAN_N",       "50"))
NEWS_REFRESH_SEC   = int(  os.getenv("NEWS_REFRESH_SEC", "600"))    # 10 min

FXSTREET_URL = "https://www.fxstreet.com/rss/news"
NEWS_KEYWORDS = re.compile(
    r"\b(gold|xau|xau/usd|silver|fed|fomc|powell|inflation|cpi|nfp|"
    r"jobless|payroll|gdp|war|attack|invasion|sanction|geopolitic|"
    r"crisis|emergency|tariff|trump|biden|treasury|yields?)\b",
    re.IGNORECASE,
)


@dataclass
class TriggerHit:
    name: str               # "gap" | "spread" | ...
    reason: str             # message court pour log/notif
    ttl_min: int = 30       # durée pour laquelle ce trigger est "actif"
    instrument: str = ""    # info contextuelle


# ---------- helpers --------------------------------------------------------

def _enabled_set() -> set[str]:
    env = os.getenv("BOOST_TRIGGERS_ENABLED", DEFAULT_ENABLED)
    return {x.strip().lower() for x in env.split(",") if x.strip()}


def _last_atr(buffer: Iterable, period: int = 14) -> Optional[float]:
    """Renvoie l'ATR(period) sur un buffer de OHLCVCandle (ou rien si trop court)."""
    bars = list(buffer)
    if len(bars) < period + 2:
        return None
    h = np.array([c.high  for c in bars], dtype=np.float64)
    l = np.array([c.low   for c in bars], dtype=np.float64)
    c = np.array([c.close for c in bars], dtype=np.float64)
    atr = talib.ATR(h, l, c, period)
    v = atr[-1]
    return float(v) if not np.isnan(v) else None


# ---------- news fetcher (cache 10 min, dedup) ---------------------------

class NewsWatcher:
    def __init__(self):
        self._seen: Deque[str] = deque(maxlen=200)
        self._last_fetch: Optional[datetime] = None
        self._latest_match: Optional[tuple[datetime, str]] = None  # (when_seen, title)

    def tick(self) -> Optional[str]:
        """Fetch RSS si > NEWS_REFRESH_SEC. Renvoie le titre si nouvel item matchant."""
        now = datetime.now(tz=timezone.utc)
        if (self._last_fetch
                and (now - self._last_fetch).total_seconds() < NEWS_REFRESH_SEC):
            return None
        self._last_fetch = now
        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(
                FXSTREET_URL,
                headers={"User-Agent": "Mozilla/5.0 Bonaza/1.0"})
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                data = r.read()
            root = ET.fromstring(data)
        except Exception as e:
            logger.debug(f"[TRIG] news fetch echec : {e}")
            return None

        new_match: Optional[str] = None
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link")  or "").strip()
            key = link or title
            if not key or key in self._seen:
                continue
            self._seen.append(key)
            if self._last_fetch and NEWS_KEYWORDS.search(title):
                # Premier nouveau match suffit pour le trigger
                if new_match is None:
                    new_match = title
                    self._latest_match = (now, title)
        return new_match

    def latest_match_recent(self, ttl_min: int = 30) -> Optional[str]:
        if not self._latest_match:
            return None
        when, title = self._latest_match
        if (datetime.now(tz=timezone.utc) - when).total_seconds() < ttl_min * 60:
            return title
        return None


# ---------- main evaluator -----------------------------------------------

class MarketTriggers:
    """
    Évalue les triggers runtime. Construit avec des callables/objets
    optionnels — chaque trigger se skip si sa source de données manque.
    """

    def __init__(self,
                 engines: Optional[dict] = None,
                 risk_manager=None,
                 order_executor=None):
        self.engines = engines or {}        # {"XAUUSD": StrategyEngine, ...}
        self.rm = risk_manager
        self.exe = order_executor
        self.news = NewsWatcher()
        self.enabled = _enabled_set()
        logger.info(f"[TRIG] triggers actifs : {sorted(self.enabled)}")

    # ----- triggers individuels ------------------------------------------

    def _check_gap(self) -> Optional[TriggerHit]:
        eng = self.engines.get("XAUUSD")
        if not eng or not getattr(eng, "_buffer", None):
            return None
        bars = list(eng._buffer)
        if len(bars) < 16:
            return None
        atr = _last_atr(bars, 14)
        if atr is None or atr <= 0:
            return None
        last = bars[-1]
        body = abs(last.close - last.open)
        if body > GAP_ATR_MULT * atr:
            return TriggerHit(
                "gap",
                f"XAUUSD bougie M5 body={body:.2f} > {GAP_ATR_MULT}*ATR({atr:.2f})",
                ttl_min=30, instrument="XAUUSD",
            )
        return None

    def _check_spread(self) -> Optional[TriggerHit]:
        eng = self.engines.get("XAUUSD")
        if not eng or not getattr(eng, "_buffer", None):
            return None
        bars = list(eng._buffer)
        if not bars:
            return None
        last = bars[-1]
        sp = getattr(last, "spread", 0.0)
        if sp and sp > SPREAD_MAX_XAUUSD:
            return TriggerHit(
                "spread",
                f"XAUUSD spread={sp:.2f}$ > seuil {SPREAD_MAX_XAUUSD:.2f}$",
                ttl_min=20, instrument="XAUUSD",
            )
        return None

    def _check_dd(self) -> Optional[TriggerHit]:
        if not self.rm:
            return None
        try:
            snap = self.rm.snapshot()
        except Exception as e:
            logger.debug(f"[TRIG] dd snapshot fail : {e}")
            return None
        dd = float(getattr(snap, "daily_dd_pct", 0.0))
        if dd > DD_BOOST_PCT:
            return TriggerHit(
                "dd",
                f"Drawdown intra-day {dd:.2f}% > {DD_BOOST_PCT}%",
                ttl_min=60,
            )
        return None

    def _check_pnl(self) -> Optional[TriggerHit]:
        if not self.rm:
            return None
        positions = self.rm.open_positions  # property dict {id: Position}
        if not positions:
            return None
        # Sans current_prices on ne peut calculer le P&L latent ; on utilise
        # une heuristique sur les pos avec realized cumulee proche du R
        # initial stocke a l'ouverture. Skip si on n'a pas d'info.
        for pid, pos in positions.items():
            r_init = getattr(pos, "r_initial", 0.0) or 0.0
            if r_init <= 0:
                continue
            unr = getattr(pos, "last_unrealized_pnl", None)
            if unr is None:
                continue
            if abs(unr) > PNL_BOOST_R * r_init:
                side = "profit" if unr > 0 else "perte"
                return TriggerHit(
                    "pnl",
                    f"Position {pid} {side} latent={unr:+.2f} > {PNL_BOOST_R}xR ({r_init:.2f})",
                    ttl_min=30,
                )
        return None

    def _check_news(self) -> Optional[TriggerHit]:
        if "news" not in self.enabled:
            return None
        title = self.news.tick()
        if title is None:
            # Cas ou un match recent existe encore -> on garde le boost
            recent = self.news.latest_match_recent(ttl_min=30)
            if recent:
                return TriggerHit(
                    "news",
                    f"News recente : \"{recent[:80]}\"",
                    ttl_min=30,
                )
            return None
        return TriggerHit(
            "news",
            f"Nouvelle news : \"{title[:80]}\"",
            ttl_min=30,
        )

    def _check_range(self) -> Optional[TriggerHit]:
        eng = self.engines.get("XAUUSD")
        if not eng or not getattr(eng, "_buffer", None):
            return None
        bars = list(eng._buffer)
        if len(bars) < DONCHIAN_N + 1:
            return None
        recent = bars[-(DONCHIAN_N + 1):-1]  # exclut bougie courante
        hi = max(b.high for b in recent)
        lo = min(b.low  for b in recent)
        last = bars[-1]
        if last.close > hi:
            return TriggerHit(
                "range",
                f"XAUUSD close={last.close:.2f} > Donchian({DONCHIAN_N})={hi:.2f} (breakout HAUT)",
                ttl_min=45, instrument="XAUUSD",
            )
        if last.close < lo:
            return TriggerHit(
                "range",
                f"XAUUSD close={last.close:.2f} < Donchian({DONCHIAN_N})={lo:.2f} (breakout BAS)",
                ttl_min=45, instrument="XAUUSD",
            )
        return None

    # ----- API ------------------------------------------------------------

    def evaluate(self) -> list[TriggerHit]:
        hits: list[TriggerHit] = []
        if "gap"    in self.enabled and (h := self._check_gap()):    hits.append(h)
        if "spread" in self.enabled and (h := self._check_spread()): hits.append(h)
        if "dd"     in self.enabled and (h := self._check_dd()):     hits.append(h)
        if "pnl"    in self.enabled and (h := self._check_pnl()):    hits.append(h)
        if "news"   in self.enabled and (h := self._check_news()):   hits.append(h)
        if "range"  in self.enabled and (h := self._check_range()):  hits.append(h)
        return hits


# CLI smoke test (sans bougies / sans RM)
if __name__ == "__main__":
    mt = MarketTriggers()
    print(f"Triggers actifs : {mt.enabled}")
    print(f"News tick (premier fetch) : {mt.news.tick()}")
