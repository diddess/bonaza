"""
economic_calendar.py — Calendrier économique ForexFactory pour Bonaza.

Source : https://nfs.faireconomy.media/ff_calendar_thisweek.xml (XML public,
gratuit, sans clé). Le flux est mis a jour ~1x/jour avec tous les events
de la semaine en cours, en heure Eastern (ET).

API publique :
  cal = EconomicCalendar()
  await cal.refresh()                          # fetch + cache 12h
  cal.next_events(n=5, only_high=True)         # 5 prochains events HIGH
  cal.is_in_event_window(now, before=30, after=90) -> (bool, event_or_None)

Filtrage par defaut : impact=High, country in {USD, EUR, GBP} (les 3 devises
qui font bouger XAUUSD). Configurable via :
  CALENDAR_COUNTRIES=USD,EUR,GBP
  CALENDAR_MIN_IMPACT=High        # ou Medium pour plus de signal
"""
from __future__ import annotations

import os
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
CACHE_FILE = Path(__file__).parent.parent / "data" / "calendar_cache.xml"
CACHE_TTL_HOURS = 12

DEFAULT_COUNTRIES = {"USD", "EUR", "GBP"}
IMPACT_ORDER = {"Low": 1, "Medium": 2, "High": 3, "Holiday": 0, "": 0}


@dataclass(frozen=True)
class Event:
    title: str
    country: str
    impact: str   # "High" | "Medium" | "Low" | "Holiday"
    dt_utc: datetime
    forecast: str = ""
    previous: str = ""

    def short(self) -> str:
        t = self.dt_utc.strftime("%a %d/%m %H:%M UTC")
        return f"[{self.impact[:1]}/{self.country}] {t} — {self.title}"


def _et_to_utc(date_str: str, time_str: str) -> Optional[datetime]:
    """Combine date 'MM-DD-YYYY' + time 'h:MMam' (ET) en datetime UTC."""
    if not date_str or not time_str:
        return None
    # 'All Day' / 'Tentative' : non-horodate
    if time_str.lower() in {"all day", "tentative"} or ":" not in time_str:
        return None
    try:
        dt_naive = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
    except ValueError:
        return None
    dt_et = dt_naive.replace(tzinfo=ZoneInfo("America/New_York"))
    return dt_et.astimezone(timezone.utc)


class EconomicCalendar:
    """Cache + parse + query du calendrier ForexFactory."""

    def __init__(self,
                 countries: Optional[set[str]] = None,
                 min_impact: str = "High"):
        env_c = os.getenv("CALENDAR_COUNTRIES", "")
        self.countries = (
            set(c.strip().upper() for c in env_c.split(",") if c.strip())
            if env_c else (countries or DEFAULT_COUNTRIES)
        )
        self.min_impact = os.getenv("CALENDAR_MIN_IMPACT", min_impact)
        self._min_impact_val = IMPACT_ORDER.get(self.min_impact, 3)
        self.events: list[Event] = []
        self._last_fetch: Optional[datetime] = None

    # ---- fetch + parse ---------------------------------------------------

    def _read_cache(self) -> Optional[bytes]:
        if not CACHE_FILE.exists():
            return None
        age = datetime.now(tz=timezone.utc) - datetime.fromtimestamp(
            CACHE_FILE.stat().st_mtime, tz=timezone.utc)
        if age > timedelta(hours=CACHE_TTL_HOURS):
            return None
        try:
            return CACHE_FILE.read_bytes()
        except Exception:
            return None

    def _fetch(self) -> bytes:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            FF_URL, headers={"User-Agent": "Mozilla/5.0 Bonaza/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            data = r.read()
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_bytes(data)
        return data

    async def refresh(self, force: bool = False) -> int:
        """Lit le cache si frais, sinon fetch. Retourne nb d'events parses."""
        data = None if force else self._read_cache()
        if data is None:
            try:
                data = self._fetch()
                logger.info(f"[CAL] Calendrier ForexFactory fetch OK ({len(data)} bytes)")
            except Exception as e:
                logger.error(f"[CAL] Echec fetch ForexFactory : {e}")
                # Si on a un cache meme expire, fallback dessus
                if CACHE_FILE.exists():
                    data = CACHE_FILE.read_bytes()
                    logger.warning("[CAL] Fallback cache expire")
                else:
                    return 0

        events: list[Event] = []
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            logger.error(f"[CAL] XML invalide : {e}")
            return 0

        for e in root.findall("event"):
            country = (e.findtext("country") or "").strip().upper()
            impact  = (e.findtext("impact") or "").strip()
            title   = (e.findtext("title") or "").strip()
            date_s  = (e.findtext("date") or "").strip()
            time_s  = (e.findtext("time") or "").strip()
            dt_utc  = _et_to_utc(date_s, time_s)
            if dt_utc is None:
                continue
            events.append(Event(
                title=title, country=country, impact=impact, dt_utc=dt_utc,
                forecast=(e.findtext("forecast") or "").strip(),
                previous=(e.findtext("previous") or "").strip(),
            ))
        events.sort(key=lambda x: x.dt_utc)
        self.events = events
        self._last_fetch = datetime.now(tz=timezone.utc)
        logger.info(f"[CAL] {len(events)} events parses "
                    f"({sum(1 for x in events if x.impact == 'High')} HIGH)")
        return len(events)

    # ---- queries ---------------------------------------------------------

    def _filter(self, evs: list[Event]) -> list[Event]:
        out = []
        for e in evs:
            if e.country not in self.countries:
                continue
            if IMPACT_ORDER.get(e.impact, 0) < self._min_impact_val:
                continue
            out.append(e)
        return out

    def next_events(self, n: int = 10, only_high: bool = False) -> list[Event]:
        now = datetime.now(tz=timezone.utc)
        futur = [e for e in self.events if e.dt_utc >= now]
        evs = self._filter(futur)
        if only_high:
            evs = [e for e in evs if e.impact == "High"]
        return evs[:n]

    def is_in_event_window(self,
                           now: Optional[datetime] = None,
                           before_min: int = 30,
                           after_min: int = 90) -> tuple[bool, Optional[Event]]:
        """Sommes-nous a T-before ou T+after d'un event HIGH filtre ?"""
        now = now or datetime.now(tz=timezone.utc)
        for e in self._filter(self.events):
            start = e.dt_utc - timedelta(minutes=before_min)
            end   = e.dt_utc + timedelta(minutes=after_min)
            if start <= now <= end:
                return True, e
        return False, None

    def next_window_start(self,
                          now: Optional[datetime] = None,
                          before_min: int = 30) -> Optional[tuple[datetime, Event]]:
        """Prochaine date d'entree de fenetre HIGH (utile pour /boost status)."""
        now = now or datetime.now(tz=timezone.utc)
        for e in self._filter(self.events):
            start = e.dt_utc - timedelta(minutes=before_min)
            if start >= now:
                return start, e
        return None


# Utilisation directe en CLI pour debug
if __name__ == "__main__":
    import asyncio
    cal = EconomicCalendar()
    asyncio.run(cal.refresh(force=True))
    print(f"\nProchains 10 events (countries={cal.countries}, min={cal.min_impact}) :")
    for e in cal.next_events(10):
        print(f"  {e.short()}")
    in_win, ev = cal.is_in_event_window()
    print(f"\nEn fenetre event ? {in_win}  ({ev.short() if ev else '-'})")
    nxt = cal.next_window_start()
    if nxt:
        start, e = nxt
        print(f"Prochaine fenetre boost : {start.strftime('%a %d/%m %H:%M UTC')} — {e.title}")
