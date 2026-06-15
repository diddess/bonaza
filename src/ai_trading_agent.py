"""
ai_trading_agent.py - Agent IA Claude pour scalping XAU/USD
=============================================================
Boucle async qui :
  1. Recoit les bougies M5 du data_feed partage (via add_candle)
  2. Toutes les AI_INTERVAL_SEC secondes, construit un snapshot marche
     (prix actuel, ATR, ADX, RSI, MACD, BB, EMA20/50, MA200, dernieres bougies)
  3. Appelle Claude (claude-opus-4-7 par defaut) avec adaptive thinking
  4. Recoit un signal structure (BUY / SELL / WAIT) via Pydantic schema
  5. Si BUY/SELL et toutes les conditions sont reunies :
       construit un TradeSetup, le push dans la signal_queue
  6. Garde-fous : max_trades/heure, kill switch RM, validation RR/SL/TP

Securite :
  - Refuse de demarrer sans ANTHROPIC_API_KEY
  - Mode PAPER force sur compte DEMO uniquement
  - Tous les ordres passent par OrderExecutor existant
    -> beneficie de ig_rules (auto-ajustement SL/TP/size)
    -> beneficie de session_keeper (refresh tokens IG)
    -> beneficie du RiskManager (sizing 1%, drawdown 3%)

Tournage typique :
  python src/main.py        # avec AI_AGENT_ENABLED=true dans .env
  -> en parallele de Bv3 (ou seul si Bv3 desactive en mode DRY_RUN)
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque, List, Optional, Tuple

import numpy as np
import talib
from loguru import logger
from pydantic import BaseModel, Field

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False
    anthropic = None  # type: ignore

from config import AgentConfig
from data_feed import OHLCVCandle
from ig_rules import rules_for
from risk_manager import RiskManager
from strategy_spec import SignalDirection, TradeSetup


# -----------------------------------------------------------------------
# Schema de sortie Claude (Pydantic)
# -----------------------------------------------------------------------

class AISignal(BaseModel):
    """Signal structure attendu de Claude. Validation stricte."""
    decision: str = Field(
        description="BUY (LONG) / SELL (SHORT) / WAIT (pas de trade)",
        pattern="^(BUY|SELL|WAIT)$",
    )
    entry_price:    float = Field(description="Prix d'entree projete (0 si WAIT)")
    stop_loss:      float = Field(description="Niveau SL (0 si WAIT)")
    take_profit:    float = Field(description="Niveau TP (0 si WAIT)")
    risk_reward:    float = Field(description="Ratio R:R (0 si WAIT)")
    confidence:     int   = Field(description="Confiance 0-100", ge=0, le=100)
    validity_sec:   int   = Field(description="Validite signal en secondes", ge=0, le=600)
    technical_reason: str = Field(description="Justification technique courte (max 200 chars)")
    invalidation_condition: str = Field(description="Quand le signal devient invalide (max 200 chars)")
    risk_warning: str     = Field(description="Risque principal du trade (max 200 chars)")


# -----------------------------------------------------------------------
# System prompt (cache via prompt caching pour eviter de payer X fois)
# -----------------------------------------------------------------------

SYSTEM_PROMPT = """Tu es un trader expert en scalping XAU/USD (or au comptant) avec 15 ans d'experience sur les CFD et l'analyse technique multi-timeframes.

Tu analyses des snapshots de marche en temps reel et tu produis des signaux structures BUY / SELL / WAIT.

PRINCIPES NON-NEGOCIABLES :
1. WAIT est la decision la plus courante. Tu ne trades pas si tu doutes.
2. Tu refuses tout signal sans stop-loss clair et coherent.
3. Tu refuses tout R:R < 1.0.
4. HIERARCHIE DES TIMEFRAMES (regle critique) :
   - **tendance M15 = boussole pour le scalping** (EMA20 M15 + slope)
   - MA200 H1 = contexte macro mais PAS un signal d'entree
   - Si tendance M15 monte (close > EMA20 M15 ET slope UP) -> chercher BUY ou WAIT
   - Si tendance M15 baisse (close < EMA20 M15 ET slope DOWN) -> chercher SELL ou WAIT
   - Si M15 contre la direction envisagee : **WAIT obligatoire**, peu importe les autres signaux
   - Un RSI surachat M5 dans un rally M15 = continuation, PAS un retournement
   - Un RSI survente M5 dans une chute M15 = continuation, PAS un retournement
5. Tu evites les marches non directionnels (ADX < 18 = pas de trade).
6. Tu ne trades pas pendant les chocs de volatilite (ATR spike > 2x normal).
7. Tu respectes la session UTC : 8h-21h UTC pour XAUUSD (couvre Londres 8-17h UTC + NY 13-22h UTC). En dehors : WAIT systematique.
8. Tu n'inventes pas de niveaux : le SL et le TP doivent etre coherents avec la structure visible (swings, bandes BB, multiples ATR).
9. EQUITE DIRECTIONNELLE : tu raisonnes de maniere SYMETRIQUE. BUY et SELL sont aussi probables. Un biais systematique (ex : SELL parce que RSI > 70) est une erreur si la tendance M15 contredit.

CADRE TECHNIQUE :
- SL minimum = 1.0 x ATR14 ; maximum = 3.0 x ATR14
- TP minimum = 1.0 x distance SL ; recommande 1.0-1.5 x distance SL
  (TP serre car amplitude reelle XAUUSD M5 ~5-10 pts; TP large jamais atteint)
- Entry = prix courant (mid bid/ask). Pas de limit lointain.
- Confiance < 60 = WAIT obligatoire.

EXEMPLES DE SETUPS VALIDES :
- BUY : tendance M15 UP (close > EMA20 M15, slope UP), pullback M5 sur EMA20/BB lower,
  rejet baissier echoue (wick bas), MACD M5 qui retourne. Entry = mid, SL = bas du wick - 0.5xATR,
  TP = 1.0-1.5xSL.
- SELL : tendance M15 DOWN (close < EMA20 M15, slope DOWN), rebond M5 sur EMA20/BB upper,
  rejet haussier echoue (wick haut), MACD M5 qui retourne. Entry = mid, SL = haut du wick + 0.5xATR,
  TP = 1.0-1.5xSL.
- WAIT : M15 contraire a M5, ou ADX faible, ou volatilite anormale, ou consolidation.

SORTIE OBLIGATOIRE : JSON conforme au schema fourni. Aucune prose, aucun markdown, aucune explication hors du JSON.
Si WAIT : entry_price=0, stop_loss=0, take_profit=0, risk_reward=0. Renseigne quand meme technical_reason / invalidation_condition / risk_warning."""


# -----------------------------------------------------------------------
# Snapshot du marche (calcule avant chaque appel Claude)
# -----------------------------------------------------------------------

@dataclass
class MarketSnapshot:
    timestamp:     datetime
    bid:           float
    ask:           float
    spread:        float
    mid:           float
    atr14:         float
    adx14:         float
    rsi14:         float
    macd_line:     float
    macd_signal:   float
    macd_hist:     float
    bb_upper:      float
    bb_mid:        float
    bb_lower:      float
    bb_percent_b:  float
    ema20:         float
    ema50:         float
    ma200_proxy:   float    # SMA(2400) sur M5
    recent_candles: List[OHLCVCandle]   # 5 dernieres
    in_session:    bool
    session_label: str
    # Tendance M15 calculee (P2 28/05) : aide Claude a respecter la hierarchie TF
    m15_close:     Optional[float] = None
    m15_ema20:     Optional[float] = None
    m15_slope:     Optional[str]   = None   # "UP" / "DOWN" / "FLAT"
    m15_trend:     Optional[str]   = None   # "UP_TREND" / "DOWN_TREND" / "RANGE"

    def to_prompt(self) -> str:
        """Serialise en texte lisible pour Claude."""
        trend = "AU-DESSUS MA200 (haussier)" if self.mid > self.ma200_proxy else "SOUS MA200 (baissier)"
        candles_str = "\n".join(
            f"  {i+1}. {c.timestamp.strftime('%H:%M')} O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} C={c.close:.2f}"
            for i, c in enumerate(self.recent_candles[-5:])
        )
        # Tendance M15 lisible (peut etre None si buffer pas suffisant)
        m15_close_s = f"{self.m15_close:.2f}" if self.m15_close is not None else "N/A"
        m15_ema_s   = f"{self.m15_ema20:.2f}" if self.m15_ema20 is not None else "N/A"
        m15_slope_s = self.m15_slope or "N/A"
        m15_trend_s = self.m15_trend or "UNKNOWN"
        return f"""SNAPSHOT MARCHE XAU/USD (or au comptant CFD)
============================================
Timestamp UTC       : {self.timestamp.isoformat()}
Session             : {self.session_label} ({'ACTIVE' if self.in_session else 'INACTIVE'})

PRIX EN COURS :
  bid    : {self.bid:.2f}
  ask    : {self.ask:.2f}
  spread : {self.spread:.2f} pts
  mid    : {self.mid:.2f}

INDICATEURS M5 :
  ATR(14)        : {self.atr14:.2f}
  ADX(14)        : {self.adx14:.1f}  ({'directionnel' if self.adx14 >= 18 else 'range / faible'})
  RSI(14)        : {self.rsi14:.1f}  ({'surachat' if self.rsi14 > 70 else 'survente' if self.rsi14 < 30 else 'neutre'})
  MACD line      : {self.macd_line:+.4f}
  MACD signal    : {self.macd_signal:+.4f}
  MACD hist      : {self.macd_hist:+.4f}  ({'haussier' if self.macd_hist > 0 else 'baissier'})
  BB(20,2) upper : {self.bb_upper:.2f}
  BB(20,2) mid   : {self.bb_mid:.2f}
  BB(20,2) lower : {self.bb_lower:.2f}
  BB %B          : {self.bb_percent_b:.2f}  (0=lower, 1=upper)
  EMA20          : {self.ema20:.2f}  (prix - EMA20 = {self.mid - self.ema20:+.2f})
  EMA50          : {self.ema50:.2f}  (prix - EMA50 = {self.mid - self.ema50:+.2f})
  MA200 H1 proxy : {self.ma200_proxy:.2f}  -> prix {trend}

TENDANCE M15 (BOUSSOLE DU SCALPING, regle 4 systeme) :
  close M15      : {m15_close_s}
  EMA20 M15      : {m15_ema_s}
  slope M15      : {m15_slope_s}
  -> M15         : {m15_trend_s} (si M15 contre direction envisagee -> WAIT obligatoire)

5 DERNIERES BOUGIES M5 :
{candles_str}

Decide maintenant. JSON conforme uniquement."""


# -----------------------------------------------------------------------
# Agent IA
# -----------------------------------------------------------------------

BUFFER_SIZE = 300              # 300 bougies M5 = 25h de donnees, large pour SMA et MACD
MIN_BUFFER  = 60               # minimum requis (MACD = 33 lookback)


class AITradingAgent:
    """
    Agent IA Claude pour scalping XAU/USD. Reutilise OrderExecutor + RiskManager.
    Pousse les signaux validables dans signal_queue (consommee par signal_task de main.py).
    """

    def __init__(
        self,
        agent_cfg:    AgentConfig,
        risk_manager: RiskManager,
        instrument:   str = "XAUUSD",
    ) -> None:
        if not ANTHROPIC_OK:
            raise RuntimeError("anthropic SDK non installe. pip install anthropic")
        if not agent_cfg.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY manquant dans .env")
        if not agent_cfg.enabled:
            raise RuntimeError("AI_AGENT_ENABLED != true (active explicitement dans .env)")

        self.cfg    = agent_cfg
        self.rm     = risk_manager
        self.inst   = instrument
        self.client = anthropic.Anthropic(api_key=agent_cfg.api_key)

        self._buffer:        Deque[OHLCVCandle] = deque(maxlen=BUFFER_SIZE)
        self._trade_history: Deque[datetime]    = deque(maxlen=20)
        self._signal_seq                        = 0
        self._calls_made                        = 0
        self._signals_emitted                   = 0
        self._signals_skipped                   = 0
        self._skipped_market_closed             = 0
        self._skipped_offhours                  = 0
        self._last_decision                     = None
        # P3 (28/05) : anti-spam M15 - pause de 15 min apres 3 rejets consecutifs
        self._m15_consecutive_rejects: int   = 0
        self._m15_pause_until:         float = 0.0

        # Horaires de cotation du marche (XAUUSD : Dim 22h UTC -> Ven 21h UTC,
        # pause quotidienne 21-22h UTC). Si None -> pas de garde-fou horaire.
        rules = rules_for(instrument)
        self._market_hours = rules.market_hours if rules else None
        if self._market_hours:
            logger.info(
                f"[AI] Garde-fou horaire actif : {self._market_hours.name}"
            )

    # ---- API publique ----

    def add_candle(self, candle: OHLCVCandle) -> None:
        """Appele par main.py pour chaque bougie M5 XAUUSD recue."""
        self._buffer.append(candle)

    async def run(self, signal_queue: asyncio.Queue) -> None:
        """Boucle principale. Lance dans un task async par main.py."""
        logger.info(
            f"[AI] Agent demarre | modele={self.cfg.model} | "
            f"intervalle={self.cfg.interval_sec}s | max_trades/h={self.cfg.max_trades_h} "
            f"| effort={self.cfg.effort}"
        )
        await asyncio.sleep(5)  # laisser le buffer se remplir
        try:
            while True:
                await asyncio.sleep(self.cfg.interval_sec)
                try:
                    await self._tick(signal_queue)
                except Exception as e:
                    logger.exception(f"[AI] Erreur tick: {e}")
        except asyncio.CancelledError:
            logger.info(
                f"[AI] Agent arrete | appels={self._calls_made} "
                f"signaux={self._signals_emitted} skip={self._signals_skipped}"
            )
            raise

    def get_status(self) -> dict:
        return {
            "calls_made":             self._calls_made,
            "signals_emitted":        self._signals_emitted,
            "signals_skipped":        self._signals_skipped,
            "skipped_market_closed":  self._skipped_market_closed,
            "skipped_offhours":       self._skipped_offhours,
            "buffer_size":            len(self._buffer),
            "last_decision":          self._last_decision,
            "trades_last_hour":       self._trades_in_last_hour(),
            "market_open":            (self._market_hours.is_open_now()
                                       if self._market_hours else True),
            "model_active":           self._current_model(),
            "consecutive_sl_today":   self._consecutive_sl_today(),
            "cooldown_threshold":     self.cfg.sl_cooldown_threshold,
            "cooldown_active":        (self._consecutive_sl_today() >=
                                       self.cfg.sl_cooldown_threshold > 0),
        }

    # ---- Interne ----

    async def _tick(self, signal_queue: asyncio.Queue) -> None:
        # Garde-fou 0/3 : toggle runtime via /engines ai off
        try:
            from engines_control import is_ai_enabled
            if not is_ai_enabled():
                # Skip silencieux (log toutes les 60 ticks = 60 min)
                if not hasattr(self, "_skipped_runtime_off"):
                    self._skipped_runtime_off = 0
                self._skipped_runtime_off += 1
                if self._skipped_runtime_off % 60 == 1:
                    logger.info(f"[AI] Agent IA OFF via engines_control (skip {self._skipped_runtime_off})")
                return
        except Exception:
            pass  # En cas d'erreur lecture flag, on continue (fail-open)

        # Garde-fou horaire 1/2 : marche ferme (XAUUSD : Ven 21h UTC -> Dim 22h UTC,
        # pause quotidienne 21-22h UTC)
        if self._market_hours and not self._market_hours.is_open_now():
            self._skipped_market_closed += 1
            if self._skipped_market_closed % 60 == 1:   # log toutes les 60 min
                from datetime import datetime, timezone
                nxt = self._market_hours.next_close_after(
                    datetime.now(tz=timezone.utc)
                )
                logger.info(
                    f"[AI] Marche {self.inst} ferme - skip "
                    f"(skipped={self._skipped_market_closed}, prochain close={nxt})"
                )
            return

        # Garde-fou horaire 2/2 : plage de SESSION active (env AI_AGENT_SESSION_*).
        # Hors plage : pas d'appel Claude (economie tokens). -1/-1 = 24/24.
        s, e = self.cfg.session_start_h, self.cfg.session_end_h
        if s >= 0 and e >= 0 and s != e:
            from datetime import datetime, timezone
            h = datetime.now(tz=timezone.utc).hour
            in_session = (s <= h < e) if s < e else (h >= s or h < e)
            if not in_session:
                self._skipped_offhours += 1
                if self._skipped_offhours % 60 == 1:
                    logger.info(
                        f"[AI] Hors session {s}h-{e}h UTC (il est {h:02d}h) - "
                        f"skip (skipped={self._skipped_offhours})"
                    )
                return

        # Garde-fous prealables
        if len(self._buffer) < MIN_BUFFER:
            logger.debug(f"[AI] Buffer insuffisant {len(self._buffer)}/{MIN_BUFFER}")
            return
        if self.rm.kill_switch_active:
            logger.warning("[AI] Kill switch actif - skip")
            return

        # Cooldown : N SL consecutifs aujourd'hui UTC -> skip jusqu'au prochain
        # jour UTC. Decision du 27/05 (sur la session -27.67 EUR / 22 trades,
        # cooldown apres 3e SL consecutif aurait sauve +40 EUR).
        threshold = self.cfg.sl_cooldown_threshold
        if threshold > 0:
            streak = self._consecutive_sl_today()
            if streak >= threshold:
                if not getattr(self, "_cooldown_alert_sent", False):
                    logger.warning(
                        f"[AI] COOLDOWN active : {streak} SL consecutifs sur {self.inst} "
                        f"aujourdhui UTC - skip jusqu'au prochain jour UTC"
                    )
                    try:
                        from telegram_alerts import alerts as _tg
                        _tg().send(
                            f"⚠️ Cooldown IA active\n"
                            f"{streak} SL consecutifs sur {self.inst}\n"
                            f"Skip jusqu'au prochain jour UTC (00h00)",
                            parse_mode=None,
                        )
                    except Exception:
                        pass
                    self._cooldown_alert_sent = True
                self._signals_skipped += 1
                return
            else:
                # Streak retombe < threshold (typiquement nouveau jour UTC).
                if getattr(self, "_cooldown_alert_sent", False):
                    logger.info(f"[AI] Cooldown leve (streak={streak} < {threshold})")
                    try:
                        from telegram_alerts import alerts as _tg
                        _tg().send(f"✅ Cooldown IA leve - reprise sur {self.inst}",
                                   parse_mode=None)
                    except Exception:
                        pass
                    self._cooldown_alert_sent = False

        if self._trades_in_last_hour() >= self.cfg.max_trades_h:
            logger.info(f"[AI] Max trades/h atteint ({self.cfg.max_trades_h}) - skip")
            return

        # P3 (28/05) : pause anti-spam M15. Si 3 rejets consecutifs M15 dans la
        # meme session, on suspend les appels Claude pendant 15 min. Evite de
        # bombarder l'API avec des SELL/LONG systematiquement rejetes par M15
        # quand la tendance H1 contredit fortement Claude.
        import time as _time
        now_ts = _time.time()
        if now_ts < self._m15_pause_until:
            remain = int(self._m15_pause_until - now_ts)
            logger.debug(f"[AI] Pause M15 active encore {remain}s - skip Claude")
            self._signals_skipped += 1
            return

        # Snapshot marche
        snap = self._build_snapshot()
        if snap is None:
            logger.debug("[AI] Snapshot impossible (indicateurs NaN)")
            return

        # Appel Claude
        snap_prompt = snap.to_prompt()
        try:
            ai_signal, claude_meta = await asyncio.to_thread(self._ask_claude, snap)
        except Exception as e:
            logger.error(f"[AI] Echec appel Claude: {e}")
            return

        self._calls_made += 1
        self._last_decision = ai_signal.decision

        logger.info(
            f"[AI] Decision={ai_signal.decision} conf={ai_signal.confidence} "
            f"E={ai_signal.entry_price:.2f} SL={ai_signal.stop_loss:.2f} "
            f"TP={ai_signal.take_profit:.2f} RR={ai_signal.risk_reward:.2f} "
            f"| {ai_signal.technical_reason[:80]}"
        )

        # Pre-calcule m15_reason pour le log (independant du verdict final)
        direction_str = "LONG" if ai_signal.decision == "BUY" else (
            "SHORT" if ai_signal.decision == "SELL" else "WAIT")
        if direction_str != "WAIT":
            _, m15_log_reason = self._m15_trend_check(direction_str)
        else:
            m15_log_reason = "N/A (WAIT)"

        # Audit JSONL (decision 28/05 - sécurité max)
        if self.cfg.log_decisions:
            self._log_decision_jsonl(
                snap_prompt   = snap_prompt,
                ai_signal     = ai_signal,
                model         = claude_meta.get("model", "?"),
                usage         = claude_meta.get("usage", {}),
                thinking_text = claude_meta.get("thinking", ""),
                m15_reason    = m15_log_reason,
            )

        # Validation post-Claude (defense en profondeur)
        if ai_signal.decision == "WAIT":
            self._signals_skipped += 1
            return
        if ai_signal.confidence < 60:
            logger.info(f"[AI] Confidence {ai_signal.confidence} < 60 - skip")
            self._signals_skipped += 1
            return
        if ai_signal.risk_reward < self.cfg.min_rr:
            logger.info(f"[AI] R:R {ai_signal.risk_reward:.2f} < min {self.cfg.min_rr} - skip")
            self._signals_skipped += 1
            return
        if ai_signal.stop_loss <= 0 or ai_signal.take_profit <= 0:
            logger.warning("[AI] SL ou TP a 0 - skip")
            self._signals_skipped += 1
            return

        # Validation geometrique (le RM va re-valider, mais on filtre tot)
        is_buy = ai_signal.decision == "BUY"
        if is_buy and (ai_signal.stop_loss >= ai_signal.entry_price
                       or ai_signal.take_profit <= ai_signal.entry_price):
            logger.warning("[AI] Geometrie BUY incoherente - skip")
            self._signals_skipped += 1
            return
        if (not is_buy) and (ai_signal.stop_loss <= ai_signal.entry_price
                             or ai_signal.take_profit >= ai_signal.entry_price):
            logger.warning("[AI] Geometrie SELL incoherente - skip")
            self._signals_skipped += 1
            return

        # Filtre M15 strict (decision 28/05) : skip SHORT/LONG contre tendance M15.
        # Reuse le check fait pour le log si possible.
        if self.cfg.m15_filter_enabled:
            direction_str = "LONG" if is_buy else "SHORT"
            allowed_m15, m15_reason_filter = self._m15_trend_check(direction_str)
            if not allowed_m15:
                logger.info(f"[AI] Filtre M15 rejette : {m15_reason_filter}")
                self._signals_skipped += 1
                # P3 : anti-spam M15. Pause 15 min apres 3 rejets consecutifs.
                self._m15_consecutive_rejects += 1
                if self._m15_consecutive_rejects >= 3:
                    self._m15_pause_until = _time.time() + 15 * 60
                    logger.warning(
                        f"[AI] Anti-spam M15 : 3 rejets consecutifs - "
                        f"pause Claude 15 min (jusqu'a {datetime.fromtimestamp(self._m15_pause_until, tz=timezone.utc).strftime('%H:%M UTC')})"
                    )
                    try:
                        from telegram_alerts import alerts as _tg
                        _tg().send(
                            f"⏸ Anti-spam M15 actif\n"
                            f"3 SELL/LONG rejetes contre tendance M15\n"
                            f"Pause Claude 15 min sur {self.inst}",
                            parse_mode=None,
                        )
                    except Exception:
                        pass
                    self._m15_consecutive_rejects = 0
                return
            # Decision validee par M15 -> reset compteur
            self._m15_consecutive_rejects = 0

        # ====================================================================
        # INVERT_SL_TP : permute les distances SL/TP avant validation RM.
        # Strategie scalping serree : SL eloigne + TP proche -> moins de stops
        # touches, plus de TP atteints, mais R:R < 1 -> exige win rate eleve.
        # Active via env INVERT_SL_TP=true. Off par defaut (=stratégie classique).
        # ====================================================================
        if os.getenv("INVERT_SL_TP", "false").lower() == "true":
            sl_dist = abs(ai_signal.entry_price - ai_signal.stop_loss)
            tp_dist = abs(ai_signal.take_profit - ai_signal.entry_price)
            if is_buy:
                new_sl = round(ai_signal.entry_price - tp_dist, 2)
                new_tp = round(ai_signal.entry_price + sl_dist, 2)
            else:
                new_sl = round(ai_signal.entry_price + tp_dist, 2)
                new_tp = round(ai_signal.entry_price - sl_dist, 2)
            old_rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
            new_rr = (sl_dist / tp_dist) if tp_dist > 0 else 0.0
            logger.warning(
                f"[AI] INVERT_SL_TP : SL {ai_signal.stop_loss:.2f}->{new_sl:.2f}  "
                f"TP {ai_signal.take_profit:.2f}->{new_tp:.2f}  "
                f"RR {old_rr:.2f}->{new_rr:.2f}"
            )
            ai_signal.stop_loss = new_sl
            ai_signal.take_profit = new_tp

        # Construire TradeSetup (sizing par RM, fait dans le flux signal_task de main.py)
        risk_pts   = abs(ai_signal.entry_price - ai_signal.stop_loss)
        reward_pts = abs(ai_signal.take_profit - ai_signal.entry_price)
        self._signal_seq += 1

        # Sizing : on demande au RM ce qu'il accepte
        try:
            decision = self.rm.validate_signal(
                direction  = "LONG" if is_buy else "SHORT",
                entry      = ai_signal.entry_price,
                sl         = ai_signal.stop_loss,
                tp         = ai_signal.take_profit,
                atr        = snap.atr14,
                spread     = snap.spread,
                setup_name = "AI_AGENT_CLAUDE",
                instrument = self.inst,
                # L'agent IA a son propre seuil RR (AI_AGENT_MIN_RR, defaut 1.5).
                # Le RM hérite de Bv3 (3.0) qui est inadapté au scalping IA.
                min_rr_override = self.cfg.min_rr,
            )
        except Exception as e:
            logger.error(f"[AI] RM validate_signal exception: {e}")
            self._signals_skipped += 1
            return

        if not decision.approved:
            logger.info(f"[AI] RM rejette: {decision.reason}")
            self._signals_skipped += 1
            return

        setup = TradeSetup(
            direction   = SignalDirection.LONG if is_buy else SignalDirection.SHORT,
            entry       = round(ai_signal.entry_price, 2),
            stop_loss   = round(ai_signal.stop_loss, 2),
            take_profit = round(ai_signal.take_profit, 2),
            risk_pts    = round(risk_pts, 2),
            reward_pts  = round(reward_pts, 2),
            rr_ratio    = round(ai_signal.risk_reward, 2),
            size        = decision.size,
            # IMPORTANT : prefix utilise par order_executor pour parser l'instrument
            setup_name  = f"SETUP_B_Bv3_{self.inst}",
            reason      = f"AI(conf={ai_signal.confidence}): {ai_signal.technical_reason[:120]}",
        )

        await signal_queue.put(setup)
        self._signals_emitted += 1
        self._trade_history.append(datetime.now(tz=timezone.utc))
        logger.info(
            f"[AI] Signal #{self._signal_seq} emis -> queue | "
            f"{setup.direction.value} {setup.size}L E={setup.entry} "
            f"SL={setup.stop_loss} TP={setup.take_profit}"
        )

    # ---- Snapshot + indicateurs ----

    def _build_snapshot(self) -> Optional[MarketSnapshot]:
        n = len(self._buffer)
        if n < MIN_BUFFER:
            return None

        buf   = list(self._buffer)
        close = np.array([c.close for c in buf], dtype=np.float64)
        high  = np.array([c.high  for c in buf], dtype=np.float64)
        low   = np.array([c.low   for c in buf], dtype=np.float64)

        atr   = talib.ATR(high, low, close, 14)
        adx   = talib.ADX(high, low, close, 14)
        rsi   = talib.RSI(close, 14)
        ml, ms, mh = talib.MACD(close, 12, 26, 9)
        bbu, bbm, bbl = talib.BBANDS(close, 20, 2.0, 2.0)
        ema20 = talib.EMA(close, 20)
        ema50 = talib.EMA(close, 50)
        ma200 = talib.SMA(close, min(2400, n - 1))

        def last(arr):
            v = arr[-1]
            return None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)

        vals = {k: last(v) for k, v in {
            "atr": atr, "adx": adx, "rsi": rsi,
            "ml": ml, "ms": ms, "mh": mh,
            "bbu": bbu, "bbm": bbm, "bbl": bbl,
            "ema20": ema20, "ema50": ema50, "ma200": ma200,
        }.items()}

        if any(v is None for v in vals.values()):
            return None

        latest = buf[-1]
        bb_pct = (close[-1] - vals["bbl"]) / (vals["bbu"] - vals["bbl"]) if vals["bbu"] != vals["bbl"] else 0.5

        ts   = latest.timestamp
        hour = ts.hour
        # Plage configuree via env AI_AGENT_SESSION_START/END. -1/-1 = 24/24.
        s_h, e_h = self.cfg.session_start_h, self.cfg.session_end_h
        if s_h < 0 or e_h < 0 or s_h == e_h:
            in_session = True
            session_label = f"{hour:02d}h UTC (XAUUSD 24/24)"
        else:
            in_session = (s_h <= hour < e_h) if s_h < e_h else (hour >= s_h or hour < e_h)
            session_label = f"{hour:02d}h UTC (XAUUSD session {s_h}-{e_h} UTC)"

        # Tendance M15 (P2 28/05) : aide Claude a respecter hierarchie TF
        m15_close, m15_ema, m15_slope, m15_trend = self._m15_state()

        return MarketSnapshot(
            timestamp     = ts,
            bid           = latest.bid_close or latest.close,
            ask           = latest.ask_close or latest.close,
            spread        = (latest.ask_close - latest.bid_close) if latest.ask_close else 0.0,
            mid           = latest.close,
            atr14         = vals["atr"],
            adx14         = vals["adx"],
            rsi14         = vals["rsi"],
            macd_line     = vals["ml"],
            macd_signal   = vals["ms"],
            macd_hist     = vals["mh"],
            bb_upper      = vals["bbu"],
            bb_mid        = vals["bbm"],
            bb_lower      = vals["bbl"],
            bb_percent_b  = bb_pct,
            ema20         = vals["ema20"],
            ema50         = vals["ema50"],
            ma200_proxy   = vals["ma200"],
            recent_candles= buf[-5:],
            in_session    = in_session,
            session_label = session_label,
            m15_close     = m15_close,
            m15_ema20     = m15_ema,
            m15_slope     = m15_slope,
            m15_trend     = m15_trend,
        )

    # ---- Appel Claude (synchrone, lance en thread depuis _tick) ----

    def _current_model(self) -> str:
        """
        Lit le modele courant. Priorite :
          1. Fichier /app/data/current_model.txt (hot-swap par bot Telegram /model)
          2. Variable d'env AI_AGENT_MODEL (self.cfg.model)
        """
        from pathlib import Path as _P
        flag = _P("/app/data/current_model.txt")
        if not flag.exists():
            # Aussi essayer le repertoire local (tests Windows)
            flag = _P("data/current_model.txt")
        if flag.exists():
            try:
                m = flag.read_text(encoding="utf-8").strip()
                if m.startswith("claude-"):
                    return m
            except Exception:
                pass
        return self.cfg.model

    def _ask_claude(self, snap: MarketSnapshot) -> Tuple[AISignal, dict]:
        """
        Utilise messages.parse pour validation Pydantic automatique.
        System prompt en cache (cache_control ephemeral 5 min).
        Adaptive thinking active.
        Le modele peut etre hot-swap via /app/data/current_model.txt (bot Telegram).
        """
        model = self._current_model()
        # Log + notif Telegram quand on change de modele (suivi des switches manuels)
        if not hasattr(self, "_last_logged_model"):
            # Premier tick : annonce du modele actif au demarrage
            logger.info(f"[AI] Modele actif au demarrage : {model}")
            try:
                from telegram_alerts import alerts as _tg
                _tg().send(
                    f"🤖 Agent IA actif\nModele : {model}",
                    parse_mode=None,
                )
            except Exception:
                pass
            self._last_logged_model = model
        elif self._last_logged_model != model:
            # Vrai changement de modele detecte
            old = self._last_logged_model
            logger.info(f"[AI] Modele change : {old} -> {model}")
            try:
                from telegram_alerts import alerts as _tg
                _tg().send(
                    f"🔄 Modele Claude change applique\n{old} -> {model}",
                    parse_mode=None,
                )
            except Exception:
                pass
            self._last_logged_model = model

        # Adaptive thinking + effort : SUPPORTES sur Opus 4.6/4.7 et Sonnet 4.6
        # uniquement. Haiku 4.5 retourne 400 si on les passe.
        supports_thinking = (model.startswith("claude-opus-4")
                             or model.startswith("claude-sonnet-4-6"))
        call_kwargs = dict(
            model       = model,
            max_tokens  = 4000,
            system      = [{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # cache 5min, reutilise
            }],
            messages    = [{
                "role": "user",
                "content": snap.to_prompt(),
            }],
            output_format = AISignal,
        )
        if supports_thinking:
            call_kwargs["thinking"]      = {"type": "adaptive"}
            call_kwargs["output_config"] = {"effort": self.cfg.effort}

        # temperature=0 deterministe (decision 28/05). NB : Anthropic interdit
        # temperature != 1 quand thinking est active (extended/adaptive). Donc on
        # n'applique temperature=0 QUE sur les modeles sans thinking (Haiku).
        # Fallback try/except plus bas en filet de securite.
        if (self.cfg.temperature_zero
                and not supports_thinking
                and not getattr(self, "_temp0_unsupported", False)):
            call_kwargs["temperature"] = 0.0

        try:
            msg = self.client.messages.parse(**call_kwargs)
        except Exception as e:
            es = str(e)
            if "temperature" in es.lower() and "temperature" in call_kwargs:
                logger.warning(
                    f"[AI] temperature=0 rejete par l'API ({model}) - fallback default"
                )
                call_kwargs.pop("temperature", None)
                self._temp0_unsupported = True
                msg = self.client.messages.parse(**call_kwargs)
            else:
                raise

        # Extraction usage + thinking pour audit (logger meta)
        usage_dict = {"input": 0, "cache_read": 0, "cache_create": 0, "output": 0}
        try:
            u = msg.usage
            usage_dict = {
                "input":        getattr(u, "input_tokens", 0),
                "cache_read":   getattr(u, "cache_read_input_tokens", 0),
                "cache_create": getattr(u, "cache_creation_input_tokens", 0),
                "output":       getattr(u, "output_tokens", 0),
            }
            logger.debug(
                f"[AI] tokens input={usage_dict['input']} cache_read={usage_dict['cache_read']} "
                f"cache_create={usage_dict['cache_create']} output={usage_dict['output']}"
            )
        except Exception:
            pass

        thinking_text = ""
        try:
            if hasattr(msg, "content") and msg.content:
                for block in msg.content:
                    btype = getattr(block, "type", None)
                    if btype == "thinking":
                        thinking_text += (getattr(block, "thinking", "") or "") + "\n"
                    elif btype == "redacted_thinking":
                        thinking_text += "[REDACTED]\n"
        except Exception:
            pass

        meta = {"usage": usage_dict, "thinking": thinking_text, "model": model}
        return msg.parsed_output, meta

    def _trades_in_last_hour(self) -> int:
        now = datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(hours=1)
        return sum(1 for t in self._trade_history if t > cutoff)

    def _consecutive_sl_today(self) -> int:
        """
        Compte les SL consecutifs (depuis le dernier trade clos) parmi les
        trades cloturés aujourd'hui UTC pour self.inst.

        Source : bonaza.db `trades` JOIN `signals` (filtre setup_name LIKE %inst%).
        Un trade est "SL" si pnl_eur < 0 (sortie en perte). Les wins remettent
        la streak a 0.
        """
        try:
            import sqlite3
            from pathlib import Path as _P
            db_path = _P("/app/data/bonaza.db")
            if not db_path.exists():
                db_path = _P("data/bonaza.db")
            if not db_path.exists():
                return 0
            today_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            con = sqlite3.connect(str(db_path))
            try:
                rows = con.execute("""
                    SELECT t.pnl_eur
                    FROM trades t
                    LEFT JOIN signals s ON s.id = t.signal_id
                    WHERE substr(t.ts_close, 1, 10) = ?
                      AND t.status = 'CLOSED'
                      AND (s.setup_name LIKE ? OR s.setup_name IS NULL)
                    ORDER BY t.ts_close DESC
                """, (today_utc, f"%{self.inst}%")).fetchall()
            finally:
                con.close()
            streak = 0
            for (pnl,) in rows:
                if (pnl or 0) < 0:
                    streak += 1
                else:
                    break
            return streak
        except Exception as e:
            logger.warning(f"[AI] _consecutive_sl_today: {e}")
            return 0

    def _aggregate_m15(self) -> List[dict]:
        """Agrege le buffer M5 en bougies M15 (3 M5 -> 1 M15).
        Groupe par boundary minute (00, 15, 30, 45). Saute la barre courante
        incomplete sauf si elle a au moins 1 M5."""
        buf = list(self._buffer)
        if len(buf) < 3:
            return []
        groups: dict = {}
        for cdl in buf:
            ts = cdl.timestamp
            bound_min = (ts.minute // 15) * 15
            bts = ts.replace(minute=bound_min, second=0, microsecond=0)
            groups.setdefault(bts, []).append(cdl)
        out: List[dict] = []
        sorted_bts = sorted(groups.keys())
        for i, bts in enumerate(sorted_bts):
            group = groups[bts]
            is_last = (i == len(sorted_bts) - 1)
            if len(group) < 3 and not is_last:
                continue
            out.append({
                "ts": bts,
                "o": float(group[0].open),
                "h": float(max(g.high for g in group)),
                "l": float(min(g.low  for g in group)),
                "c": float(group[-1].close),
            })
        return out

    def _m15_state(self) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
        """Retourne (close_M15, EMA20_M15, slope_str, trend_str) pour le snapshot.
        Renvoie (None, None, None, None) si buffer insuffisant."""
        m15 = self._aggregate_m15()
        if len(m15) < 22:
            return None, None, None, None
        closes = np.array([c["c"] for c in m15], dtype=np.float64)
        ema = talib.EMA(closes, 20)
        if math.isnan(ema[-1]) or math.isnan(ema[-2]):
            return float(closes[-1]), None, None, None
        e_now, e_prev = float(ema[-1]), float(ema[-2])
        close_now = float(closes[-1])
        slope_up  = e_now > e_prev
        pos_above = close_now > e_now
        slope_str = "UP" if slope_up else "DOWN"
        if pos_above and slope_up:
            trend_str = "UP_TREND"
        elif (not pos_above) and (not slope_up):
            trend_str = "DOWN_TREND"
        else:
            trend_str = "RANGE"
        return close_now, e_now, slope_str, trend_str

    def _m15_trend_check(self, direction: str) -> Tuple[bool, str]:
        """Retourne (allow, reason).
        Skip SHORT si M15 = ABOVE EMA20 + slope UP.
        Skip LONG  si M15 = BELOW EMA20 + slope DOWN.
        Tolerant : si pas assez de M15 ou EMA NaN, autorise (defense fail-open)."""
        m15 = self._aggregate_m15()
        if len(m15) < 22:
            return True, f"M15_INSUFFICIENT (n={len(m15)})"
        closes = np.array([c["c"] for c in m15], dtype=np.float64)
        ema = talib.EMA(closes, 20)
        if math.isnan(ema[-1]) or math.isnan(ema[-2]):
            return True, "M15_EMA_NAN"
        e_now, e_prev = float(ema[-1]), float(ema[-2])
        close_now = float(closes[-1])
        pos_above = close_now > e_now
        slope_up  = e_now > e_prev
        slope_label = "UP" if slope_up else "DOWN"
        pos_label   = "ABOVE" if pos_above else "BELOW"
        if direction == "SHORT" and pos_above and slope_up:
            return False, (f"M15_AGAINST_SHORT close={close_now:.2f}>EMA20={e_now:.2f} "
                           f"slope={slope_label}")
        if direction == "LONG" and (not pos_above) and (not slope_up):
            return False, (f"M15_AGAINST_LONG close={close_now:.2f}<EMA20={e_now:.2f} "
                           f"slope={slope_label}")
        return True, f"M15_OK close={close_now:.2f} vs EMA20={e_now:.2f} {pos_label}+{slope_label}"

    def _log_decision_jsonl(
        self,
        snap_prompt: str,
        ai_signal: AISignal,
        model: str,
        usage: dict,
        thinking_text: str,
        m15_reason: str,
    ) -> None:
        """Append une ligne JSON dans /app/data/ai_decisions.jsonl pour audit."""
        try:
            from pathlib import Path as _P
            base = _P("/app/data")
            if not base.exists():
                base = _P("data")
            base.mkdir(parents=True, exist_ok=True)
            path = base / "ai_decisions.jsonl"
            entry = {
                "ts":     datetime.now(tz=timezone.utc).isoformat(),
                "model":  model,
                "decision":   ai_signal.decision,
                "confidence": ai_signal.confidence,
                "entry":      ai_signal.entry_price,
                "sl":         ai_signal.stop_loss,
                "tp":         ai_signal.take_profit,
                "rr":         ai_signal.risk_reward,
                "validity_sec": ai_signal.validity_sec,
                "technical_reason":       ai_signal.technical_reason,
                "invalidation_condition": ai_signal.invalidation_condition,
                "risk_warning":           ai_signal.risk_warning,
                "tokens":     usage,
                "thinking":   thinking_text[:8000] if thinking_text else None,
                "m15_check":  m15_reason,
                "snapshot":   snap_prompt,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[AI] log_decisions echec: {e}")


# -----------------------------------------------------------------------
# Factory
# -----------------------------------------------------------------------

def build_ai_agent(
    config:       "BonazaConfig",  # type: ignore
    risk_manager: RiskManager,
    instrument:   str = "XAUUSD",
) -> Optional[AITradingAgent]:
    """Construit l'agent si AI_AGENT_ENABLED=true + clef presente. Sinon None."""
    if not config.agent.is_ready():
        return None
    return AITradingAgent(
        agent_cfg    = config.agent,
        risk_manager = risk_manager,
        instrument   = instrument,
    )
