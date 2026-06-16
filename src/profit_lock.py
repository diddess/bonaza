"""
profit_lock.py - Gestion ADAPTATIVE de la prise de benefice (scalp + copieur).
==============================================================================
La DECISION est une fonction PURE `adaptive_action(...)` (testable, sans I/O),
reutilisee par le scalp demo (ProfitLockManager) ET le copieur live
(TelegramCopier._manage_tick) -> meme logique, pas de divergence.

3 modes (classement = tendance M5) :
  (1) WITH-TREND  -> LAISSE COURIR : trailing STRUCTUREL (SL sous dernier creux
        confirme / au-dessus dernier sommet, buffer ATR) + plancher BREAK-EVEN
        une fois profit >= arm*ATR + SORTIE sur RETOURNEMENT survenu apres l'entree
        (CHoCH contre / liquidity grab contre).
  (2) COUNTER-TREND -> CLIQUET serre (arm*ATR, plancher max(exit*ATR, ratchet*pic),
        close au repli).
  (3) RANGE -> CLIQUET aussi.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, Optional, Tuple

from loguru import logger

LOCK_ARM_ATR   = 1.0
LOCK_EXIT_ATR  = 0.8
RATCHET_PCT    = 0.5
STRUCT_BUF_ATR = 0.3
IG_GAP_ATR     = 0.5
IG_MIN_IMPROVE_ATR = 0.2
EVENT_RECENCY  = 8
TICK_SEC = 2.0


def classify(long: bool, trend: str) -> str:
    if (long and trend == "bull") or ((not long) and trend == "bear"):
        return "with"
    if (long and trend == "bear") or ((not long) and trend == "bull"):
        return "counter"
    return "range"


def adaptive_action(long: bool, entry: float, price: float, peak: float, atr: float,
                    trend: str, swings_with, events, opened_at, cur_sl: float,
                    arm_atr: float = LOCK_ARM_ATR, exit_atr: float = LOCK_EXIT_ATR,
                    struct_buf_atr: float = STRUCT_BUF_ATR) -> Tuple[str, object, str]:
    """Decision PURE. Retourne (kind, payload, mode) :
      kind="close"  payload=motif ("REV_CHoCH"/"REV_grab"/"LOCK")
      kind="move_sl" payload=niveau SL (float)
      kind="hold"   payload=None
    `swings_with` = swing_lows si long, swing_highs si short (objets avec .price).
    """
    mode = classify(long, trend)
    profit = (price - entry) if long else (entry - price)

    if mode == "with":
        # a) sortie sur retournement CONTRE la position, survenu APRES l'entree
        want = "bearish" if long else "bullish"
        for ev in list(events)[-EVENT_RECENCY:]:
            ts = ev.get("ts")
            if not ts:
                continue
            try:
                edt = datetime.fromisoformat(ts)
            except Exception:
                continue
            if opened_at is not None and edt <= opened_at:
                continue
            if ev.get("type") == "reversal" and ev.get("direction") == want:
                return ("close", "REV_CHoCH", mode)
            if ev.get("type") == "liquidity_grab" and ev.get("bias") == want:
                return ("close", "REV_grab", mode)
        # b) trailing structurel + plancher break-even une fois arme
        ref = swings_with[-1].price if swings_with else None
        trail = None
        if ref is not None:
            trail = (ref - struct_buf_atr * atr) if long else (ref + struct_buf_atr * atr)
        if peak >= arm_atr * atr:
            if trail is None:
                trail = entry
            else:
                trail = max(trail, entry) if long else min(trail, entry)
        if trail is None:
            return ("hold", None, mode)
        improves = (trail > cur_sl + IG_MIN_IMPROVE_ATR * atr) if long \
            else (cur_sl == 0.0 or trail < cur_sl - IG_MIN_IMPROVE_ATR * atr)
        if improves and abs(price - trail) >= IG_GAP_ATR * atr:
            return ("move_sl", round(trail, 2), mode)
        return ("hold", None, mode)

    # counter / range : cliquet serre
    if peak < arm_atr * atr:
        return ("hold", None, mode)
    floor = max(exit_atr * atr, RATCHET_PCT * peak)
    if profit <= floor:
        return ("close", "LOCK", mode)
    new_sl = round(entry + floor, 2) if long else round(entry - floor, 2)
    improves = (new_sl > cur_sl + IG_MIN_IMPROVE_ATR * atr) if long \
        else (cur_sl == 0.0 or new_sl < cur_sl - IG_MIN_IMPROVE_ATR * atr)
    if improves and (profit - floor) >= IG_GAP_ATR * atr:
        return ("move_sl", new_sl, mode)
    return ("hold", None, mode)


class ProfitLockManager:
    """Applique adaptive_action a toutes les positions de l'executeur (scalp demo)."""

    def __init__(self, executor, feed, instruments: dict, market_state, adaptive: bool = True) -> None:
        self.executor = executor
        self.feed = feed
        self.market_state = market_state
        self.adaptive = adaptive
        self._epic_to_name: Dict[str, str] = {inst.epic: name for name, inst in instruments.items()}
        self._peak: Dict[str, float] = {}

    async def tick(self) -> None:
        ps = self.executor.open_positions()
        live = {p.deal_id for p in ps}
        for d in [d for d in self._peak if d not in live]:
            self._peak.pop(d, None)
        for p in ps:
            epic = getattr(p, "epic", "") or ""
            price = self.feed.get_price(epic) if epic else None
            if not price or price <= 0:
                continue
            name = self._epic_to_name.get(epic) or getattr(p, "instrument", "")
            st = self.market_state.get(name, "M5")
            atr = st.last_indicators.get("atr") if st.last_indicators else None
            if not atr or atr <= 0:
                continue
            long = p.direction in ("LONG", "BUY")
            entry = p.entry_level
            profit = (price - entry) if long else (entry - price)
            peak = max(self._peak.get(p.deal_id, 0.0), profit)
            self._peak[p.deal_id] = peak
            cur_sl = float(getattr(p, "sl_level", 0.0) or 0.0)
            swings = st.swing_lows if long else st.swing_highs
            trend = st.trend if self.adaptive else ("bear" if long else "bull")  # force counter -> cliquet
            kind, payload, mode = adaptive_action(
                long, entry, price, peak, atr, trend, swings, st.events,
                getattr(p, "opened_at", None), cur_sl)
            try:
                if kind == "close":
                    reason = ("SCALP_REVERSAL_%s" % payload[4:]) if str(payload).startswith("REV_") \
                        else ("SCALP_LOCK_%+.1f" % profit)
                    if await self.executor.close_position(p.deal_id, reason):
                        self._peak.pop(p.deal_id, None)
                    logger.info("[SCALP-LOCK] %s %s : %s (%s, profit %+.1f, pic %+.1f)"
                                % (name, p.deal_id, reason, mode, profit, peak))
                elif kind == "move_sl":
                    if await self.executor.move_stop_to(p.deal_id, payload):
                        logger.info("[SCALP-LOCK] %s %s : SL -> %.2f (%s)" % (name, p.deal_id, payload, mode))
            except Exception as e:
                logger.error("[SCALP-LOCK] action %s : %s" % (p.deal_id, e))

    async def run(self) -> None:
        logger.info("[SCALP-LOCK] gestion ADAPTATIVE active (tick %.0fs | with-trend=trailing structurel"
                    " + sortie retournement | counter/range=cliquet %.1f/%.1f xATR/%.0f%%)"
                    % (TICK_SEC, LOCK_ARM_ATR, LOCK_EXIT_ATR, RATCHET_PCT * 100))
        while True:
            try:
                await asyncio.sleep(TICK_SEC)
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[SCALP-LOCK] tick : %s" % e)
