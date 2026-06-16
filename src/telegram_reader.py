"""Copieur de signaux Telegram (TRADAMAX) -> ordres XAUUSD (demo).
ENTRY -> 3 positions (TP1/TP2/TP3 'open') en exact_levels.
BREAKEVEN -> IGNORE depuis le 12/06/2026 (decision operateur : le cliquet
             protege deja ; un BE juste apres l'ENTRY fermait tout a l'ouverture).
CLOSE -> ferme toutes les positions TG (formes explicites uniquement).
CLOSE_PARTIAL -> ferme la seule jambe TPn visee (fix faux-CLOSE 2026-06-11).
CANCEL_PENDING -> no-op (copieur market-only, aucun ordre en attente cote IG).
AMBIGUOUS -> aucune action auto, demande de confirmation operateur
             (/tg closeall ou /tg ignore via le bot ; flag data/tg_closeall.flag).
"""
import os, asyncio
from loguru import logger

from strategy_spec import SignalDirection, TradeSetup
from telegram_signal_parser import parse_signal

ENVF      = "/app/data/.env.telegram"
SESSION   = "/app/data/tg_signals"
COPY_SIZE = 0.5
TG_PREFIX = "TG_XAU"
MAX_TG_POSITIONS = 6   # stacking autorise jusqu'a 6 positions (= 2 trades x 3 TP)
CLOSEALL_FLAG = "/app/data/tg_closeall.flag"   # pose par /tg closeall (bot)

# --- Gestion active des positions du copieur (tick sur prix live) ---
MANAGE_TICK_SEC = 2.0   # cadence d'evaluation (prix streaming, comme le portfolio)
LOCK_ARM_PTS    = 6.0   # profit (pts) atteint qui ARME le verrou de prise de benefice
LOCK_EXIT_PTS   = 5.0   # plancher minimal une fois arme
# CLIQUET 50% (runners) : plancher = max(LOCK_EXIT_PTS, RATCHET_PCT x pic).
# Le plancher ne descend jamais ; on ne rend jamais plus de ~50% du meilleur gain.
RATCHET_PCT            = 0.5
RATCHET_IG_GAP_PTS     = 5.0  # repercute sur le SL IG seulement si le prix est a
                              # >= 5 pts du nouveau SL (min stop IG XAU = 4 pts)
RATCHET_IG_MIN_IMPROVE = 2.0  # anti-spam API : bouge le SL IG par pas de >= 2 pts

# --- Gardes anti-peremption (incident latence 12/06 : messages livres ~11 min
#     en retard par rafale apres une coupure Telethon silencieuse) ---
LAT_WARN_SEC      = 30.0   # message plus vieux -> warning + alerte Telegram
ENTRY_MAX_AGE_SEC = 180.0  # ENTRY plus vieille -> on N'OUVRE PAS (signal perime)
ENTRY_MAX_DEV_PTS = 5.0    # prix live trop loin de l'entree du signal -> idem
WATCHDOG_SEC      = 60.0   # test actif de la liaison Telethon (reconnexion forcee)

def _load_env(p):
    if os.path.exists(p):
        for line in open(p):
            s = line.strip()
            if "=" in s and not s.startswith("#"):
                k, v = s.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

class TelegramCopier:
    def __init__(self, executor, instrument="XAUUSD", market_state=None):
        self.executor = executor
        self.instrument = instrument
        self._ms = market_state   # MarketState (structure M5) pour la gestion adaptative
        self._peak = {}     # deal_id -> profit max favorable vu en live (pts)
        self._be   = set()  # deal_id -> breakeven demande par le groupe (filet moteur)
        # serialise le traitement des messages : un BREAKEVEN/CLOSE qui arrive
        # pendant les 3-4s d'ouverture des jambes ATTEND la fin de l'ouverture
        # (race observee 4x du 08 au 10/06)
        self._msg_lock = asyncio.Lock()
        self._last_lag_alert = 0.0   # anti-spam alerte latence (1 / 5 min)

    @staticmethod
    def _alert(msg):
        try:
            from telegram_alerts import alerts as _tg
            _tg().send(msg, parse_mode=None)
        except Exception:
            pass

    def _tg_positions(self):
        return [p for p in self.executor.open_positions()
                if str(getattr(p, "setup_name", "")).startswith("TG_")]

    def _live_price(self):
        feed = getattr(self.executor, "_feed", None)
        if feed is None:
            return None
        try:
            from instruments import INSTRUMENTS
            p = feed.get_price(INSTRUMENTS[self.instrument].epic)
            return float(p) if p and p > 0 else None
        except Exception:
            return None

    async def _on_entry(self, sig, lag_sec=None):
        d = SignalDirection.LONG if sig["direction"] == "LONG" else SignalDirection.SHORT
        entry, sl = sig.get("entry"), sig.get("sl")
        if not entry or not sl:
            logger.warning("[TG] entree incomplete ignoree : %s" % sig); return
        # GARDE 1 : signal trop vieux (livre en retard par Telegram) -> on n'ouvre pas
        if lag_sec is not None and lag_sec > ENTRY_MAX_AGE_SEC:
            logger.warning("[TG] ENTRY PERIMEE (message vieux de %.0fs > %.0fs) -> ignoree"
                           % (lag_sec, ENTRY_MAX_AGE_SEC))
            self._alert("⛔ Signal TRADAMAX ignore : recu avec %.0f min de retard "
                        "(entry %s, le marche a deja bouge)." % (lag_sec / 60.0, entry))
            return
        # GARDE 2 : prix live trop loin de l'entree du signal -> signal invalide
        live = self._live_price()
        if live is not None and abs(live - float(entry)) > ENTRY_MAX_DEV_PTS:
            logger.warning("[TG] ENTRY PERIMEE (prix live %.2f vs entry %s : ecart %.1f pts "
                           "> %.1f) -> ignoree" % (live, entry, abs(live - float(entry)),
                                                   ENTRY_MAX_DEV_PTS))
            self._alert("⛔ Signal TRADAMAX ignore : prix live %.2f trop loin de "
                        "l'entree %s (ecart %.1f pts)." % (live, entry,
                                                           abs(live - float(entry))))
            return
        n_open = len(self._tg_positions())
        if n_open >= MAX_TG_POSITIONS:
            logger.info("[TG] cap stacking atteint (%d/%d positions) -> signal ignore"
                        % (n_open, MAX_TG_POSITIONS))
            return
        targets = [("TP1", sig.get("tp1")), ("TP2", sig.get("tp2")), ("TP3", 0.0)]
        opened = 0
        for tag, tp in targets:
            tp_val = float(tp) if (tp and tp > 0) else 0.0   # 0 = pas de TP (TP3 open)
            setup = TradeSetup(
                direction=d, entry=float(entry), stop_loss=float(sl),
                take_profit=tp_val, size=COPY_SIZE,
                setup_name="%s_%s" % (TG_PREFIX, tag),
                reason="TG_COPY %s %s" % (sig["direction"], tag))
            try:
                deal = await self.executor._handle_signal(
                    setup, instrument=self.instrument, exact_levels=True)
                if deal:
                    opened += 1
            except Exception as e:
                logger.error("[TG] ouverture %s KO : %s" % (tag, e))
        logger.info("[TG] ENTREE %s %s -> %d/3 ouvertes | entry~%s SL=%s TP1=%s TP2=%s" %
                    (sig["direction"], self.instrument, opened, entry, sl,
                     sig.get("tp1"), sig.get("tp2")))

    async def _on_breakeven(self):
        ps = self._tg_positions()
        for p in ps:
            self._be.add(p.deal_id)                       # filet moteur si IG refuse le SL
            await self.executor.move_stop_to(p.deal_id, p.entry_level)
        logger.info("[TG] BREAKEVEN -> %d position(s)" % len(ps))

    def _opened_before(self, ps, msg_dt):
        """GARDE anti-peremption : une instruction ne s'applique qu'aux positions
        ouvertes AVANT son horodatage Telegram (un CLOSE livre en retard ne doit
        pas toucher des positions nees apres lui — incident BE du 12/06)."""
        if msg_dt is None:
            return ps
        kept = [p for p in ps if getattr(p, "opened_at", None) is None
                or p.opened_at <= msg_dt]
        skipped = len(ps) - len(kept)
        if skipped:
            logger.warning("[TG] instruction anterieure a %d position(s) plus "
                           "recentes -> non touchees" % skipped)
        return kept

    async def _on_close(self, reason, msg_dt=None):
        ps = self._opened_before(self._tg_positions(), msg_dt)
        for p in ps:
            await self.executor.close_position(p.deal_id, "TG_%s" % reason)
            self._forget(p.deal_id)
        logger.info("[TG] CLOSE (%s) -> %d position(s) fermee(s)" % (reason, len(ps)))

    async def _on_close_partial(self, leg, msg_dt=None):
        """Ferme UNIQUEMENT la jambe TPn visee (les jambes sont taguees
        TG_XAU_TP1/TP2/TP3 dans setup_name a l'ouverture)."""
        tag = "_TP%d" % leg
        ps = [p for p in self._opened_before(self._tg_positions(), msg_dt)
              if str(getattr(p, "setup_name", "")).endswith(tag)]
        if not ps:
            logger.info("[TG] CLOSE_PARTIAL TP%d : aucune jambe correspondante "
                        "(deja sortie en TP ?) -> aucune action" % leg)
            self._alert("ℹ️ TRADAMAX demande la cloture du TP%d : aucune jambe "
                        "correspondante ouverte (deja sortie ?). Aucune action." % leg)
            return
        for p in ps:
            await self.executor.close_position(p.deal_id, "TG_PARTIAL_TP%d" % leg)
            self._forget(p.deal_id)
        logger.info("[TG] CLOSE_PARTIAL TP%d -> %d jambe(s) fermee(s), "
                    "les autres restent ouvertes" % (leg, len(ps)))

    async def _on_cancel_pending(self):
        """'Supprimer l'ordre' = annuler un ordre EN ATTENTE cote groupe.
        Le copieur n'ouvre que des ordres MARKET : il n'y a jamais d'ordre en
        attente cote IG -> ne JAMAIS toucher aux positions ouvertes."""
        logger.info("[TG] CANCEL_PENDING : copieur market-only, aucun ordre en "
                    "attente cote IG -> aucune action")
        self._alert("ℹ️ TRADAMAX : « supprimer l'ordre » recu — aucun ordre en "
                    "attente cote IG, positions ouvertes intactes.")

    async def _on_ambiguous(self, sig):
        txt = sig.get("text", "")
        logger.warning("[TG] instruction AMBIGUE (%s) -> aucune action auto : %s"
                       % (sig.get("hint", "?"), txt[:200]))
        self._alert("⚠️ TRADAMAX — instruction ambigue, AUCUNE action automatique :\n"
                    "« %s »\n\n"
                    "Fermer toutes les positions du copieur : /tg closeall\n"
                    "Ignorer : /tg ignore" % txt[:400])

    # ------------------------------------------------------------------
    # Gestion active des positions du copieur (prix live, tick 2s)
    #   1) VERROU prise-benef : a +LOCK_ARM_PTS le verrou s'arme ; si le profit
    #      repasse <= LOCK_EXIT_PTS on ferme pour encaisser (~+5 pts).
    #   2) BREAKEVEN moteur : si le groupe a demande breakeven mais qu'IG n'a pas
    #      pu poser le SL (distance mini), on ferme nous-memes quand le prix
    #      revient a l'entree (cloture ~0 au lieu de laisser filer vers le SL filet).
    # ------------------------------------------------------------------
    def _forget(self, deal_id):
        self._peak.pop(deal_id, None)
        self._be.discard(deal_id)

    async def _manage_tick(self):
        # confirmation operateur apres instruction ambigue (/tg closeall)
        if os.path.exists(CLOSEALL_FLAG):
            try:
                os.unlink(CLOSEALL_FLAG)
            except Exception:
                pass
            logger.info("[TG] /tg closeall confirme par l'operateur -> fermeture totale")
            async with self._msg_lock:
                await self._on_close("MANUAL_CONFIRM")
        feed = getattr(self.executor, "_feed", None)
        if feed is None:
            return
        ps = self._tg_positions()
        live_ids = {p.deal_id for p in ps}
        for d in list(self._peak.keys()) + list(self._be):   # purge positions fermees
            if d not in live_ids:
                self._forget(d)
        for p in ps:
            price = feed.get_price(p.epic) if getattr(p, "epic", "") else None
            if not price or price <= 0:
                continue
            long = p.direction in ("LONG", "BUY")
            profit = (price - p.entry_level) if long else (p.entry_level - price)
            # 1) VERROU / CLIQUET DE PRISE DE BENEFICE
            peak = self._peak.get(p.deal_id, 0.0)
            if profit > peak:
                peak = profit
                self._peak[p.deal_id] = peak
            # GESTION ADAPTATIVE (with-trend = laisse courir ; counter/range = cliquet),
            # via la MEME fonction pure que le scalp demo (profit_lock.adaptive_action),
            # si la structure M5 est disponible ; sinon repli sur le cliquet pts fixe.
            st = self._ms.get(self.instrument, "M5") if self._ms is not None else None
            atr = (st.last_indicators.get("atr") if (st and st.last_indicators) else None)
            if st is not None and atr and atr > 0:
                from profit_lock import adaptive_action
                cur_sl = float(getattr(p, "sl_level", 0.0) or 0.0)
                swings = st.swing_lows if long else st.swing_highs
                kind, payload, mode = adaptive_action(
                    long, p.entry_level, price, peak, atr, st.trend, swings,
                    st.events, getattr(p, "opened_at", None), cur_sl)
                if kind == "close":
                    reason = ("TG_ADAPT_%s" % payload[4:]) if str(payload).startswith("REV_") \
                        else ("TG_LOCK_%+.1fpts" % profit)
                    logger.info("[TG] ADAPT %s : %s (%s, profit %+.1f, pic %+.1f) -> cloture"
                                % (p.deal_id, reason, mode, profit, peak))
                    if await self.executor.close_position(p.deal_id, reason):
                        self._forget(p.deal_id)
                    continue
                elif kind == "move_sl":
                    if await self.executor.move_stop_to(p.deal_id, payload):
                        logger.info("[TG] ADAPT %s : SL IG -> %.2f (%s, pic +%.1f)"
                                    % (p.deal_id, payload, mode, peak))
            elif peak >= LOCK_ARM_PTS:
                floor = max(LOCK_EXIT_PTS, RATCHET_PCT * peak)
                if profit <= floor:
                    logger.info("[TG] CLIQUET %s : pic +%.1f -> repli +%.1f pts (plancher +%.1f) "
                                "@ %.2f -> cloture" % (p.deal_id, peak, profit, floor, price))
                    if await self.executor.close_position(p.deal_id, "TG_LOCK_%+.1fpts" % profit):
                        self._forget(p.deal_id)
                    continue
                new_sl = round(p.entry_level + floor, 2) if long \
                    else round(p.entry_level - floor, 2)
                cur_sl = float(getattr(p, "sl_level", 0.0) or 0.0)
                improves = (new_sl > cur_sl + RATCHET_IG_MIN_IMPROVE) if long \
                    else (cur_sl == 0.0 or new_sl < cur_sl - RATCHET_IG_MIN_IMPROVE)
                if improves and (profit - floor) >= RATCHET_IG_GAP_PTS:
                    if await self.executor.move_stop_to(p.deal_id, new_sl):
                        logger.info("[TG] CLIQUET %s : SL IG -> %.2f (pic +%.1f, "
                                    "plancher +%.1f pts garanti)"
                                    % (p.deal_id, new_sl, peak, floor))
            # 2) BREAKEVEN MOTEUR (filet si IG a refuse le SL trop proche)
            if p.deal_id in self._be:
                if (long and price <= p.entry_level) or (not long and price >= p.entry_level):
                    logger.info("[TG] BREAKEVEN moteur %s : prix %.2f vs entree %.2f -> cloture ~0"
                                % (p.deal_id, price, p.entry_level))
                    if await self.executor.close_position(p.deal_id, "TG_BREAKEVEN"):
                        self._forget(p.deal_id)
                    continue

    async def run_manager(self):
        logger.info("[TG] gestion active copieur demarree (tick %.0fs | verrou +%.0f/+%.0f pts)"
                    % (MANAGE_TICK_SEC, LOCK_ARM_PTS, LOCK_EXIT_PTS))
        while True:
            try:
                await self._manage_tick()
            except Exception as e:
                logger.error("[TG] gestion erreur : %s" % e)
            await asyncio.sleep(MANAGE_TICK_SEC)

    async def handle_text(self, text, msg_dt=None):
        # Detecteur de latence : message plus vieux que LAT_WARN_SEC -> alerte
        lag_sec = None
        if msg_dt is not None:
            from datetime import datetime, timezone
            lag_sec = (datetime.now(timezone.utc) - msg_dt).total_seconds()
            if lag_sec > LAT_WARN_SEC:
                logger.warning("[TG] LATENCE : message recu avec %.0fs de retard" % lag_sec)
                import time as _t
                if _t.monotonic() - self._last_lag_alert > 300:
                    self._last_lag_alert = _t.monotonic()
                    self._alert("⚠️ Copieur en retard : message TRADAMAX livre avec "
                                "%.1f min de retard (liaison Telegram degradee)."
                                % (lag_sec / 60.0))
        sig = parse_signal(text)
        if not sig:
            return
        logger.info("[TG] message classe : %s%s"
                    % (sig.get("type"),
                       " (lag %.0fs)" % lag_sec if lag_sec and lag_sec > 5 else ""))
        async with self._msg_lock:   # serialise ENTRY/BE/CLOSE (anti-race)
            if sig["type"] == "ENTRY":
                await self._on_entry(sig, lag_sec=lag_sec)
            elif sig["type"] == "BREAKEVEN":
                # DECISION DIDIER 12/06/2026 : BREAKEVEN du groupe IGNORE.
                # Le cliquet protege deja les gains, et un BE poste juste apres
                # l'ENTRY fermait les jambes des l'ouverture (incident 12:18).
                logger.info("[TG] BREAKEVEN du groupe ignore (protection cliquet "
                            "active, decision operateur 12/06)")
            elif sig["type"] == "CLOSE":
                await self._on_close(sig.get("reason", "close"), msg_dt=msg_dt)
            elif sig["type"] == "CLOSE_PARTIAL":
                await self._on_close_partial(sig["leg"], msg_dt=msg_dt)
            elif sig["type"] == "CANCEL_PENDING":
                await self._on_cancel_pending()
            elif sig["type"] == "AMBIGUOUS":
                await self._on_ambiguous(sig)


async def run_telegram_copier(executor, group_id, market_state=None):
    """Tache asyncio : connecte Telethon, copie les signaux. Ne se termine JAMAIS
    (sinon FIRST_COMPLETED tue le process)."""
    _load_env(ENVF)
    api_id = os.environ.get("TG_API_ID"); api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        logger.warning("[TG] api creds absents (%s) -> copieur en veille" % ENVF)
        while True:
            await asyncio.sleep(3600)
    try:
        from telethon import TelegramClient, events
    except ImportError:
        logger.error("[TG] telethon absent -> copieur en veille")
        while True:
            await asyncio.sleep(3600)

    copier = TelegramCopier(executor, market_state=market_state)
    client = TelegramClient(SESSION, int(api_id), api_hash)

    # SHADOW 'Groupe Prive' : copie PAPIER (aucun ordre reel), meme client
    shadow = None
    try:
        from telegram_shadow import ShadowTracker, SHADOW_GROUP_ID
        shadow = ShadowTracker(executor)

        @client.on(events.NewMessage(chats=SHADOW_GROUP_ID))
        async def _shadow_handler(event):
            try:
                await shadow.handle_text(event.message.message or "")
            except Exception as e:
                logger.error("[SHADOW] handler erreur : %s" % e)
    except Exception as e:
        logger.warning("[SHADOW] non demarre : %s" % e)

    # DIAGNOSTIC TEMPORAIRE : logge le chat_id de TOUT message recu, pour verifier
    # que Telethon recoit bien les updates et identifier le vrai chat_id du groupe.
    @client.on(events.NewMessage())
    async def _debug_all(event):
        try:
            logger.info("[TG-DEBUG] update recu | chat_id=%s | %s"
                        % (event.chat_id, (event.message.message or "")[:50]))
        except Exception:
            pass

    @client.on(events.NewMessage(chats=group_id))
    async def _handler(event):
        txt = event.message.message or ""
        msg_dt = getattr(event.message, "date", None)
        # GARDE-FOU anti-peremption : un message trop vieux (typiquement rejoue par
        # catch_up apres une reconnexion) n'est NI relaye NI traite -> aucune action
        # de trading sur un signal perime, pas de spam de vieux messages. Les messages
        # frais (<= seuil) suivent le flux normal (le copieur applique en plus son
        # propre garde ENTRY_MAX_AGE_SEC sur les entrees).
        if msg_dt is not None:
            from datetime import datetime, timezone
            try:
                age = (datetime.now(timezone.utc) - msg_dt).total_seconds()
            except Exception:
                age = 0.0
            if age > ENTRY_MAX_AGE_SEC:
                logger.warning("[TG] message PERIME ignore (age %.0fs > %.0fs) : %s"
                               % (age, ENTRY_MAX_AGE_SEC, txt[:60]))
                return
        # RELAIS : renvoyer le message brut du groupe vers le bot Bonaza
        if txt.strip():
            try:
                from telegram_alerts import alerts as _tg
                _tg().send("📩 TRADAMAX :\n" + txt[:3500], parse_mode=None)
            except Exception:
                pass
        try:
            await copier.handle_text(txt, msg_dt=msg_dt)
        except Exception as e:
            logger.error("[TG] handler erreur : %s" % e)

    async def _watchdog():
        """Teste activement la liaison toutes les WATCHDOG_SEC : une coupure TCP
        silencieuse (cause des livraisons en rafale ~11 min en retard le 12/06)
        est detectee en <= 75s et la reconnexion est forcee immediatement."""
        while True:
            await asyncio.sleep(WATCHDOG_SEC)
            try:
                if client.is_connected():
                    await asyncio.wait_for(client.get_me(), timeout=15)
            except Exception as e:
                logger.warning("[TG] watchdog : liaison suspecte (%s) -> "
                               "reconnexion forcee" % str(e)[:80])
                try:
                    await client.disconnect()   # run_until_disconnected rend la main
                except Exception:               # -> _client_loop se reconnecte
                    pass

    async def _client_loop():
        while True:
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    logger.error("[TG] session non autorisee -> veille")
                    await asyncio.sleep(3600); continue
                me = await client.get_me()
                logger.info("[TG] Copieur connecte (@%s) | ecoute groupe %s | XAUUSD exact_levels"
                            % (getattr(me, "username", "?"), group_id))
                # ABONNEMENT AUX UPDATES DE CANAUX : pour recevoir les NewMessage d'un
                # canal/supergroupe, Telethon doit avoir l'entite en cache ET etre
                # "abonne" cote serveur. get_dialogs() fait les deux ; sans ca, apres
                # une reconnexion le client reste connecte mais ne recoit AUCUN message
                # du canal (handler jamais declenche).
                try:
                    await client.get_dialogs()
                    logger.info("[TG] get_dialogs OK (entites + abonnement updates canaux)")
                except Exception as e:
                    logger.warning("[TG] get_dialogs : %s" % str(e)[:120])
                # RE-SYNC des updates : sans ca, apres une (re)connexion Telethon peut
                # rester connecte mais ne PLUS recevoir les NewMessage (pts/qts desync)
                # -> messages du groupe jamais livres. catch_up() resynchronise et
                # rejoue les messages manques (les ENTRY perimees sont filtrees plus bas
                # + par ENTRY_MAX_AGE_SEC -> aucun ordre sur signal vieux).
                try:
                    await client.catch_up()
                    logger.info("[TG] catch_up effectue (re-sync des updates)")
                except Exception as e:
                    logger.warning("[TG] catch_up echoue : %s" % str(e)[:120])
                await client.run_until_disconnected()
            except Exception as e:
                logger.error("[TG] connexion perdue (%s) -> retry 30s" % str(e)[:120])
                await asyncio.sleep(30)

    # client Telethon + watchdog liaison + gestion active + shadow, en parallele
    tasks = [_client_loop(), _watchdog(), copier.run_manager()]
    if shadow is not None:
        tasks.append(shadow.run())
    await asyncio.gather(*tasks)
