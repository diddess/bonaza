"""
order_executor.py - M05 : Exécution d'ordres IG Markets
=========================================================
Corrections appliquées :
  - Direction BUY/SELL → LONG/SHORT pour RiskManager et SQLite
  - _fetch_close_price() : args corrects par méthode, pas de break prématuré
  - log_fill() et log_close() appelés → journal SQLite complet
  - Taille lue depuis setup.size (calculée par RiskManager, pas de recalcul)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from loguru import logger

from config import BonazaConfig
from ig_rules import rules_for
from risk_manager import RiskManager
from strategy_spec import SignalDirection, TradeSetup
from telegram_alerts import alerts as telegram
from trade_logger import TradeLogger

try:
    from trading_ig import IGService
    IG_AVAILABLE = True
except ImportError:
    IG_AVAILABLE = False
    IGService = None


# MAPs derivees automatiquement de ig_rules.RULES (source unique de verite).
# Ne pas modifier ici, modifier src/ig_rules.py si une spec IG change.
from ig_rules import RULES as _IG_RULES   # noqa: E402

EPIC_MAP        = {name: r.epic             for name, r in _IG_RULES.items()}
POINT_VALUE_MAP = {name: r.value_of_one_pip for name, r in _IG_RULES.items()}
MIN_SIZE_MAP    = {name: r.min_deal_size    for name, r in _IG_RULES.items()}
CURRENCY_CODE     = "EUR"
POLL_INTERVAL_SEC = 10
# Delai de grace avant de considerer une position absente d'IG comme fermee :
# IG tarde parfois a publier une position fraiche dans fetch_open_positions.
SYNC_GRACE_SEC = 45
MAX_RETRIES       = 3
RETRY_DELAY_SEC   = 2.0

# Sessions IG v3 expirent apres 1800s (30 min). On refresh proactivement
# a 25 min pour eviter toute fenetre de token expire.
SESSION_REFRESH_SEC = 1500

# NB : _fetch_close_price utilise fetch_account_activity_v2 (match short_id)
# depuis 2026-05-27. Voir docstring de la methode pour les details.
# avec dates dynamiques. Les autres methodes (fetch_account_activity*)
# soit plantent (signature stale), soit retournent vide.


# Gestion active : le SL pose sur IG est un FILET LARGE de catastrophe. Le vrai
# SL (theorique, serre) est gere par le moteur. backstop = theo_sl x ce facteur.
BACKSTOP_SL_MULT = 2.5


@dataclass
class OpenPosition:
    deal_id:      str
    deal_ref:     str
    direction:    str       # "LONG" ou "SHORT"
    direction_ig: str       # "BUY" ou "SELL"
    size:         float
    entry_level:  float
    sl_level:     float     # SL FILET pose sur IG (large)
    tp_level:     float     # TP pose sur IG (= theo, filet)
    atr:          float
    instrument:   str = "XAUUSD"
    signal_id:    Optional[int] = None
    opened_at:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # --- Gestion active (moteur) ---
    epic:         str = ""
    setup_name:   str = ""    # strategie proprietaire (pour evaluer la sortie)
    theo_sl:      float = 0.0  # SL theorique serre (1xATR) gere par le moteur
    theo_tp:      float = 0.0  # TP theorique gere par le moteur
    trail_peak:   float = 0.0  # extreme favorable atteint (pour trailing)
    trail_sl:     float = 0.0  # niveau de trailing stop actif (0 = inactif)


class OrderExecutor:

    def __init__(
        self,
        config:        BonazaConfig,
        risk_managers, # dict[str, RiskManager] OU un seul RiskManager (back-compat)
        trade_logger:  Optional[TradeLogger] = None,
    ) -> None:
        self.config    = config
        # Support dict (multi-instruments) ou un RM unique (legacy XAUUSD-only)
        if isinstance(risk_managers, dict):
            self._rms: Dict[str, RiskManager] = dict(risk_managers)
        else:
            self._rms = {"XAUUSD": risk_managers}
        self._tl       = trade_logger
        self._ig:      Optional[IGService] = None
        self._positions: Dict[str, OpenPosition] = {}
        self._running  = False
        # Reference au data_feed (pose par main.py) pour consulter MARKET_STATE
        # avant de poster un ordre. None = pas de gating (compat).
        self._feed = None
        # Clotures OPU recues du TRADE stream (dealId -> niveaux IG exacts).
        # Alimente par note_trade_event() (main.trade_events_consumer) ; consomme
        # par _sync_positions pour publier le VRAI prix/PnL/mecanisme de cloture
        # au lieu du fallback pnl=0 "(IG)" (fix 2026-06-12).
        self._opu_closes: Dict[str, dict] = {}

    def note_trade_event(self, evt: dict) -> None:
        """Memorise les clotures de positions (OPU status=DELETED) recues en
        push (<100 ms apres execution serveur IG)."""
        try:
            import json as _json
            raw = evt.get("opu")
            if not raw:
                return
            d = _json.loads(raw) if isinstance(raw, str) else raw
            if d.get("status") != "DELETED":
                return
            deal_id = str(d.get("dealId") or "")
            if not deal_id:
                return
            self._opu_closes[deal_id] = {
                "close": float(d.get("level") or 0.0),
                "open":  float(d.get("openLevel") or 0.0),
                "stop":  float(d["stopLevel"]) if d.get("stopLevel") else None,
                "limit": float(d["limitLevel"]) if d.get("limitLevel") else None,
                "dir":   str(d.get("direction") or ""),
                "ts":    str(d.get("timestamp") or ""),
            }
            if len(self._opu_closes) > 200:        # borne memoire
                for k in list(self._opu_closes)[:100]:
                    self._opu_closes.pop(k, None)
        except Exception as e:
            logger.debug(f"note_trade_event : {e}")

    @staticmethod
    def _close_mechanism(o: dict) -> str:
        """Mecanisme de cloture deduit des niveaux IG au moment du DELETE :
        ~limite => TP ; ~stop => SL / BE / SL_CLIQUET selon la position du stop
        par rapport a l'entree. Fallback SL_OR_TP."""
        close, stop, lim, op = o["close"], o.get("stop"), o.get("limit"), o["open"]
        short = o.get("dir") == "SELL"
        cands = []
        if lim:
            cands.append(("TP", abs(close - lim)))
        if stop:
            cands.append(("STOP", abs(close - stop)))
        if not cands:
            return "SL_OR_TP"
        kind, dist = min(cands, key=lambda x: x[1])
        if dist > 1.5:
            return "MANUAL_OR_OTHER"
        if kind == "TP":
            return "TP"
        profit_side = (op - stop) if short else (stop - op)
        if profit_side > 0.3:
            return "SL_CLIQUET"
        if profit_side >= -0.3:
            return "BE"
        return "SL"

    def _rm(self, instrument: str) -> RiskManager:
        """Renvoie le RiskManager associe a un instrument, fallback XAUUSD."""
        return self._rms.get(instrument) or self._rms["XAUUSD"]

    @property
    def rm(self) -> RiskManager:
        """Back-compat : ancien attribut self.rm (XAUUSD par defaut)."""
        return self._rms.get("XAUUSD") or next(iter(self._rms.values()))

    async def connect(self, ig_service=None) -> bool:
        if ig_service is not None:
            self._ig = ig_service
            logger.info("OrderExecutor : session IG partagée avec data_feed")
            return True
        if not IG_AVAILABLE:
            logger.error("trading_ig non installé")
            return False
        cfg = self.config.ig
        if not cfg.is_valid():
            logger.error("Credentials IG incomplets")
            return False
        try:
            self._ig = IGService(
                username   = cfg.identifier,
                password   = cfg.password,
                api_key    = cfg.api_key,
                acc_type   = cfg.account_type,
                acc_number = cfg.account_id or None,
            )
            session  = self._ig.create_session()
            balance  = session.get("accountInfo", {}).get("available", "?")
            currency = session.get("currencyIsoCode", "EUR")
            logger.info(f"OrderExecutor connecté | Solde: {balance} {currency}")
            return True
        except Exception as e:
            logger.error(f"Connexion IG échouée : {e}")
            return False

    async def disconnect(self) -> None:
        self._running = False
        logger.info("OrderExecutor déconnecté")

    async def _refresh_session(self) -> bool:
        """
        Re-authentifie completement (fallback). Cher mais sur : appele uniquement
        en dernier recours quand refresh_session() via refresh_token a echoue.
        """
        if not self._ig:
            return False
        try:
            await asyncio.to_thread(self._ig.create_session, version="3")
            valid_until = getattr(self._ig, "_valid_until", None)
            logger.info(f"Session IG re-authentifiee | valide jusqu'a {valid_until}")
            return True
        except Exception as e:
            logger.error(f"Echec re-auth session IG : {e}")
            return False

    async def _proactive_refresh(self) -> bool:
        """
        Refresh proactif rapide via refresh_token (1 appel HTTP /session/refresh-token).
        Appele par session_keeper toutes les SESSION_REFRESH_SEC. Si refresh_token
        est aussi expire ou invalide, fallback sur re-auth complete.
        """
        if not self._ig:
            return False
        try:
            await asyncio.to_thread(self._ig.refresh_session)
            valid_until = getattr(self._ig, "_valid_until", None)
            logger.info(f"Session IG rafraichie | valide jusqu'a {valid_until}")
            return True
        except Exception as e:
            logger.warning(f"refresh_session a echoue ({e}), tentative re-auth...")
            return await self._refresh_session()

    async def session_keeper(self) -> None:
        """
        Tache de fond : refresh la session IG REST toutes les SESSION_REFRESH_SEC
        pour ne jamais laisser le token v3 expirer (expires_in = 1800s = 30min).
        Lancee en parallele dans main.py.
        """
        logger.info(
            f"Session keeper actif | refresh toutes les {SESSION_REFRESH_SEC}s "
            f"(token v3 dure 1800s)"
        )
        try:
            while True:
                await asyncio.sleep(SESSION_REFRESH_SEC)
                await self._proactive_refresh()
        except asyncio.CancelledError:
            logger.info("Session keeper arrete")
            raise

    async def close_resolver(self, interval_sec: int = 180) -> None:
        """Tache de fond : re-resout les trades CLOSED dont pnl_eur=0 via
        fetch_account_activity_v2. Necessaire car IG met parfois plusieurs
        minutes a publier l'activite de fermeture, donc l'appel sync
        immediat dans _fetch_close_price echoue souvent.
        """
        logger.info(f"close_resolver actif | scan DB toutes les {interval_sec}s")
        try:
            while True:
                await asyncio.sleep(interval_sec)
                try:
                    n = await self._resolve_pending_closes()
                    if n > 0:
                        logger.info(f"close_resolver : {n} trade(s) PnL resolu(s)")
                except Exception as e:
                    logger.warning(f"close_resolver/iter : {e}")
        except asyncio.CancelledError:
            logger.info("close_resolver arrete")
            raise

    async def _resolve_pending_closes(self) -> int:
        """Scan SQLite : pour chaque trade CLOSED avec pnl_eur=0 ou null
        et ts_close des dernieres 12h, tente de retrouver le prix de close
        reel via activity_v2 et met a jour exit_price + pnl_eur.

        Retourne le nb de trades resolus.
        """
        if self._ig is None or self._tl is None:
            return 0

        import sqlite3
        from datetime import datetime, timedelta, timezone

        db_path = self._tl.db_path
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, position_id, direction, size, entry_price
                FROM trades
                WHERE status='CLOSED'
                  AND (pnl_eur IS NULL OR pnl_eur = 0.0)
                  AND ts_close >= datetime('now', '-12 hours')
            """)
            rows = cur.fetchall()
            if not rows:
                conn.close()
                return 0
        except Exception as e:
            logger.warning(f"_resolve_pending_closes/SQL : {e}")
            return 0

        # Fetch activity_v2 sur 24h pour couvrir tous les trades a resoudre
        now   = datetime.now(tz=timezone.utc)
        start = now - timedelta(hours=24)
        try:
            result = await self._ig_call(
                self._ig.fetch_account_activity_v2, start, now
            )
            items = self._normalize_rows(result, "activities")
        except Exception as e:
            logger.warning(f"_resolve_pending_closes/activity_v2 : {e}")
            conn.close()
            return 0

        # Lookup short_id -> close level (extrait du champ result "Position(s)
        # fermee(s): KH7UMPBB")
        lookup: Dict[str, float] = {}
        for item in items:
            res = str(item.get("result") or "")
            if ":" in res and ("ferm" in res.lower() or "close" in res.lower()):
                short = res.split(":")[-1].strip()
                lvl = item.get("level")
                if lvl is not None and short:
                    try:
                        lookup[short] = float(str(lvl).replace(",", "."))
                    except (ValueError, TypeError):
                        pass

        if not lookup:
            conn.close()
            return 0

        resolved = 0
        for trade_id, pos_id, direction, size, entry in rows:
            short = (pos_id or "")[-8:]
            if not short or short not in lookup:
                continue
            exit_price = lookup[short]
            if direction in ("LONG", "BUY"):
                pnl_pts = exit_price - entry
            else:
                pnl_pts = entry - exit_price
            # 1 EUR / pt sur XAUUSD-mini, DAX-mini, CAC40-mini (point_value=1)
            pnl_eur = pnl_pts * float(size) * 1.0
            try:
                cur.execute(
                    "UPDATE trades SET exit_price = ?, pnl_eur = ? WHERE id = ?",
                    (exit_price, pnl_eur, trade_id),
                )
                resolved += 1
                logger.info(
                    f"close_resolver : #{trade_id} {direction} entry={entry:.2f} "
                    f"exit={exit_price:.2f} pnl={pnl_eur:+.2f}EUR (activity_v2)"
                )
            except Exception as e:
                logger.warning(f"_resolve_pending_closes/UPDATE #{trade_id} : {e}")

        try:
            conn.commit()
        finally:
            conn.close()
        return resolved

    async def _ig_call(self, method, *args, **kwargs):
        """
        Appelle une methode IGService en thread avec deux garde-fous :
          1. PRE-CHECK : si _valid_until est depasse, refresh avant l'appel
             (no-op si la session est encore valide)
          2. POST-CHECK : si on recoit malgre tout un 401 token-invalid,
             on tente un refresh + 1 retry
        """
        # 1. Pre-check via _check_session() natif de trading-ig
        if self._ig is not None:
            try:
                await asyncio.to_thread(self._ig._check_session)
            except Exception as e:
                logger.debug(f"_check_session erreur (continu) : {e}")

        # 2. Appel reel, avec filet sur 401
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except Exception as e:
            es = str(e)
            is_token_401 = ("401" in es and
                            ("client-token-invalid" in es or "token" in es.lower()))
            if is_token_401:
                logger.warning("Token IG expire malgre pre-check - refresh + 1 retry")
                if await self._proactive_refresh():
                    return await asyncio.to_thread(method, *args, **kwargs)
            raise

    async def run_poll(self) -> None:
        self._running = True
        logger.info("OrderExecutor polling positions...")
        while self._running:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            try:
                await self._sync_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"run_poll : {e}")

    def _is_real_account(self) -> bool:
        """True si le compte IG est reel (CFD, SPREADBET, LIVE), False si DEMO."""
        return self.config.ig.account_type.upper() not in ("DEMO",)

    def _check_kill_switch(self, context: str = "") -> bool:
        """Lit data/kill_switch.flag (pose par le bot Telegram /kill).
        Si present, refuse tout nouvel ordre. Retourne False = ordre refuse."""
        from pathlib import Path
        flag = Path(self.config.db.path).parent / "kill_switch.flag"
        if flag.exists():
            try:
                reason = flag.read_text(encoding="utf-8").strip()[:200]
            except Exception:
                reason = "no_reason"
            logger.warning(f"[KILL] {context} bloque par kill_switch.flag : {reason}")
            try:
                telegram().send(
                    f"🛑 Ordre bloque par /kill\n{context}\nRaison : {reason}",
                    parse_mode=None,
                )
            except Exception:
                pass
            return False
        return True

    def _check_live_authorized(self, context: str = "") -> bool:
        """
        Double verrou anti-live :
          - Si compte reel (non-DEMO) OU BONAZA_MODE=LIVE
          - REQUIERT : ALLOW_LIVE_TRADING=true ET CONFIRM_LIVE_TRADING="I_UNDERSTAND_THE_RISK"
        Retourne False (= refuser l'ordre) si check echoue.
        Log CRITICAL + notif Telegram en cas de blocage.
        """
        ig_type = self.config.ig.account_type.upper()
        mode    = self.config.trading.mode.upper()
        will_be_live = self._is_real_account() or mode == "LIVE"

        if not will_be_live:
            # Compte DEMO + mode PAPER : OK, on continue (pas besoin de verrou)
            return True

        if self.config.security.live_authorized:
            # Verrou active : on autorise
            return True

        # BLOCAGE
        details = (
            f"IG_ACCOUNT_TYPE={ig_type} (reel={self._is_real_account()}) | "
            f"BONAZA_MODE={mode} | "
            f"ALLOW_LIVE_TRADING={self.config.security.allow_live} | "
            f"CONFIRM_LIVE_TRADING={'OK' if self.config.security.confirm_live=='I_UNDERSTAND_THE_RISK' else 'MANQUANT/INVALIDE'}"
        )
        logger.critical(f"[LIVE BLOQUE] {context} | {details}")
        # parse_mode=None car le contenu contient des underscores qui cassent Markdown
        try:
            telegram().send(
                f"🛑 ORDRE LIVE BLOQUE\n"
                f"{context}\n"
                f"{details}\n\n"
                f"Pour autoriser, .env doit contenir :\n"
                f"  ALLOW_LIVE_TRADING=true\n"
                f"  CONFIRM_LIVE_TRADING=I_UNDERSTAND_THE_RISK",
                parse_mode=None,
            )
        except Exception:
            pass
        return False

    async def _handle_signal(
        self,
        setup:      TradeSetup,
        signal_id:  Optional[int] = None,
        instrument: Optional[str] = None,
        exact_levels: bool = False,
    ) -> Optional[str]:
        """exact_levels=True (copieur Telegram) : on pose sur IG le SL/TP EXACT du
        signal (pas le filet large), et theo_sl/theo_tp=0 -> manage_positions ignore
        la position (geree par le lecteur via breakeven/close). Retourne le deal_id."""
        if not self._ig:
            logger.error("OrderExecutor non connecté")
            return

        # L'instrument DOIT etre fourni par l'appelant (signal_task connait la
        # bonne queue). Fallback legacy : derivation depuis le nom de setup Bv3.
        # Sans ca, un setup portfolio (ex "S8_RegimeAdaptive") ne se derive pas
        # et tombe sur l'EPIC XAUUSD par defaut -> ordre sur le mauvais marche.
        if instrument is None:
            instrument = setup.setup_name.replace("SETUP_B_Bv3_", "")

        # === KILL SWITCH externe (fichier flag pose par /kill Telegram) ===
        if not self._check_kill_switch(f"_handle_signal {instrument} {setup.direction.value}"):
            return

        # === VERROU ANTI-LIVE (deuxieme check, avant tout le reste) ===
        if not self._check_live_authorized(f"_handle_signal {instrument} {setup.direction.value}"):
            return

        rm           = self._rm(instrument)
        if rm.kill_switch_active:
            logger.warning(f"[{instrument}] Kill switch actif — signal ignoré")
            return

        # Resolution des regles IG (sondees, source unique de verite)
        rules = rules_for(instrument)
        if rules is None:
            # Fallback : EPIC inconnu, on tente quand meme avec defauts conservateurs
            logger.warning(f"[{instrument}] aucune ig_rules — fallback defauts")
            epic = EPIC_MAP.get(instrument, EPIC_MAP["XAUUSD"])
        else:
            epic = rules.epic

        # === GARDE MARKET_STATE : ne pas poster dans un marche non TRADEABLE ===
        # (CLOSED / EDITS_ONLY / SUSPENDED / OFFLINE). On ne bloque PAS si l'etat
        # est inconnu (None : flux MARKET pas encore recu) pour ne pas geler le
        # trading au demarrage.
        if self._feed is not None:
            try:
                mstate = self._feed.get_market_state(epic)
            except Exception:
                mstate = None
            if mstate is not None and mstate != "TRADEABLE":
                logger.warning(
                    f"[{instrument}] Ordre refuse : MARKET_STATE={mstate} (epic {epic})"
                )
                try:
                    telegram().send(
                        f"⛔ Ordre {instrument} {setup.direction.value} refuse\n"
                        f"Marche {mstate} (epic {epic})",
                        parse_mode=None,
                    )
                except Exception:
                    pass
                return

        direction_ig = "BUY" if setup.direction == SignalDirection.LONG else "SELL"

        # --- Pre-validation et auto-ajustement (anti-erreurs IG) ---
        entry       = setup.entry
        sl_dist_req = abs(setup.entry - setup.stop_loss)
        # take_profit<=0 -> pas de TP (ex TP3 'open' du copieur)
        tp_dist_req = (abs(setup.take_profit - setup.entry)
                       if setup.take_profit and setup.take_profit > 0 else 0.0)
        size_req    = setup.size

        has_tp = bool(tp_dist_req and tp_dist_req > 0)
        if rules:
            size, _ = rules.adjust_size(size_req)
            decimals = rules.decimal_places
            if exact_levels:
                # COPIEUR : SL/TP EXACTS du signal (ajustes au min IG), pas de filet
                ig_sl_dist, _ = rules.adjust_stop_distance(sl_dist_req)
                ig_tp_dist = rules.adjust_limit_distance(tp_dist_req)[0] if has_tp else 0.0
            else:
                # BREAKOUT : SL IG = filet large, le moteur gere le SL theorique serre
                ig_sl_dist, _ = rules.adjust_stop_distance(sl_dist_req * BACKSTOP_SL_MULT)
                ig_tp_dist = rules.adjust_limit_distance(tp_dist_req)[0] if has_tp else 0.0
        else:
            size = max(size_req, MIN_SIZE_MAP.get(instrument, 0.1)); decimals = 2
            mult = 1.0 if exact_levels else BACKSTOP_SL_MULT
            ig_sl_dist = sl_dist_req * mult
            ig_tp_dist = tp_dist_req if has_tp else 0.0

        if direction_ig == "BUY":
            sl_level = round(entry - ig_sl_dist, decimals)
            tp_level = round(entry + ig_tp_dist, decimals) if ig_tp_dist > 0 else None
        else:
            sl_level = round(entry + ig_sl_dist, decimals)
            tp_level = round(entry - ig_tp_dist, decimals) if ig_tp_dist > 0 else None

        if exact_levels:
            theo_sl_lvl = 0.0; theo_tp_lvl = 0.0   # copieur : pas de gestion moteur
        elif direction_ig == "BUY":
            theo_sl_lvl = round(entry - sl_dist_req, decimals)
            theo_tp_lvl = round(entry + tp_dist_req, decimals)
        else:
            theo_sl_lvl = round(entry + sl_dist_req, decimals)
            theo_tp_lvl = round(entry - tp_dist_req, decimals)

        logger.info(
            f"[{instrument}] Ouverture {direction_ig} {size:.2f}L "
            f"{'(COPIE)' if exact_levels else ''}| E={entry} | "
            f"SL_IG={sl_level} TP_IG={tp_level} | "
            f"{'exact' if exact_levels else 'filet+moteur'}"
        )

        pos = await self._open_position(
            epic, direction_ig, size,
            entry, sl_level, tp_level,
            setup.risk_pts, instrument, signal_id,
            decimals=decimals,
            setup_name=setup.setup_name,
            theo_sl=theo_sl_lvl, theo_tp=theo_tp_lvl,
        )

        if pos:
            self._positions[pos.deal_id] = pos
            rm.on_fill(
                position_id = pos.deal_id,
                direction   = pos.direction,   # "LONG" ou "SHORT"
                entry       = pos.entry_level,
                sl          = pos.sl_level,
                tp          = pos.tp_level,
                size        = pos.size,
                setup_name  = setup.setup_name,
                instrument  = instrument,
                atr         = pos.atr,
            )
            # Notification Telegram (silencieuse si non configure)
            telegram().position_opened(
                instrument=instrument, direction=pos.direction,
                size=pos.size, deal_id=pos.deal_id,
                entry=pos.entry_level, sl=pos.sl_level, tp=pos.tp_level,
            )
            if self._tl:
                self._tl.log_fill(
                    position_id = pos.deal_id,
                    direction   = pos.direction,
                    entry       = pos.entry_level,
                    sl          = pos.sl_level,
                    tp          = pos.tp_level,
                    size        = pos.size,
                    signal_id   = signal_id,
                )
            return pos.deal_id
        return None

    async def _open_position(
        self, epic, direction_ig, size, entry, sl, tp, atr, instrument,
        signal_id=None, decimals=2, setup_name="", theo_sl=0.0, theo_tp=0.0,
    ) -> Optional[OpenPosition]:
        # === KILL SWITCH externe (defense en profondeur) ===
        if not self._check_kill_switch(f"_open_position {instrument} {direction_ig} {size}L"):
            return None

        # === VERROU ANTI-LIVE (defense en profondeur, au cas ou appele directement) ===
        if not self._check_live_authorized(f"_open_position {instrument} {direction_ig} {size}L"):
            return None

        sl_dist = (round(entry - sl, decimals) if direction_ig == "BUY"
                   else round(sl - entry, decimals))
        # tp=None -> position SANS take-profit (ex TP3 "open" du copieur)
        tp_dist = None
        if tp is not None:
            tp_dist = (round(tp - entry, decimals) if direction_ig == "BUY"
                       else round(entry - tp, decimals))
        if sl_dist <= 0 or (tp_dist is not None and tp_dist <= 0):
            logger.error(f"Distances invalides sl={sl_dist} tp={tp_dist}")
            return None
        direction_rm = "LONG" if direction_ig == "BUY" else "SHORT"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self._ig_call(
                    self._ig.create_open_position,
                    currency_code=CURRENCY_CODE, direction=direction_ig,
                    epic=epic, expiry="-", force_open=True,
                    guaranteed_stop=False, order_type="MARKET",
                    size=size,
                    stop_distance=sl_dist, stop_level=None,
                    limit_distance=tp_dist, limit_level=None,
                    level=None, quote_id=None,
                    trailing_stop=False, trailing_stop_increment=None,
                )
                status  = response.get("dealStatus", "")
                reason  = response.get("reason", "")
                deal_id = response.get("dealId", "")
                lvl     = response.get("level", entry)
                if status == "ACCEPTED":
                    actual_entry = float(lvl) if lvl else entry
                    logger.info(
                        f"Position ouverte | {direction_ig} {size}L | "
                        f"{deal_id} | {actual_entry}"
                    )
                    return OpenPosition(
                        deal_id=deal_id, deal_ref=response.get("dealReference", ""),
                        direction=direction_rm, direction_ig=direction_ig,
                        size=size, entry_level=actual_entry,
                        sl_level=sl, tp_level=(tp if tp is not None else 0.0), atr=atr,
                        instrument=instrument, signal_id=signal_id,
                        epic=epic, setup_name=setup_name,
                        theo_sl=theo_sl, theo_tp=theo_tp,
                        trail_peak=actual_entry,
                    )
                else:
                    logger.warning(
                        f"Ordre rejeté ({attempt}/{MAX_RETRIES}) : {status}/{reason}"
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY_SEC)
            except Exception as e:
                logger.error(f"_open_position ({attempt}/{MAX_RETRIES}) : {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY_SEC)
        return None

    @staticmethod
    def _normalize_rows(resp, key: str = "positions") -> list:
        """
        Normalise les reponses de trading-ig en list[dict].

        trading-ig retourne soit :
          - un pandas DataFrame (les colonnes 'position' et 'market' sont
            flatten au meme niveau : dealId, direction, size, openLevel, ...
            sont directement accessibles via row['dealId'])
          - un dict du type {key: [...]} si return_dataframe=False
          - une liste deja

        Cette fonction renvoie TOUJOURS une list[dict] exploitable, ou [].
        """
        if resp is None:
            return []
        # DataFrame -> records
        if hasattr(resp, "to_dict") and not isinstance(resp, dict):
            try:
                return resp.to_dict("records") or []
            except Exception:
                return []
        if isinstance(resp, dict):
            inner = resp.get(key, [])
            return inner if isinstance(inner, list) else []
        if isinstance(resp, list):
            return resp
        return []

    @staticmethod
    def _row_deal_id(row: dict) -> str:
        """
        Extrait le dealId d'une ligne IG, qu'elle vienne d'un DataFrame flatten
        ('dealId' au top level) ou d'un dict nested ('position.dealId').
        """
        if not isinstance(row, dict):
            return ""
        if row.get("dealId"):
            return str(row.get("dealId"))
        pos = row.get("position")
        if isinstance(pos, dict) and pos.get("dealId"):
            return str(pos.get("dealId"))
        return ""

    async def _sync_positions(self) -> None:
        if not self._positions or not self._ig:
            return
        try:
            response   = await self._ig_call(self._ig.fetch_open_positions)
            rows       = self._normalize_rows(response, "positions")
            open_deals = {
                self._row_deal_id(r): r
                for r in rows
                if self._row_deal_id(r)
            }
        except Exception as e:
            logger.warning(f"Sync positions : {e}")
            return

        now = datetime.now(timezone.utc)
        for deal_id in list(self._positions):
            if deal_id not in open_deals:
                pos = self._positions[deal_id]
                # GRACE : IG met parfois quelques secondes a publier une position
                # fraichement ouverte dans fetch_open_positions. Ne PAS la considerer
                # comme fermee si elle vient d'etre ouverte (sinon fausse-cloture +
                # zombie DB + orphelin, cf #53/#57/#72).
                age = (now - pos.opened_at).total_seconds()
                if age < SYNC_GRACE_SEC:
                    logger.debug(f"_sync : {deal_id} absente d'IG mais age={age:.0f}s "
                                 f"< {SYNC_GRACE_SEC}s -> on attend (anti fausse-cloture)")
                    continue
                pos = self._positions.pop(deal_id)
                # 1) Source prioritaire : OPU DELETED recu en push (prix IG exact,
                #    mecanisme deductible). 2) Fallback : activity_v2 (retardee)
                #    puis pnl=0 a rattraper par close_resolver.
                opu = self._opu_closes.pop(deal_id, None)
                if opu and opu.get("close"):
                    exit_price = float(opu["close"])
                    pnl = self._calc_pnl(pos, exit_price)
                    pnl_src = "OPU"
                    reason = self._close_mechanism(opu)
                else:
                    exit_price, pnl_from_ig = await self._fetch_close_price(deal_id, pos)
                    reason = "SL_OR_TP"
                    # Prefere le PnL fourni par IG (spread inclus, autoritaire).
                    # Sinon recalcule cote Bonaza (approx, sans spread).
                    if pnl_from_ig is not None:
                        pnl = pnl_from_ig
                        pnl_src = "IG"
                    else:
                        pnl = self._calc_pnl(pos, exit_price)
                        pnl_src = "calc"
                logger.info(
                    f"Position fermée | {pos.direction} {pos.size}L | "
                    f"{deal_id} | exit={exit_price:.2f} pnl={pnl:+.2f} EUR "
                    f"({pnl_src}) via {reason}"
                )
                self._rm(pos.instrument).on_close(
                    position_id=deal_id, exit_price=exit_price, reason=reason
                )
                # Notification Telegram
                telegram().position_closed(
                    instrument=pos.instrument, direction=pos.direction,
                    size=pos.size, entry=pos.entry_level,
                    exit_price=exit_price, pnl_eur=pnl, reason=reason,
                )
                if self._tl:
                    self._tl.log_close(
                        position_id=deal_id, exit_price=exit_price,
                        reason=reason, pnl_eur=pnl,
                    )

    async def _fetch_close_price(
        self, deal_id: str, pos: OpenPosition
    ) -> tuple[float, Optional[float]]:
        """
        Récupère le prix de clôture réel via fetch_account_activity_v2.

        Matching : les 8 derniers chars du dealId IG (court position id)
        apparaissent dans le champ `result` de la transaction de fermeture,
        sous la forme "Position(s) fermée(s): <short_id>". Le `level` de
        cette activité = close_lvl exact (spread inclus côté IG).

        Le PnL EUR exact (via spread) est dans fetch_transaction_history
        mais nécessite un second matching imprécis ; on retourne donc None
        en pnl_eur et le caller recalcule via _calc_pnl (équivalent au cent
        près sur XAUUSD/DAX/CAC40 mini).

        Returns:
            (close_lvl, None)        si match trouvé
            (entry_level, 0.0)       fallback
        """
        from datetime import datetime, timedelta, timezone
        short_id = deal_id[-8:] if deal_id else ""
        if not short_id:
            return pos.entry_level, 0.0

        now   = datetime.now(tz=timezone.utc)
        start = now - timedelta(hours=24)
        try:
            result = await self._ig_call(
                self._ig.fetch_account_activity_v2, start, now
            )
            items = self._normalize_rows(result, "activities")
            for item in items:
                res = str(item.get("result") or "")
                if short_id in res and ("ferm" in res.lower() or "close" in res.lower()):
                    lvl = item.get("level")
                    if lvl is not None:
                        try:
                            return float(str(lvl).replace(",", ".")), None
                        except (ValueError, TypeError):
                            pass
            logger.debug(
                f"_fetch_close_price : short_id={short_id} absent des "
                f"{len(items)} activities des dernieres 24h"
            )
        except Exception as e:
            logger.warning(f"_fetch_close_price/activity_v2 : {e}")

        logger.warning(
            f"Prix clôture indisponible pour {deal_id} (short={short_id}) → "
            f"fallback entry={pos.entry_level:.2f} (P&L = 0)"
        )
        return pos.entry_level, 0.0

    def _calc_pnl(self, pos: OpenPosition, exit_price: float) -> float:
        pts = (exit_price - pos.entry_level if pos.direction == "LONG"
               else pos.entry_level - exit_price)
        return round(pts * pos.size * POINT_VALUE_MAP.get(pos.instrument, 1.0), 2)

    def open_positions(self) -> list:
        """Liste des OpenPosition actuellement suivies (pour la gestion active)."""
        return list(self._positions.values())

    async def move_stop_to(self, deal_id: str, stop_level: float) -> bool:
        """Deplace le SL d'une position (ex breakeven du copieur Telegram)."""
        pos = self._positions.get(deal_id)
        if pos is None:
            return False
        try:
            await self._ig_call(
                self._ig.update_open_position,
                limit_level=(pos.tp_level if pos.tp_level else None),
                stop_level=round(float(stop_level), 2),
                deal_id=deal_id,
            )
            pos.sl_level = round(float(stop_level), 2)
            logger.info(f"[{pos.instrument}] SL deplace -> {pos.sl_level} ({deal_id})")
            return True
        except Exception as e:
            logger.error(f"move_stop_to {deal_id} : {e}")
            return False

    async def reload_open_positions(self) -> int:
        """Au DEMARRAGE : recharge les positions deja ouvertes chez IG dans le
        registre, pour ne PAS les orpheliner apres un redeploiement. Marquees
        RELOADED (sans theo SL/TP) -> manage_positions les ignore, elles tournent
        sur leur SL/TP IG existant ; _sync_positions detectera leur cloture."""
        if not self._ig:
            return 0
        try:
            resp = await self._ig_call(self._ig.fetch_open_positions)
            rows = self._normalize_rows(resp, "positions")
        except Exception as e:
            logger.warning(f"reload_open_positions : {e}")
            return 0
        from instruments import INSTRUMENTS
        epic_to_inst = {i.epic: n for n, i in INSTRUMENTS.items()}
        n = 0
        for r in rows:
            deal_id = self._row_deal_id(r)
            if not deal_id or deal_id in self._positions:
                continue
            direction_ig = str(r.get("direction") or "").upper()
            if direction_ig not in ("BUY", "SELL"):
                continue
            epic = r.get("epic") or (r.get("market", {}) or {}).get("epic") or ""
            def _f(*keys):
                for k in keys:
                    v = r.get(k)
                    if v is not None:
                        try: return float(v)
                        except (TypeError, ValueError): pass
                return 0.0
            size = _f("size", "dealSize"); entry = _f("level", "openLevel")
            sl = _f("stopLevel"); tp = _f("limitLevel")
            if size <= 0 or entry <= 0:
                continue
            direction = "LONG" if direction_ig == "BUY" else "SHORT"
            instrument = epic_to_inst.get(epic, "XAUUSD")
            # XAUUSD en LIVE = positions du copieur Telegram -> taguer TG_ pour que
            # le copieur les RE-ADOPTE apres un redemarrage (anti-stacking + gestion
            # breakeven/close). Les autres restent RELOADED (gerees par leur SL/TP IG).
            reloaded_name = "TG_XAU_RELOADED" if instrument == "XAUUSD" else "RELOADED"
            self._positions[deal_id] = OpenPosition(
                deal_id=deal_id, deal_ref="", direction=direction,
                direction_ig=direction_ig, size=size, entry_level=entry,
                sl_level=sl, tp_level=tp, atr=0.0, instrument=instrument,
                epic=epic, setup_name=reloaded_name, theo_sl=0.0, theo_tp=0.0,
                trail_peak=entry,
            )
            try:
                self._rm(instrument).on_fill(
                    position_id=deal_id, direction=direction, entry=entry,
                    sl=sl, tp=tp, size=size, setup_name="RELOADED",
                    instrument=instrument, atr=0.0)
            except Exception as e:
                logger.debug(f"reload on_fill {deal_id} : {e}")
            n += 1
            logger.info(f"[Reload] position IG rechargee : {direction} {size}L "
                        f"{instrument} @ {entry} SL={sl} TP={tp} ({deal_id})")
        if n:
            logger.info(f"[Reload] {n} position(s) IG existante(s) rechargee(s) — anti-orphelin")
        return n

    async def close_position(self, deal_id: str, reason: str = "ENGINE") -> bool:
        """Ferme une position au MARCHE (decision du moteur de strategie).
        Retire du registre + on_close RM + log + Telegram + trade_logger.
        Le filet IG (SL large) reste en place tant que la position vit ; ici on
        ferme AVANT que le filet ne soit touche."""
        pos = self._positions.get(deal_id)
        if pos is None:
            return False
        if not self._check_kill_switch(f"close_position {pos.instrument}"):
            # kill switch : on ne touche pas (laisse le filet IG gerer)
            return False
        direction_close = "SELL" if pos.direction_ig == "BUY" else "BUY"
        try:
            response = await self._ig_call(
                self._ig.close_open_position,
                deal_id=deal_id, direction=direction_close, epic=None, expiry=None,
                level=None, order_type="MARKET", quote_id=None, size=pos.size,
            )
        except Exception as e:
            logger.error(f"[{pos.instrument}] close_position {deal_id} echec : {e}")
            return False
        status = response.get("dealStatus", "") if isinstance(response, dict) else ""
        if status and status != "ACCEPTED":
            logger.warning(f"[{pos.instrument}] close refuse {deal_id} : {status} "
                           f"({response.get('reason')}) — filet IG conserve")
            return False
        # Succes : retirer du registre AVANT _sync_positions pour eviter double on_close
        self._positions.pop(deal_id, None)
        exit_price = None
        if isinstance(response, dict) and response.get("level"):
            try: exit_price = float(response["level"])
            except (ValueError, TypeError): exit_price = None
        if exit_price is None and self._feed is not None:
            exit_price = self._feed.get_price(pos.epic)
        if exit_price is None:
            exit_price = pos.entry_level
        pnl = self._calc_pnl(pos, exit_price)
        logger.info(f"[{pos.instrument}] Position fermee MOTEUR ({reason}) | "
                    f"{pos.direction} {pos.size}L exit={exit_price:.2f} pnl={pnl:+.2f} EUR")
        self._rm(pos.instrument).on_close(
            position_id=deal_id, exit_price=exit_price, reason=reason)
        try:
            telegram().position_closed(
                instrument=pos.instrument, direction=pos.direction, size=pos.size,
                entry=pos.entry_level, exit_price=exit_price, pnl_eur=pnl, reason=reason)
        except Exception:
            pass
        if self._tl:
            self._tl.log_close(position_id=deal_id, exit_price=exit_price,
                               reason=reason, pnl_eur=pnl)
        return True

    def get_status(self) -> dict:
        return {"connected": self._ig is not None, "open_positions": len(self._positions)}


def build_executor(
    config:        BonazaConfig,
    risk_managers, # dict[str, RiskManager] OU un seul RiskManager (back-compat)
    trade_logger:  Optional[TradeLogger] = None,
) -> OrderExecutor:
    return OrderExecutor(
        config=config, risk_managers=risk_managers, trade_logger=trade_logger
    )
