"""
data_feed.py - M01 : Acquisition donnees marche Bonaza
======================================================
Connexion IG Markets via Lightstreamer (protocole natif IG).
Normalisation OHLCV par consolidation de bougies.
Bridge thread-safe : callbacks Lightstreamer -> asyncio.Queue.
Reconnexion automatique avec backoff exponentiel.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    from lightstreamer.client import (
        ClientListener,
        ItemUpdate,
        Subscription,
        SubscriptionListener,
    )
    LS_AVAILABLE = True
except ImportError:
    LS_AVAILABLE = False
    class ClientListener:       pass
    class SubscriptionListener: pass
    class Subscription:
        def __init__(self, **kw): pass
        def addListener(self, l): pass

try:
    from trading_ig import IGService, IGStreamService
    TRADING_IG_AVAILABLE = True
except ImportError:
    TRADING_IG_AVAILABLE = False
    IGService = None
    IGStreamService = None


CHART_FIELDS = [
    "CONS_END", "CONS_TICK_COUNT", "LTV", "UTM",
    "OFR_OPEN", "OFR_HIGH", "OFR_LOW", "OFR_CLOSE",
    "BID_OPEN", "BID_HIGH", "BID_LOW", "BID_CLOSE",
]

# TRADE:{accountId} - mode DISTINCT : CONFIRMS (reponse a action manuelle/API),
# OPU (Open Position Update : fill, SL/TP touche, force close), WOU (working order update).
# Resout le bug close_resolver 180s : OPU arrive en <100ms apres execution serveur.
TRADE_FIELDS = ["CONFIRMS", "OPU", "WOU"]

# MARKET:{epic} - mode MERGE : on s'interesse principalement a MARKET_STATE
# (TRADEABLE / EDITS_ONLY / CLOSED) pour detecter weekend / suspensions en temps reel.
MARKET_FIELDS = [
    "MARKET_STATE", "MARKET_DELAY", "UPDATE_TIME",
    "BID", "OFFER", "HIGH", "LOW", "MID_OPEN", "CHANGE", "CHANGE_PCT",
]

# SOURCE UNIQUE : EPICs lus depuis instruments.py pour eviter toute
# desynchro. Le 26/05/2026 on a decouvert que data_feed avait
# DAX=IX.D.DAX.DAILY.IP (25 EUR/pt !) alors que instruments.py utilise le
# mini IFMM (1 EUR/pt). Lancer un trade avec le mauvais EPIC = catastrophe.
from instruments import INSTRUMENTS, SUBSCRIPTIONS as _DEFAULT_SUBSCRIPTIONS

EPICS: Dict[str, str] = {name: inst.epic for name, inst in INSTRUMENTS.items()}

SCALES: Dict[str, str] = {
    "1min":  "1MINUTE",
    "5min":  "5MINUTE",
    "15min": "15MINUTE",
    "1h":    "HOUR",
    "4h":    "4HOUR",
}


# ---------------------------------------------------------------------------
# Helper : post dans une queue sans jamais lever d'exception
# Utilise comme callback dans call_soon_threadsafe pour eviter
# "Exception in callback" quand la queue est pleine.
# ---------------------------------------------------------------------------

def _safe_put(queue: asyncio.Queue, item) -> None:
    """Met item dans queue. Si pleine, ignore silencieusement."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass
    except Exception:
        pass


class FatalFeedError(RuntimeError):
    pass


class FeedStatus(str, Enum):
    DISCONNECTED  = "DISCONNECTED"
    CONNECTING    = "CONNECTING"
    CONNECTED     = "CONNECTED"
    RECONNECTING  = "RECONNECTING"
    STOPPED       = "STOPPED"


@dataclass
class OHLCVCandle:
    """Bougie OHLCV normalisee - prix mid (BID+ASK)/2."""
    epic:        str
    scale:       str
    timestamp:   datetime
    open:        float
    high:        float
    low:         float
    close:       float
    volume:      float
    tick_count:  int   = 0
    bid_close:   float = 0.0
    ask_close:   float = 0.0
    is_complete: bool  = True

    @property
    def mid_close(self) -> float:
        return (self.bid_close + self.ask_close) / 2.0 if self.ask_close else self.close

    @property
    def spread(self) -> float:
        return round(self.ask_close - self.bid_close, 5) if self.ask_close else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    def __repr__(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S")
        status = "COMPLETE" if self.is_complete else "partial"
        return (
            f"[{self.epic}:{self.scale} {ts}] "
            f"O={self.open:.2f} H={self.high:.2f} "
            f"L={self.low:.2f} C={self.close:.2f} "
            f"V={self.volume:.0f} [{status}]"
        )


@dataclass
class RawTick:
    epic:      str
    timestamp: datetime
    bid:       float
    ask:       float
    volume:    float
    raw:       Dict[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class CandleBuilder:
    def __init__(self, epic: str, scale: str) -> None:
        self.epic  = epic
        self.scale = scale
        self._current: Optional[OHLCVCandle] = None

    def update(self, values: Dict[str, Optional[str]]) -> Optional[OHLCVCandle]:
        def f(key, default=0.0):
            v = values.get(key)
            if v is None: return default
            try: return float(v)
            except: return default

        def i(key, default=0):
            v = values.get(key)
            if v is None: return default
            try: return int(v)
            except: return default

        utm = values.get("UTM")
        if utm:
            try: ts = datetime.fromtimestamp(int(utm) / 1000, tz=timezone.utc)
            except: ts = datetime.now(tz=timezone.utc)
        else:
            ts = datetime.now(tz=timezone.utc)

        bid_open  = f("BID_OPEN");  ofr_open  = f("OFR_OPEN")
        bid_high  = f("BID_HIGH");  ofr_high  = f("OFR_HIGH")
        bid_low   = f("BID_LOW");   ofr_low   = f("OFR_LOW")
        bid_close = f("BID_CLOSE"); ofr_close = f("OFR_CLOSE")

        mid_open  = (bid_open +ofr_open)/2  if (bid_open  and ofr_open)  else (bid_open  or ofr_open)
        mid_high  = (bid_high +ofr_high)/2  if (bid_high  and ofr_high)  else (bid_high  or ofr_high)
        mid_low   = (bid_low  +ofr_low)/2   if (bid_low   and ofr_low)   else (bid_low   or ofr_low)
        mid_close = (bid_close+ofr_close)/2 if (bid_close and ofr_close) else (bid_close or ofr_close)

        volume   = f("LTV")
        tick_cnt = i("CONS_TICK_COUNT")
        cons_end = i("CONS_END")

        if self._current is None:
            if mid_open == 0.0: return None
            self._current = OHLCVCandle(
                epic=self.epic, scale=self.scale, timestamp=ts,
                open=mid_open, high=mid_high, low=mid_low, close=mid_close,
                volume=volume, tick_count=tick_cnt,
                bid_close=bid_close, ask_close=ofr_close, is_complete=False,
            )
        else:
            c = self._current
            if mid_high and mid_high > c.high: c.high = mid_high
            if mid_low  and mid_low  < c.low:  c.low  = mid_low
            if mid_close:  c.close = mid_close
            if volume:     c.volume += volume
            if tick_cnt:   c.tick_count = tick_cnt
            if bid_close:  c.bid_close  = bid_close
            if ofr_close:  c.ask_close  = ofr_close
            c.timestamp = ts

        if cons_end == 1:
            completed = self._current
            completed.is_complete = True
            self._current = None
            logger.debug("Bougie complete", epic=self.epic, scale=self.scale)
            return completed
        return None

    def current_candle(self) -> Optional[OHLCVCandle]:
        return self._current

    def reset(self) -> None:
        self._current = None


class _ChartSubscriptionListener(SubscriptionListener):
    def __init__(self, epic, scale, loop, candle_queue, tick_queue, price_store=None):
        self._epic         = epic
        self._scale        = scale
        self._loop         = loop
        self._candle_queue = candle_queue
        self._tick_queue   = tick_queue
        self._builder      = CandleBuilder(epic, scale)
        self._price_store  = price_store   # dict partage epic -> (bid, ask)

    def onItemUpdate(self, update) -> None:
        try:
            values: Dict[str, Optional[str]] = {}
            for field_name in CHART_FIELDS:
                try: values[field_name] = update.getValue(field_name)
                except: values[field_name] = None

            completed = self._builder.update(values)

            # Memoriser le dernier prix live (pour la gestion active des positions)
            if self._price_store is not None:
                try:
                    b = float(values.get("BID_CLOSE") or 0)
                    a = float(values.get("OFR_CLOSE") or 0)
                    if b and a:
                        self._price_store[self._epic] = (b, a)
                except Exception:
                    pass

            # Post tick (silencieux si queue pleine)
            self._post_tick(values)

            # Post bougie complete
            if completed is not None:
                # Bougie : on DOIT la livrer — agrandir la queue si necessaire
                try:
                    self._loop.call_soon_threadsafe(
                        _safe_put, self._candle_queue, completed
                    )
                except RuntimeError:
                    pass

        except Exception as exc:
            logger.error("Erreur onItemUpdate", epic=self._epic, error=str(exc))

    def _post_tick(self, values):
        """Poste un tick. Ignore silencieusement si la queue est pleine."""
        try:
            utm = values.get("UTM")
            ts  = (datetime.fromtimestamp(int(utm)/1000, tz=timezone.utc)
                   if utm else datetime.now(tz=timezone.utc))
            bid = float(values.get("BID_CLOSE") or 0)
            ask = float(values.get("OFR_CLOSE") or 0)
            vol = float(values.get("LTV") or 0)
            if bid and ask:
                tick = RawTick(epic=self._epic, timestamp=ts,
                               bid=bid, ask=ask, volume=vol, raw=values)
                # _safe_put est appele comme callback : QueueFull est avale en interne
                try:
                    self._loop.call_soon_threadsafe(
                        _safe_put, self._tick_queue, tick
                    )
                except RuntimeError:
                    pass
        except Exception:
            pass

    def onSubscription(self):
        logger.info("Abonnement CHART actif", epic=self._epic, scale=self._scale)

    def onSubscriptionError(self, code, message):
        logger.error(
            f"Erreur abonnement CHART | epic={self._epic} "
            f"code={code} message='{message}'"
        )

    def onUnsubscription(self):
        logger.info("Desabonnement CHART", epic=self._epic, scale=self._scale)
        self._builder.reset()

    def reset_builder(self): self._builder.reset()


class _ConnectionListener(ClientListener):
    def __init__(self, loop, on_status):
        self._loop      = loop
        self._on_status = on_status

    def onStatusChange(self, status):
        logger.info("Statut connexion Lightstreamer", status=status)
        try: self._loop.call_soon_threadsafe(self._on_status, status)
        except RuntimeError: pass

    def onServerError(self, code, message):
        logger.error("Erreur serveur Lightstreamer", code=code, message=message)

    def onPropertyChange(self, property_name): pass


class _TradeSubscriptionListener(SubscriptionListener):
    """TRADE:{accountId} : recoit CONFIRMS / OPU / WOU en push.
    Resout en temps reel les fills, SL/TP touches, force-closes IG.
    """
    def __init__(self, account_id: str, loop, event_queue: asyncio.Queue):
        self._account_id = account_id
        self._loop = loop
        self._queue = event_queue

    def onItemUpdate(self, update):
        try:
            ts = datetime.now(tz=timezone.utc)
            payload = {
                "ts": ts.isoformat(),
                "account_id": self._account_id,
                "confirms": update.getValue("CONFIRMS"),
                "opu":      update.getValue("OPU"),
                "wou":      update.getValue("WOU"),
            }
            # Au moins un champ non-null
            if not any(payload.get(k) for k in ("confirms", "opu", "wou")):
                return
            self._loop.call_soon_threadsafe(_safe_put, self._queue, payload)
            # Log court : type d'event detecte
            for k in ("opu", "confirms", "wou"):
                v = payload.get(k)
                if v:
                    logger.info(f"[TRADE-stream] {k.upper()} recu (len={len(str(v))} chars)")
                    break
        except Exception as exc:
            logger.error(f"Erreur onItemUpdate TRADE: {exc}")

    def onSubscription(self):
        logger.info("Abonnement TRADE actif", account_id=self._account_id)

    def onSubscriptionError(self, code, message):
        logger.error(f"Erreur abonnement TRADE | code={code} message='{message}'")

    def onUnsubscription(self):
        logger.info("Desabonnement TRADE", account_id=self._account_id)


class _MarketStateSubscriptionListener(SubscriptionListener):
    """MARKET:{epic} : push MARKET_STATE (TRADEABLE/EDITS_ONLY/CLOSED/OFFLINE/etc)."""
    def __init__(self, epic: str, loop, state_dict: dict, state_queue: asyncio.Queue):
        self._epic = epic
        self._loop = loop
        self._state_dict = state_dict   # partage avec IGDataFeed.market_states
        self._queue = state_queue

    def onItemUpdate(self, update):
        try:
            state = update.getValue("MARKET_STATE")
            if state is None:
                return
            previous = self._state_dict.get(self._epic)
            if state == previous:
                return  # pas de changement
            self._state_dict[self._epic] = state
            payload = {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "epic": self._epic,
                "previous": previous,
                "state": state,
                "bid":   update.getValue("BID"),
                "offer": update.getValue("OFFER"),
            }
            self._loop.call_soon_threadsafe(_safe_put, self._queue, payload)
            logger.info(f"[MARKET-stream] {self._epic} state {previous} -> {state}")
        except Exception as exc:
            logger.error(f"Erreur onItemUpdate MARKET {self._epic}: {exc}")

    def onSubscription(self):
        logger.info("Abonnement MARKET actif", epic=self._epic)

    def onSubscriptionError(self, code, message):
        logger.error(f"Erreur abonnement MARKET {self._epic} | code={code} msg='{message}'")

    def onUnsubscription(self):
        logger.info("Desabonnement MARKET", epic=self._epic)


class IGDataFeed:
    """
    Gestionnaire principal du flux IG Markets via Lightstreamer.

    candle_queue : bougies M5 completes — consommee par StrategyEngine
    tick_queue   : ticks bruts — non consommee en prod, ignores silencieusement
                   si pleine (pas de QueueFull dans les logs).
    """

    _BACKOFF_BASE   = 2.0
    _BACKOFF_MAX    = 60.0
    _MAX_RECONNECT  = 10
    _QUEUE_MAX_SIZE = 500
    # Erreurs d'AUTHENTIFICATION IG : ne JAMAIS retenter en rafale (sinon verrou
    # compte "error.security.invalid-details" — incident cutover 14/06). Cooldown
    # long + cap, distinct du backoff reseau.
    _AUTH_ERROR_MARKERS = ("invalid-details", "client-suspended", "oauth-token-invalid",
                           "error.security", "account-token", "exceeded", "suspended")
    _AUTH_COOLDOWN_SEC  = 900.0   # 15 min entre 2 tentatives apres erreur d'auth
    _MAX_AUTH_FAILS     = 3       # au-dela -> arret du feed (intervention requise)

    def __init__(self, config, subscriptions=None):
        self._config = config
        # Defaut = SUBSCRIPTIONS du module instruments (source unique).
        self._subscriptions = subscriptions or list(_DEFAULT_SUBSCRIPTIONS)
        self.candle_queue: asyncio.Queue = asyncio.Queue(maxsize=self._QUEUE_MAX_SIZE)
        # tick_queue petite : les debordements sont avales par _safe_put
        self.tick_queue:   asyncio.Queue = asyncio.Queue(maxsize=200)
        # 2026-05-30 : ajout TRADE et MARKET subscriptions (cf. IG Streaming audit)
        self.trade_event_queue:  asyncio.Queue = asyncio.Queue(maxsize=200)
        self.market_state_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        # Dict en memoire des derniers MARKET_STATE par EPIC
        self.market_states: Dict[str, str] = {}
        # Dernier prix live (bid, ask) par EPIC, alimente a chaque tick CHART.
        # Sert a la gestion active des positions (SL theorique / TP / trailing).
        self.last_prices: Dict[str, tuple] = {}
        self._status          = FeedStatus.DISCONNECTED
        self._ig_service      = None
        self._ig_stream       = None
        self._listeners       = []
        self._reconnect_count = 0
        self._auth_fail_count = 0
        self._running         = False
        self._loop            = None
        self.on_status_change = None

    @property
    def status(self): return self._status

    @property
    def is_connected(self): return self._status == FeedStatus.CONNECTED

    async def start(self):
        if self._running:
            logger.warning("IGDataFeed deja demarre")
            return
        self._loop    = asyncio.get_running_loop()
        self._running = True
        logger.info("Demarrage IGDataFeed", subscriptions=self._subscriptions)
        await self._connect_with_retry()
        asyncio.create_task(self._supervision_loop(), name="bonaza_feed_supervision")
        logger.info("IGDataFeed demarre", status=self._status.value)

    async def stop(self):
        logger.info("Arret IGDataFeed...")
        self._running = False
        self._set_status(FeedStatus.STOPPED)
        if self._ig_stream is not None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._ig_stream.disconnect)
            except Exception as exc:
                logger.warning("Erreur deconnexion", error=str(exc))
        self._drain_queue(self.candle_queue)
        self._drain_queue(self.tick_queue)
        logger.info("IGDataFeed arrete")

    async def iter_candles(self, timeout=None):
        while self._running:
            try:
                if timeout:
                    candle = await asyncio.wait_for(
                        self.candle_queue.get(), timeout=timeout)
                else:
                    candle = await self.candle_queue.get()
                self.candle_queue.task_done()
                yield candle
            except asyncio.TimeoutError:
                raise
            except asyncio.CancelledError:
                break

    async def iter_ticks(self, timeout=None):
        while self._running:
            try:
                tick = (await asyncio.wait_for(self.tick_queue.get(), timeout=timeout)
                        if timeout else await self.tick_queue.get())
                self.tick_queue.task_done()
                yield tick
            except asyncio.CancelledError:
                break

    def get_current_candle(self, epic):
        for l in self._listeners:
            if l._epic == epic: return l._builder.current_candle()
        return None

    async def _connect_with_retry(self):
        while self._running and self._reconnect_count <= self._MAX_RECONNECT:
            try:
                self._set_status(FeedStatus.CONNECTING)
                await self._do_connect()
                self._reconnect_count = 0
                self._auth_fail_count = 0
                self._set_status(FeedStatus.CONNECTED)
                return
            except FatalFeedError:
                self._running = False
                raise
            except Exception as exc:
                msg = str(exc).lower()
                is_auth = any(m in msg for m in self._AUTH_ERROR_MARKERS)
                if is_auth:
                    # Erreur d'AUTH : surtout PAS de retry rapide (chaque tentative
                    # ratee peut verrouiller le compte IG). Cooldown long + cap.
                    self._auth_fail_count += 1
                    logger.critical(
                        "Echec AUTH IG — PAS de retry rapide (anti-verrou compte)",
                        error=str(exc), auth_fail=self._auth_fail_count,
                        cooldown_s=self._AUTH_COOLDOWN_SEC)
                    self._notify_auth_failure(str(exc), self._auth_fail_count)
                    if self._auth_fail_count >= self._MAX_AUTH_FAILS:
                        self._running = False
                        raise FatalFeedError(
                            f"Auth IG refusee {self._auth_fail_count}x ({exc}) — "
                            f"feed stoppe, intervention requise (creds / compte verrouille)"
                        ) from exc
                    self._set_status(FeedStatus.RECONNECTING)
                    await asyncio.sleep(self._AUTH_COOLDOWN_SEC)
                    continue
                # Erreur reseau / transitoire : backoff exponentiel classique
                self._reconnect_count += 1
                wait = self._backoff_seconds(self._reconnect_count)
                logger.error("Echec connexion",
                             attempt=self._reconnect_count,
                             wait_seconds=round(wait, 1),
                             error=str(exc))
                if self._reconnect_count > self._MAX_RECONNECT:
                    self._running = False
                    raise RuntimeError(
                        f"Connexion impossible apres {self._MAX_RECONNECT} tentatives"
                    ) from exc
                self._set_status(FeedStatus.RECONNECTING)
                await asyncio.sleep(wait)

    def _notify_auth_failure(self, err, count):
        """Alerte Telegram sur echec d'auth IG (import garde : data_feed ne depend
        pas de telegram_alerts en temps normal)."""
        try:
            from telegram_alerts import alerts
            alerts().send(
                "🔒 Feed IG : echec d'authentification (%dx) — %s. "
                "Pause %d min avant nouvelle tentative (anti-verrou compte IG). "
                "Verifie les identifiants / l'etat du compte."
                % (count, err[:140], int(self._AUTH_COOLDOWN_SEC // 60)),
                parse_mode=None)
        except Exception:
            pass

    async def _do_connect(self):
        await asyncio.get_running_loop().run_in_executor(None, self._sync_connect)

    def _sync_connect(self):
        if not TRADING_IG_AVAILABLE:
            raise FatalFeedError("trading-ig non disponible")
        ig_cfg = self._config.ig
        if not ig_cfg.is_valid():
            raise FatalFeedError("Credentials IG incomplets. Verifier .env")

        logger.info("Authentification IG Markets REST",
                    identifier=ig_cfg.identifier,
                    account_type=ig_cfg.account_type)

        self._ig_service = IGService(
            username   = ig_cfg.identifier,
            password   = ig_cfg.password,
            api_key    = ig_cfg.api_key,
            acc_type   = ig_cfg.account_type,
            acc_number = ig_cfg.account_id or None,
        )
        self._ig_stream = IGStreamService(self._ig_service)
        self._ig_stream.create_session(version="3")

        conn_listener = _ConnectionListener(
            loop      = self._loop,
            on_status = self._on_ls_status_change,
        )
        self._ig_stream.add_client_listener(conn_listener)

        self._listeners.clear()
        for sub_cfg in self._subscriptions:
            self._subscribe_chart(sub_cfg["epic"], sub_cfg["scale"])

        # TRADE subscription (CONFIRMS/OPU/WOU) -> push instantane fills + closes
        try:
            self._subscribe_trade()
        except Exception as exc:
            logger.warning(f"TRADE subscription echouee (non-fatal) : {exc}")

        # MARKET subscriptions (MARKET_STATE) pour chaque EPIC -> detection EDITS_ONLY
        try:
            for sub_cfg in self._subscriptions:
                self._subscribe_market_state(sub_cfg["epic"])
        except Exception as exc:
            logger.warning(f"MARKET subscription echouee (non-fatal) : {exc}")

        logger.info("Connexion Lightstreamer etablie",
                    subscriptions=len(self._subscriptions))

    def _subscribe_chart(self, epic, scale):
        item_name = f"CHART:{epic}:{scale}"
        listener  = _ChartSubscriptionListener(
            epic=epic, scale=scale, loop=self._loop,
            candle_queue=self.candle_queue, tick_queue=self.tick_queue,
            price_store=self.last_prices,
        )
        self._listeners.append(listener)
        subscription = Subscription(
            mode="MERGE", items=[item_name], fields=CHART_FIELDS)
        subscription.addListener(listener)
        self._ig_stream.subscribe(subscription)
        logger.info("Abonnement CHART cree", item=item_name)

    def _subscribe_trade(self):
        """TRADE:{accountId} DISTINCT - CONFIRMS/OPU/WOU push instantane."""
        account_id = self._config.ig.account_id
        if not account_id:
            logger.warning("TRADE subscription : IG_ACCOUNT_ID absent, skip")
            return
        item_name = f"TRADE:{account_id}"
        listener = _TradeSubscriptionListener(
            account_id=account_id, loop=self._loop,
            event_queue=self.trade_event_queue,
        )
        self._listeners.append(listener)
        subscription = Subscription(
            mode="DISTINCT", items=[item_name], fields=TRADE_FIELDS)
        subscription.addListener(listener)
        self._ig_stream.subscribe(subscription)
        logger.info("Abonnement TRADE cree", item=item_name)

    def _subscribe_market_state(self, epic):
        """MARKET:{epic} MERGE - detection TRADEABLE/EDITS_ONLY/CLOSED en temps reel."""
        item_name = f"MARKET:{epic}"
        listener = _MarketStateSubscriptionListener(
            epic=epic, loop=self._loop,
            state_dict=self.market_states,
            state_queue=self.market_state_queue,
        )
        self._listeners.append(listener)
        subscription = Subscription(
            mode="MERGE", items=[item_name], fields=MARKET_FIELDS)
        subscription.addListener(listener)
        self._ig_stream.subscribe(subscription)
        logger.info("Abonnement MARKET cree", item=item_name)

    def get_market_state(self, epic: str) -> Optional[str]:
        """Retourne le dernier MARKET_STATE connu pour cet EPIC (None si inconnu).
        Utile pour order_executor : refuser un ordre si state != TRADEABLE."""
        return self.market_states.get(epic)

    def get_price(self, epic: str) -> Optional[float]:
        """Dernier prix MID live (bid+ask)/2 pour cet EPIC, None si inconnu.
        Alimente par le flux CHART a chaque tick. Sert a la gestion active."""
        p = self.last_prices.get(epic)
        if not p:
            return None
        bid, ask = p
        if bid and ask:
            return (bid + ask) / 2.0
        return bid or ask or None

    def get_bid_ask(self, epic: str) -> Optional[tuple]:
        """Dernier (bid, ask) live pour cet EPIC, None si inconnu."""
        return self.last_prices.get(epic)

    async def _supervision_loop(self):
        logger.info("Supervision connexion demarree")
        while self._running:
            await asyncio.sleep(30)
            if not self._running: break
            if self._status == FeedStatus.DISCONNECTED:
                logger.warning("Connexion perdue - reconnexion...")
                try:
                    await self._connect_with_retry()
                except (RuntimeError, FatalFeedError):
                    logger.critical("Reconnexion impossible")
                    break
        logger.info("Supervision connexion terminee")

    def _on_ls_status_change(self, status):
        logger.debug("Statut LS", ls_status=status)
        if "CONNECTED" in status:
            self._set_status(FeedStatus.CONNECTED)
            self._reconnect_count = 0
        elif "DISCONNECTED" in status and self._running:
            self._set_status(FeedStatus.DISCONNECTED)

    def _set_status(self, status):
        if status != self._status:
            logger.info("Statut feed change",
                        old=self._status.value, new=status.value)
            self._status = status
            if self.on_status_change:
                try: self.on_status_change(status)
                except Exception: pass

    @staticmethod
    def _backoff_seconds(attempt):
        return min(
            IGDataFeed._BACKOFF_BASE ** attempt + random.uniform(0, 1),
            IGDataFeed._BACKOFF_MAX
        )

    @staticmethod
    def _drain_queue(q):
        while not q.empty():
            try: q.get_nowait()
            except: break
