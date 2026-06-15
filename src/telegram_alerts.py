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
import queue
import threading
import time
from typing import Optional

import requests
from loguru import logger


class TelegramAlerts:
    """Wrapper Telegram Bot API minimal (sendMessage + parse_mode markdown)."""

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    MIN_INTERVAL_SEC = 1.0   # rate limit interne (Telegram = 30 msg/sec mais soyons doux)
    _QUEUE_MAX = 500         # borne la file (alertes non critiques : on droppe si pleine)

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
        self._lock      = threading.Lock()   # protege le demarrage du worker
        self._last_sent = 0.0
        self._fail_count = 0
        # File + worker dedie : send() est NON BLOQUANT (n'execute JAMAIS time.sleep
        # ni requests.post sur l'appelant). Indispensable car send() est appele depuis
        # des handlers asyncio (telegram_reader) : un appel bloquant gelait l'event loop
        # et retardait la reception des messages Telethon (latence 56-170s).
        self._queue: "queue.Queue" = queue.Queue(maxsize=self._QUEUE_MAX)
        self._worker: Optional[threading.Thread] = None

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, name="tg-alerts-worker", daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
        """Tourne dans un THREAD dedie (hors event loop) : rate-limit + HTTP."""
        while True:
            text, parse_mode, silent = self._queue.get()
            try:
                delta = time.time() - self._last_sent
                if delta < self.MIN_INTERVAL_SEC:
                    time.sleep(self.MIN_INTERVAL_SEC - delta)   # OK : thread, pas le loop
                self._last_sent = time.time()
                self._post(text, parse_mode, silent)
            except Exception as e:
                logger.warning(f"[Telegram] worker : {e}")
            finally:
                self._queue.task_done()

    def send(self, text: str, parse_mode: str = "Markdown", silent: bool = False) -> bool:
        """
        Met le message en file pour envoi par le worker. NON BLOQUANT : retourne
        immediatement (True = enfile, False = desactive/file pleine). L'envoi reel
        (rate-limit + requests.post) se fait dans le thread worker, jamais sur
        l'appelant -> n'affecte pas l'event loop asyncio.
        """
        if not self.enabled:
            return False
        if len(text) > 4000:
            text = text[:3990] + "…(truncated)"
        self._ensure_worker()
        try:
            self._queue.put_nowait((text, parse_mode, silent))
            return True
        except queue.Full:
            self._fail_count += 1
            logger.warning("[Telegram] file d'alertes pleine -> message ignore")
            return False

    def _post(self, text: str, parse_mode: str, silent: bool) -> bool:
        """Envoi HTTP reel (appele UNIQUEMENT par le worker). Ne leve jamais."""
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

    def flush(self, timeout: float = 15.0) -> None:
        """Attend que TOUS les messages enfiles soient reellement envoyes (utile
        pour les scripts one-shot / tests). unfinished_tasks tombe a 0 quand le
        worker a appele task_done() pour chaque message (donc apres l'envoi)."""
        end = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < end:
            time.sleep(0.2)

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
    print("Envoi message de test (enfile + flush)...")
    ok = a.send("✅ *Bonaza Telegram*\nTest de connexion OK\n"
                "Si tu vois ce message, les alertes sont fonctionnelles.")
    a.flush()
    print(f"resultat enfilage : {'OK' if ok else 'ECHEC'} (envoi reel dans le worker)")
