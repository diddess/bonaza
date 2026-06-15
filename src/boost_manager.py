"""
boost_manager.py — Orchestre le hot-swap modèle Claude (Haiku <-> Opus)
selon le calendrier économique + triggers marché.

Décision binaire à chaque tick :
  - Au moins 1 source demande boost  → modèle BOOST (défaut claude-opus-4-7)
  - Aucune source                    → modèle REST  (défaut claude-haiku-4-5)

Avec :
  - Cooldown : reste en BOOST au minimum BOOST_COOLDOWN_MIN après dernier hit
  - Override manuel : commande Telegram /boost force opus|haiku <duree_min>
  - Persist state dans data/boost_state.json
  - Notif Telegram sur chaque transition (vers BOOST + retour REST)
  - Écriture data/current_model.txt à chaque changement effectif
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from economic_calendar import EconomicCalendar
from market_triggers import MarketTriggers, TriggerHit

DATA_DIR     = Path(__file__).parent.parent / "data"
MODEL_FILE   = DATA_DIR / "current_model.txt"
STATE_FILE   = DATA_DIR / "boost_state.json"
OVERRIDE_FILE = DATA_DIR / "boost_override.json"  # ecrit par le bot Telegram

MODEL_BOOST = os.getenv("BOOST_MODEL_HIGH", "claude-opus-4-7")
MODEL_REST  = os.getenv("BOOST_MODEL_REST", "claude-haiku-4-5")
BOOST_COOLDOWN_MIN = int(os.getenv("BOOST_COOLDOWN_MIN", "5"))
EVENT_BEFORE_MIN   = int(os.getenv("BOOST_EVENT_BEFORE", "30"))
EVENT_AFTER_MIN    = int(os.getenv("BOOST_EVENT_AFTER",  "90"))


@dataclass
class BoostState:
    current_model: str = MODEL_REST
    is_boosted: bool = False
    boost_since: Optional[str] = None      # ISO UTC
    boost_until: Optional[str] = None      # ISO UTC (cooldown ou override)
    last_reason: str = ""
    sources: list[str] = field(default_factory=list)  # ["calendar:NFP US", "trigger:gap", ...]
    override_active: bool = False
    override_target: Optional[str] = None  # "claude-opus-4-7" | "claude-haiku-4-5"
    override_until: Optional[str] = None   # ISO UTC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def load(cls) -> "BoostState":
        if not STATE_FILE.exists():
            return cls()
        try:
            return cls(**json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"[BOOST] state corrompu, reset : {e}")
            return cls()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(self.to_json(), encoding="utf-8")


class BoostManager:
    def __init__(self,
                 engines: Optional[dict] = None,
                 risk_manager=None,
                 order_executor=None,
                 telegram_sender=None):
        self.calendar = EconomicCalendar()
        self.triggers = MarketTriggers(
            engines=engines, risk_manager=risk_manager, order_executor=order_executor)
        self.telegram = telegram_sender    # callable(msg, parse_mode=None) ou None
        self.state = BoostState.load()
        # Synchronise current_model.txt avec l'état persistant au boot
        self._write_model(self.state.current_model, persist_state=False)
        logger.info(f"[BOOST] init — modele actuel: {self.state.current_model} "
                    f"boost={self.state.is_boosted} override={self.state.override_active}")

    # ----- override manuel (via /boost) ----------------------------------

    def set_override(self, target: str, duration_min: int = 60) -> str:
        if target not in (MODEL_BOOST, MODEL_REST):
            return f"Modele non autorise : {target}"
        until = datetime.now(tz=timezone.utc) + timedelta(minutes=duration_min)
        self.state.override_active = True
        self.state.override_target = target
        self.state.override_until = until.isoformat()
        self.state.save()
        msg = f"Override actif → {target} pendant {duration_min}min (jusqu'a {until.strftime('%H:%M UTC')})"
        logger.info(f"[BOOST] {msg}")
        if self.telegram:
            self.telegram(f"🎛 {msg}", parse_mode=None)
        return msg

    def clear_override(self) -> str:
        self.state.override_active = False
        self.state.override_target = None
        self.state.override_until = None
        self.state.save()
        msg = "Override leve, retour mode automatique"
        logger.info(f"[BOOST] {msg}")
        if self.telegram:
            self.telegram(f"🎛 {msg}", parse_mode=None)
        return msg

    def _check_override_expiry(self) -> None:
        if not self.state.override_active or not self.state.override_until:
            return
        try:
            until = datetime.fromisoformat(self.state.override_until)
        except Exception:
            self.clear_override(); return
        if datetime.now(tz=timezone.utc) >= until:
            self.clear_override()

    def _load_override_file(self) -> None:
        """Lit data/boost_override.json (ecrit par le bot Telegram).
        Format : {"target": "claude-opus-4-7|claude-haiku-4-5|null",
                  "until_utc": "ISO"} ou fichier absent.
        Si valeur differe de l'etat persistant, applique."""
        if not OVERRIDE_FILE.exists():
            if self.state.override_active:
                self.clear_override()
            return
        try:
            data = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[BOOST] boost_override.json invalide : {e}")
            return
        target = data.get("target")
        until_s = data.get("until_utc")
        if target in (None, "", "null"):
            if self.state.override_active:
                self.clear_override()
            return
        if target not in (MODEL_BOOST, MODEL_REST):
            return
        # Active ou met a jour l'override seulement si different
        if (not self.state.override_active
                or self.state.override_target != target
                or self.state.override_until != until_s):
            self.state.override_active = True
            self.state.override_target = target
            self.state.override_until  = until_s
            self.state.save()
            logger.info(f"[BOOST] override externe lu : {target} jusqu'a {until_s}")

    # ----- decision loop -------------------------------------------------

    async def tick(self) -> None:
        """Appele toutes les ~60s par main.py."""
        # Refresh calendrier si pas a jour
        if (self.calendar._last_fetch is None
                or (datetime.now(tz=timezone.utc) - self.calendar._last_fetch).total_seconds() > 6*3600):
            await self.calendar.refresh()

        self._load_override_file()
        self._check_override_expiry()

        # 1. Override prioritaire
        if self.state.override_active and self.state.override_target:
            self._apply(target=self.state.override_target,
                        is_boosted=(self.state.override_target == MODEL_BOOST),
                        reason="Override manuel",
                        sources=["override"])
            return

        # 2. Sources de boost (calendrier + triggers)
        sources: list[str] = []
        reasons: list[str] = []

        in_win, ev = self.calendar.is_in_event_window(
            before_min=EVENT_BEFORE_MIN, after_min=EVENT_AFTER_MIN)
        if in_win and ev:
            sources.append(f"calendar:{ev.country} {ev.title[:30]}")
            reasons.append(f"📅 {ev.country} {ev.title}")

        hits: list[TriggerHit] = self.triggers.evaluate()
        for h in hits:
            sources.append(f"trigger:{h.name}")
            reasons.append(f"⚡ {h.reason}")

        if sources:
            self._apply(MODEL_BOOST, is_boosted=True,
                        reason=" | ".join(reasons), sources=sources)
            return

        # 3. Cooldown : si on est en boost depuis moins de BOOST_COOLDOWN_MIN
        # apres la derniere demande, on reste en boost
        if self.state.is_boosted and self.state.boost_until:
            try:
                until = datetime.fromisoformat(self.state.boost_until)
                if datetime.now(tz=timezone.utc) < until:
                    return  # encore en cooldown, on garde le boost
            except Exception:
                pass

        # 4. Aucune source -> mode REST
        self._apply(MODEL_REST, is_boosted=False,
                    reason="Aucun event/trigger actif", sources=[])

    # ----- application + persist + notif ---------------------------------

    def _apply(self, target: str, is_boosted: bool,
               reason: str, sources: list[str]) -> None:
        now = datetime.now(tz=timezone.utc)
        previous_model = self.state.current_model
        previous_boost = self.state.is_boosted

        # Mise a jour state
        self.state.current_model = target
        self.state.is_boosted = is_boosted
        self.state.last_reason = reason
        self.state.sources = sources

        # Cooldown : si on est en boost actif, prolonge la fenetre
        if is_boosted:
            if not previous_boost:
                self.state.boost_since = now.isoformat()
            self.state.boost_until = (now + timedelta(minutes=BOOST_COOLDOWN_MIN)).isoformat()
        elif not previous_boost:
            self.state.boost_since = None
            self.state.boost_until = None

        self.state.save()

        # Si transition de modele -> ecrire current_model.txt + notif
        if previous_model != target:
            self._write_model(target, persist_state=True)
            self._notify_transition(previous_model, target, is_boosted, reason)

    def _write_model(self, target: str, persist_state: bool) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MODEL_FILE.write_text(target, encoding="utf-8")
        logger.info(f"[BOOST] current_model.txt -> {target}")

    def _notify_transition(self, old: str, new: str, boosted: bool, reason: str) -> None:
        arrow = "🚀 BOOST ON" if boosted else "✅ BOOST OFF"
        msg = f"{arrow}\n{old}\n   ↓\n{new}\n\nRaison : {reason[:300]}"
        logger.warning(f"[BOOST] {arrow}: {old} -> {new} | {reason[:200]}")
        if self.telegram:
            try:
                self.telegram(msg, parse_mode=None)
            except Exception as e:
                logger.error(f"[BOOST] telegram fail : {e}")

    # ----- status (pour /boost et dashboard) -----------------------------

    def status_dict(self) -> dict:
        nxt = self.calendar.next_window_start(before_min=EVENT_BEFORE_MIN)
        out = {
            "current_model": self.state.current_model,
            "is_boosted": self.state.is_boosted,
            "boost_since": self.state.boost_since,
            "boost_until": self.state.boost_until,
            "last_reason": self.state.last_reason,
            "sources": self.state.sources,
            "override_active": self.state.override_active,
            "override_target": self.state.override_target,
            "override_until": self.state.override_until,
        }
        if nxt:
            start, ev = nxt
            out["next_window_start_utc"] = start.isoformat()
            out["next_event"] = f"{ev.country} {ev.title}"
        return out


# CLI smoke test
if __name__ == "__main__":
    import asyncio
    bm = BoostManager()
    asyncio.run(bm.tick())
    print(json.dumps(bm.status_dict(), indent=2))
