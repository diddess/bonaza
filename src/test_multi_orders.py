"""
test_multi_orders.py - Test multi-instruments : 3 ouvertures + modif SL + cloture
==================================================================================
Sequence :
  1. Connexion IG (compte DEMO uniquement)
  2. Ouverture LONG sur XAUUSD, DAX, CAC40 (SL initial large + TP large)
  3. Attente 60 sec
  4. Modification du SL a entry - 10 pts pour chaque position ouverte
  5. Attente 30 sec
  6. Cloture des positions (SELL inverse)
  7. Resume P&L total

Securite :
  - DEMO uniquement (refus si IG_ACCOUNT_TYPE != DEMO)
  - SL pose des l'ouverture (initial large : 20 pts)
  - Try/except generaux + finally pour TOUJOURS tenter la cloture
  - Si une ouverture echoue, on continue avec les autres
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))

from config import config

try:
    from trading_ig import IGService
except ImportError:
    print("ERREUR : trading-ig non installe.")
    sys.exit(1)


# -----------------------------------------------------------------------
# Configuration du test
# -----------------------------------------------------------------------

@dataclass
class TestInstrument:
    name: str
    epic: str
    size: float
    point_value_eur: float   # pour estimer la perte/gain en EUR

INSTRUMENTS = [
    TestInstrument("XAUUSD", "CS.D.CFEGOLD.CFE.IP", size=0.5, point_value_eur=1.0),
    TestInstrument("DAX",    "IX.D.DAX.IFMM.IP",    size=0.5, point_value_eur=1.0),  # mini 1 EUR/pt
    TestInstrument("CAC40",  "IX.D.CAC.IMF.IP",     size=0.5, point_value_eur=1.0),  # mini 1 EUR/pt
]

DIRECTION_OPEN     = "BUY"        # LONG
SL_DIST_INITIAL    = 30.0          # pts, large pour ne pas etre stoppe pendant les 60s d'attente
TP_DIST_INITIAL    = 60.0          # pts, large (au-dela du nouveau SL apres modif)
SL_FINAL_PIPS      = 10.0          # SL final = entry - 10 pts (pour BUY)
WAIT_BEFORE_MODIFY = 60.0          # 1 minute
WAIT_BEFORE_CLOSE  = 30.0          # 30 secondes


@dataclass
class OpenedPosition:
    inst:        TestInstrument
    deal_id:     str
    deal_ref:    str
    entry_level: float
    sl_initial:  float
    tp_initial:  float
    sl_modified: Optional[float] = None
    exit_level:  Optional[float] = None
    profit_eur:  Optional[float] = None
    error:       Optional[str]   = None


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def banner(title: str) -> None:
    bar = "=" * 64
    print(f"\n{bar}\n  {title}\n{bar}")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def connect_ig() -> IGService:
    print(f"[{now()}] Connexion IG Markets...")
    print(f"  Identifier   : {config.ig.identifier}")
    print(f"  Account type : {config.ig.account_type}")
    print(f"  Account id   : {config.ig.account_id}")
    ig = IGService(
        username   = config.ig.identifier,
        password   = config.ig.password,
        api_key    = config.ig.api_key,
        acc_type   = config.ig.account_type,
        acc_number = config.ig.account_id or None,
    )
    session = ig.create_session()
    info  = session.get("accountInfo", {})
    bal   = info.get("balance", "?")
    avail = info.get("available", "?")
    print(f"  Solde initial: {bal} EUR  (dispo: {avail})")
    return ig, float(bal) if isinstance(bal, (int, float)) else None


def open_one(ig: IGService, inst: TestInstrument) -> OpenedPosition:
    print(f"\n[{now()}] OUVERTURE {inst.name} ({inst.epic}) {DIRECTION_OPEN} size={inst.size}")
    try:
        r = ig.create_open_position(
            currency_code  = "EUR",
            direction      = DIRECTION_OPEN,
            epic           = inst.epic,
            expiry         = "-",
            force_open     = True,
            guaranteed_stop= False,
            order_type     = "MARKET",
            size           = inst.size,
            stop_distance  = SL_DIST_INITIAL, stop_level  = None,
            limit_distance = TP_DIST_INITIAL, limit_level = None,
            level          = None,            quote_id    = None,
            trailing_stop  = False,           trailing_stop_increment = None,
        )
        status = r.get("dealStatus", "?")
        reason = r.get("reason", "?")
        print(f"  -> dealStatus={status} reason={reason}")
        if status != "ACCEPTED":
            return OpenedPosition(
                inst=inst, deal_id="", deal_ref=r.get("dealReference", ""),
                entry_level=0.0, sl_initial=0.0, tp_initial=0.0,
                error=f"{status}/{reason}",
            )
        entry = float(r.get("level", 0) or 0)
        sl    = float(r.get("stopLevel", 0) or 0)
        tp    = float(r.get("limitLevel", 0) or 0)
        print(f"  -> dealId={r.get('dealId')} entry={entry} SL={sl} TP={tp}")
        return OpenedPosition(
            inst=inst, deal_id=r.get("dealId", ""), deal_ref=r.get("dealReference", ""),
            entry_level=entry, sl_initial=sl, tp_initial=tp,
        )
    except Exception as e:
        print(f"  -> ERREUR : {e}")
        return OpenedPosition(
            inst=inst, deal_id="", deal_ref="",
            entry_level=0.0, sl_initial=0.0, tp_initial=0.0,
            error=str(e),
        )


def modify_sl(ig: IGService, p: OpenedPosition) -> None:
    new_sl = round(p.entry_level - SL_FINAL_PIPS, 2)    # BUY : SL sous entry
    print(f"\n[{now()}] MODIF SL {p.inst.name} dealId={p.deal_id} : "
          f"{p.sl_initial} -> {new_sl}  (entry={p.entry_level})")
    try:
        r = ig.update_open_position(
            limit_level    = p.tp_initial,
            stop_level     = new_sl,
            deal_id        = p.deal_id,
            guaranteed_stop= False,
            trailing_stop  = False,
            trailing_stop_distance  = None,
            trailing_stop_increment = None,
        )
        status = r.get("dealStatus", "?")
        reason = r.get("reason", "?")
        print(f"  -> dealStatus={status} reason={reason} "
              f"stopLevel={r.get('stopLevel')} limitLevel={r.get('limitLevel')}")
        if status == "ACCEPTED":
            p.sl_modified = new_sl
    except Exception as e:
        print(f"  -> ERREUR : {e}")


def close_one(ig: IGService, p: OpenedPosition) -> None:
    inverse = "SELL" if DIRECTION_OPEN == "BUY" else "BUY"
    print(f"\n[{now()}] CLOTURE {p.inst.name} dealId={p.deal_id} : {inverse} {p.inst.size}")
    try:
        r = ig.close_open_position(
            deal_id    = p.deal_id,
            direction  = inverse,
            epic       = None, expiry  = None,
            level      = None, quote_id= None,
            order_type = "MARKET",
            size       = p.inst.size,
        )
        status = r.get("dealStatus", "?")
        reason = r.get("reason", "?")
        exit_lvl = float(r.get("level", 0) or 0)
        p.exit_level = exit_lvl
        pts = exit_lvl - p.entry_level   # BUY
        p.profit_eur = round(pts * p.inst.size * p.inst.point_value_eur, 2)
        print(f"  -> dealStatus={status} reason={reason} exit={exit_lvl} "
              f"pts={pts:+.2f} pnl~={p.profit_eur:+.2f} EUR")
    except Exception as e:
        print(f"  -> ERREUR : {e}")


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    banner(f"BONAZA - TEST MULTI-ORDRES {DIRECTION_OPEN} 3 INDICES - {datetime.now(timezone.utc).isoformat()}")

    if config.ig.account_type.upper() != "DEMO":
        print(f"REFUSE : compte non-DEMO ({config.ig.account_type}). Aborter.")
        sys.exit(2)

    ig, balance_before = connect_ig()

    print(f"\nPlan :")
    print(f"  1. Ouverture {DIRECTION_OPEN} sur {len(INSTRUMENTS)} instruments "
          f"(SL initial {SL_DIST_INITIAL}pts, TP {TP_DIST_INITIAL}pts)")
    print(f"  2. Attente {WAIT_BEFORE_MODIFY}s")
    print(f"  3. Modification SL -> entry - {SL_FINAL_PIPS}pts")
    print(f"  4. Attente {WAIT_BEFORE_CLOSE}s")
    print(f"  5. Cloture des positions ouvertes")

    positions: list[OpenedPosition] = []

    try:
        banner("PHASE 1 - OUVERTURES")
        for inst in INSTRUMENTS:
            positions.append(open_one(ig, inst))

        ok_positions = [p for p in positions if p.deal_id and not p.error]
        print(f"\n[{now()}] Resultat phase 1 : {len(ok_positions)}/{len(INSTRUMENTS)} positions ouvertes")
        for p in positions:
            if p.error:
                print(f"  KO {p.inst.name}: {p.error}")
            else:
                print(f"  OK {p.inst.name}: dealId={p.deal_id} entry={p.entry_level}")

        if not ok_positions:
            print("Aucune position ouverte. Abandon.")
            return

        banner(f"PHASE 2 - ATTENTE {WAIT_BEFORE_MODIFY}s")
        for sec in range(int(WAIT_BEFORE_MODIFY), 0, -10):
            print(f"  [{now()}] {sec}s restantes...")
            time.sleep(10)

        banner("PHASE 3 - MODIFICATION SL -> entry - 10 pts")
        for p in ok_positions:
            modify_sl(ig, p)

        banner(f"PHASE 4 - ATTENTE {WAIT_BEFORE_CLOSE}s")
        for sec in range(int(WAIT_BEFORE_CLOSE), 0, -10):
            print(f"  [{now()}] {sec}s restantes...")
            time.sleep(10)

    finally:
        banner("PHASE 5 - CLOTURES (toujours executee)")
        for p in [pp for pp in positions if pp.deal_id]:
            close_one(ig, p)

    # Resume
    banner("RESUME FINAL")
    total_pnl = 0.0
    for p in positions:
        if p.error and not p.deal_id:
            print(f"  {p.inst.name:7s} ECHEC OUVERTURE : {p.error}")
            continue
        pnl_str = f"{p.profit_eur:+.2f} EUR" if p.profit_eur is not None else "N/A"
        sl_str  = (f"{p.sl_initial} -> {p.sl_modified}"
                   if p.sl_modified is not None else f"{p.sl_initial} (non modif)")
        exit_str = f"{p.exit_level}" if p.exit_level is not None else "non ferme"
        print(f"  {p.inst.name:7s} entry={p.entry_level} SL={sl_str} exit={exit_str} pnl={pnl_str}")
        if p.profit_eur is not None:
            total_pnl += p.profit_eur

    print(f"\n  TOTAL P&L estime : {total_pnl:+.2f} EUR")

    try:
        s = ig.fetch_accounts()
        for acc in s.get("accounts", []):
            if acc.get("accountId") == config.ig.account_id:
                print(f"  Solde apres test : {acc.get('balance', {}).get('balance')} EUR")
                break
    except Exception:
        pass


if __name__ == "__main__":
    main()
