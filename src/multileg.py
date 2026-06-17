"""
multileg.py - Gestion multi-jambes du scalp demo (validee backtest 16/06).
==========================================================================
Sur chaque signal, on ouvre 3 JAMBES :
  - TP1 = 3xATR M5, TP2 = 4.5xATR, TP3 = 6xATR ; SL commun = 2xATR.
  - Des que TP1 est atteint (la jambe TP1 se ferme en profit), on REMONTE le SL
    des jambes restantes a entree +/- LOCK_ATR (au-dessus de l'ouverture) -> les
    runners deviennent quasi sans risque.
Backtest DAX S10 6 mois (with-trend M5 + filtre correlation) : PF 1.22, Sharpe 1.79
(vs 0.77 en single-leg SL1/TP3).
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

from loguru import logger

from strategy_spec import TradeSetup, SignalDirection

TPS_ATR   = [3.0, 4.5, 6.0]   # multiples d'ATR M5 des 3 jambes
SL_ATR    = 2.0               # SL commun (xATR)
LOCK_ATR  = 0.5               # SL remonte a entree + LOCK_ATR apres TP1
LEG_SIZE  = 0.5               # taille par jambe (clampee au mini IG par l'executeur)
TICK_SEC  = 2.0


def build_legs(direction, entry: float, atr: float, name: str, tps=None) -> List[TradeSetup]:
    """N TradeSetup (jambes) autour de `entry`, SL commun 2xATR, TP echelonnes.
    `tps` = liste des multiples d'ATR (defaut TPS_ATR = 3 jambes 3/4.5/6). Le live
    sous-capitalise passe tps=[3.0, 6.0] (2 jambes : rapide + runner)."""
    tps = tps if tps is not None else TPS_ATR
    long = direction == SignalDirection.LONG
    sl_d = SL_ATR * atr
    legs = []
    for tpm in tps:
        tp_d = tpm * atr
        sl = entry - sl_d if long else entry + sl_d
        tp = entry + tp_d if long else entry - tp_d
        legs.append(TradeSetup(
            direction=direction, entry=round(entry, 2),
            stop_loss=round(sl, 2), take_profit=round(tp, 2),
            risk_pts=round(sl_d, 2), reward_pts=round(tp_d, 2),
            size=LEG_SIZE, setup_name="SCALP3_%s_TP%.1f" % (name, tpm)))
    return legs


class MultiLegManager:
    """Suit les groupes de 3 jambes et remonte le SL des runners apres TP1."""

    def __init__(self, executor, feed, instruments: dict) -> None:
        self.executor = executor
        self.feed = feed
        self._epic = {name: inst.epic for name, inst in instruments.items()}
        self._groups: Dict[str, dict] = {}   # name -> {entry, atr, long, tp1_deal, legs:set, tp1_done, epic}

    def register(self, name: str, direction, entry: float, atr: float, deal_ids: List[str]) -> None:
        """deal_ids dans l'ordre des jambes [TP1, TP2, TP3] (None filtres en amont)."""
        ids = [d for d in deal_ids if d]
        if not ids:
            return
        self._groups[name] = {
            "entry": entry, "atr": atr, "long": direction == SignalDirection.LONG,
            "tp1_deal": deal_ids[0], "legs": set(ids), "tp1_done": False,
            "epic": self._epic.get(name, ""),
        }
        logger.info("[SCALP3] %s : groupe 3 jambes enregistre (entree %.2f, ATR %.2f, deals %s)"
                    % (name, entry, atr, ids))

    async def tick(self) -> None:
        if not self._groups:
            return
        open_ids = {p.deal_id for p in self.executor.open_positions()}
        for name, g in list(self._groups.items()):
            still = g["legs"] & open_ids
            if not still:
                self._groups.pop(name, None)   # toutes les jambes fermees
                continue
            if g["tp1_done"]:
                continue
            # TP1 ferme + au moins une autre jambe encore ouverte = TP1 touche (le SL
            # est COMMUN : un stop fermerait toutes les jambes ensemble). Garde-fou prix.
            if g["tp1_deal"] not in open_ids and len(still) >= 1:
                price = self.feed.get_price(g["epic"]) if g["epic"] else None
                favorable = price is not None and ((price > g["entry"]) if g["long"] else (price < g["entry"]))
                if not favorable:
                    continue   # probablement un SL (pas un TP1) -> on ne remonte pas
                g["tp1_done"] = True
                new_sl = g["entry"] + LOCK_ATR * g["atr"] if g["long"] else g["entry"] - LOCK_ATR * g["atr"]
                new_sl = round(new_sl, 2)
                for d in still:
                    try:
                        await self.executor.move_stop_to(d, new_sl)
                    except Exception as e:
                        logger.error("[SCALP3] move_stop %s : %s" % (d, e))
                logger.info("[SCALP3] %s : TP1 atteint -> SL des %d runner(s) remonte a %.2f "
                            "(entree+%.1fxATR)" % (name, len(still), new_sl, LOCK_ATR))

    async def run(self) -> None:
        logger.info("[SCALP3] gestion multi-jambes active (tick %.0fs | SL %.1fxATR, "
                    "lock TP1 +%.1fxATR, %.2f lot/jambe | TP par jambe selon build_legs)"
                    % (TICK_SEC, SL_ATR, LOCK_ATR, LEG_SIZE))
        while True:
            try:
                await asyncio.sleep(TICK_SEC)
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[SCALP3] tick : %s" % e)
