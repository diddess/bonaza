"""
telegram_alerts.py - Notifications Telegram pour Bonaza
========================================================
Envoie des messages au bot Telegram configure pour tracer en direct :
  - signaux Bv3 / agent IA emis
  - ouvertures / fermetures de positions
  - erreurs critiques + kill switch
  - bilan de session

Configuration (.env) :
  TELEGRAM_ALERTS_ENABLED=true
  TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
  TELEGRAM_CHAT_ID=...

Comment creer le bot :
  1. Sur Telegram, parler a @BotFather
  2. Envoyer /newbot, suivre les instructions
  3. Recuperer le token (ex: 1234567890:ABCdef-GhI...)
  4. Parler une fois a TON bot (n'importe quel message)
  5. Visiter https://api.telegram.org/bot<TOKEN>/getUpdates
     Recuperer le "chat":{"id": ...} = TELEGRAM_CHAT_ID

Comportement :
  - Si TELEGRAM_ALERTS_ENABLED=false ou token manquant : no-op silencieux
    (Bonaza continue de fonctionner normalement)
  - Si erreur reseau Telegram : log warning, pas de crash
  - Rate limit interne : max 1 msg/sec pour eviter le ban
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import requests
from loguru import logger


class TelegramAlerts:
    """Wrapper Telegram Bot API minimal (sendMessage + parse_mode markdown)."""

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    MIN_INTERVAL_SEC = 1.0   # rate limit interne (Telegram = 30 msg/sec mais soyons doux)

    def __init__(
        self,
        token:   Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.token   = token   or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        if enabled is None:
            enabled = os.getenv("TELEGRAM_ALERTS_ENABLED", "false").lower() == "true"
        self.enabled = enabled and bool(self.token) and bool(self.chat_id)
        self._lock      = threading.Lock()
        self._last_sent = 0.0
        self._fail_count = 0

    def send(self, text: str, parse_mode: str = "Markdown", silent: bool = False) -> bool:
        """
        Envoie un message Telegram. Retourne True si envoye, False sinon.
        silent=True : pas de notification sonore (msg arrive quand meme).
        Ne leve jamais d'exception : si Telegram down, on log warning et continue.
        """
        if not self.enabled:
            return False
        # Rate limit interne
        with self._lock:
            now = time.time()
            delta = now - self._last_sent
            if delta < self.MIN_INTERVAL_SEC:
                time.sleep(self.MIN_INTERVAL_SEC - delta)
            self._last_sent = time.time()
        # Truncate (Telegram = 4096 chars max)
        if len(text) > 4000:
            text = text[:3990] + "…(truncated)"
        try:
            payload = {
                "chat_id":             self.chat_id,
                "text":                text,
                "disable_notification": silent,
            }
            # N'inclure parse_mode que s'il est defini : Telegram rejette
            # parse_mode=null avec "Bad Request: unsupported parse_mode".
            if parse_mode:
                payload["parse_mode"] = parse_mode
            r = requests.post(
                self.API_URL.format(token=self.token),
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                self._fail_count = 0
                return True
            self._fail_count += 1
            logger.warning(f"[Telegram] HTTP {r.status_code} : {r.text[:200]}")
            return False
        except Exception as e:
            self._fail_count += 1
            logger.warning(f"[Telegram] envoi echoue : {e}")
            return False

    # -----------------------------------------------------------------
    # Formatters Bonaza
    # -----------------------------------------------------------------

    # NB : tous ces formatters envoient en TEXTE BRUT (parse_mode=None). Les noms de
    # strategie / reason contiennent des underscores (ex "S5_ToDMomentum",
    # "TOD_SHORT_5UTC") qui cassaient le Markdown -> HTTP 400 "can't parse entities"
    # -> notifications de trade jamais delivrees. Le texte brut est bulletproof.

    def signal_emitted(self, source: str, instrument: str, direction: str,
                       entry: float, sl: float, tp: float, rr: float,
                       size: float, reason: str = "") -> bool:
        emoji = "🟢" if direction == "LONG" else "🔴"
        msg = (
            f"{emoji} Signal {source} — {instrument}\n"
            f"{direction} {size:.2f}L  E={entry}  SL={sl}  TP={tp}\n"
            f"R:R = {rr:.2f}"
        )
        if reason:
            msg += f"\n{reason[:200]}"
        return self.send(msg, parse_mode=None)

    def position_opened(self, instrument: str, direction: str, size: float,
                        deal_id: str, entry: float, sl: float, tp: float) -> bool:
        msg = (
            f"✅ Position ouverte — {instrument}\n"
            f"{direction} {size:.2f}L @ {entry}\n"
            f"SL = {sl}  TP = {tp}\n"
            f"{deal_id}"
        )
        return self.send(msg, parse_mode=None)

    def position_closed(self, instrument: str, direction: str, size: float,
                        entry: float, exit_price: float, pnl_eur: float,
                        reason: str = "SL_OR_TP") -> bool:
        emoji = "💰" if pnl_eur > 0 else "🩸"
        msg = (
            f"{emoji} Position fermee — {instrument} ({reason})\n"
            f"{direction} {size:.2f}L  entry={entry}  exit={exit_price}\n"
            f"P&L = {pnl_eur:+.2f} EUR"
        )
        return self.send(msg, parse_mode=None)

    def error(self, source: str, msg: str) -> bool:
        return self.send(f"❌ ERREUR {source}\n{msg[:500]}", parse_mode=None)

    def kill_switch(self, reason: str) -> bool:
        return self.send(f"🛑 KILL SWITCH ACTIVE\n{reason}", parse_mode=None)

    def session_start(self, label: str, mode: str, instruments: list) -> bool:
        msg = (
            f"▶️ Session demarree\n"
            f"label : {label}\n"
            f"mode  : {mode}\n"
            f"instruments : {', '.join(instruments)}"
        )
        return self.send(msg, parse_mode=None)

    def session_end(self, label: str, signals: int, trades: int,
                    pnl_eur: float, wins: int, losses: int) -> bool:
        wr = wins / max(wins+losses, 1) * 100
        msg = (
            f"📊 Session terminee — {label}\n"
            f"Signaux : {signals}  Trades : {trades}\n"
            f"W/L : {wins}/{losses}  WR = {wr:.1f}%\n"
            f"P&L = {pnl_eur:+.2f} EUR"
        )
        return self.send(msg, parse_mode=None)

    def custom(self, text: str) -> bool:
        return self.send(text)


# Singleton lazy : import telegram_alerts; telegram_alerts.alerts.send(...)
_singleton: Optional[TelegramAlerts] = None

def alerts() -> TelegramAlerts:
    """Retourne le singleton TelegramAlerts initialise depuis .env."""
    global _singleton
    if _singleton is None:
        _singleton = TelegramAlerts()
    return _singleton


# Helper rapide pour usage occasionnel
def alert(text: str) -> bool:
    return alerts().send(text)


# -----------------------------------------------------------------------
# Test standalone
# -----------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from config import config  # noqa: F401  pour charger .env

    a = alerts()
    print(f"enabled={a.enabled}  token={'***' if a.token else 'absent'}  chat_id={'***' if a.chat_id else 'absent'}")
    if not a.enabled:
        print("Telegram alerts desactives. Renseigner TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALERTS_ENABLED=true dans .env")
        sys.exit(1)
    print("Envoi message de test...")
    ok = a.send("✅ *Bonaza Telegram*\nTest de connexion OK\n"
                "Si tu vois ce message, les alertes sont fonctionnelles.")
    print(f"resultat : {'OK' if ok else 'ECHEC'}")
