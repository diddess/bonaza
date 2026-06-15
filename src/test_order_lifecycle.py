"""
test_order_lifecycle.py - Test interactif du cycle complet d'un ordre IG Markets
================================================================================
Valide les 4 operations API sur le compte DEMO :
  1. OUVERTURE   : create_open_position (avec SL + TP en distance)
  2. CONSULTATION: fetch_open_position(deal_id)  ou fetch_open_positions
  3. MODIFICATION: update_open_position (deplace SL et TP)
  4. CLOTURE     : close_open_position (direction inverse)

Usage : python src\\test_order_lifecycle.py
        python src\\test_order_lifecycle.py --no-confirm    # auto, pas de prompt
        python src\\test_order_lifecycle.py --size 0.5      # taille personnalisee
        python src\\test_order_lifecycle.py --epic XYZ      # autre EPIC

Securite :
  - Compte DEMO uniquement (refuse si IG_ACCOUNT_TYPE != DEMO sauf --force-live)
  - SL et TP TOUJOURS poses des l'ouverture
  - Confirmation interactive a chaque etape (sauf --no-confirm)
  - Tente toujours la cloture finale, meme apres une erreur en cours de route
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from config import config

try:
    from trading_ig import IGService
except ImportError:
    print("ERREUR : trading-ig non installe. pip install trading-ig")
    sys.exit(1)


DEFAULT_EPIC      = "CS.D.CFEGOLD.CFE.IP"   # XAUUSD spot DEMO
DEFAULT_SIZE      = 0.5                     # 0.5 lot mini XAUUSD
DEFAULT_DIRECTION = "BUY"                   # LONG
SL_DISTANCE_INIT  = 5.0                     # 5 pts initial
TP_DISTANCE_INIT  = 10.0                    # 10 pts initial
SL_DISTANCE_NEW   = 3.0                     # SL rapproche
TP_DISTANCE_NEW   = 8.0                     # TP rapproche


def banner(title: str) -> None:
    bar = "=" * 60
    print(f"\n{bar}\n  {title}\n{bar}")


def confirm(msg: str, auto: bool) -> bool:
    if auto:
        print(f"{msg} [auto: OUI]")
        return True
    ans = input(f"{msg} [O/n] : ").strip().lower()
    return ans in ("", "o", "oui", "y", "yes")


def connect_ig() -> IGService:
    print(f"Connexion IG Markets...")
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
    cur     = session.get("currencyIsoCode", "?")
    info    = session.get("accountInfo", {})
    bal     = info.get("balance",   "?")
    avail   = info.get("available", "?")
    print(f"  Solde        : {bal} {cur}  (dispo: {avail})")
    return ig


def fetch_market_snapshot(ig: IGService, epic: str) -> dict:
    """Affiche bid/offer/spread courant pour reperer le prix d'execution attendu."""
    try:
        m = ig.fetch_market(epic)
        snap = m.get("snapshot", {})
        bid    = snap.get("bid")
        offer  = snap.get("offer")
        status = snap.get("marketStatus", "?")
        print(f"  Marche {epic}")
        print(f"    bid={bid}  offer={offer}  status={status}")
        return snap
    except Exception as e:
        print(f"  fetch_market : {e}")
        return {}


def open_position(
    ig: IGService, epic: str, direction: str, size: float,
    sl_distance: float, tp_distance: float,
) -> dict:
    banner("1. OUVERTURE POSITION")
    print(f"  EPIC      : {epic}")
    print(f"  Direction : {direction}")
    print(f"  Size      : {size}")
    print(f"  SL dist   : {sl_distance} pts")
    print(f"  TP dist   : {tp_distance} pts")

    response = ig.create_open_position(
        currency_code  = "EUR",
        direction      = direction,
        epic           = epic,
        expiry         = "-",
        force_open     = True,
        guaranteed_stop= False,
        order_type     = "MARKET",
        size           = size,
        stop_distance  = sl_distance, stop_level    = None,
        limit_distance = tp_distance, limit_level   = None,
        level          = None,        quote_id      = None,
        trailing_stop  = False,       trailing_stop_increment = None,
    )
    print(f"\n  Reponse IG :")
    for k in ("dealStatus", "reason", "dealId", "dealReference",
              "level", "size", "stopLevel", "limitLevel"):
        if k in response:
            print(f"    {k:14s} = {response.get(k)}")
    return response


def fetch_position(ig: IGService, deal_id: str) -> dict | None:
    """Recupere une position ouverte par deal_id."""
    try:
        # API directe : GET /positions/{dealId}
        p = ig.fetch_open_position_by_deal_id(deal_id)
        return p
    except AttributeError:
        # fallback : lister toutes et filtrer
        try:
            resp = ig.fetch_open_positions()
            for item in resp.get("positions", []):
                if item.get("position", {}).get("dealId") == deal_id:
                    return item
        except Exception as e:
            print(f"  fetch_open_positions : {e}")
    except Exception as e:
        print(f"  fetch_open_position_by_deal_id : {e}")
    return None


def print_position(label: str, pos: dict | None) -> None:
    if pos is None:
        print(f"  {label} : position introuvable")
        return
    p = pos.get("position", pos)
    print(f"  {label}")
    for k in ("dealId", "direction", "size", "openLevel",
              "stopLevel", "limitLevel"):
        if k in p:
            print(f"    {k:12s} = {p.get(k)}")


def modify_stops(
    ig: IGService, deal_id: str, new_stop_level: float, new_limit_level: float,
) -> dict:
    banner("3. MODIFICATION SL / TP")
    print(f"  deal_id     : {deal_id}")
    print(f"  Nouveau SL  : {new_stop_level}")
    print(f"  Nouveau TP  : {new_limit_level}")
    response = ig.update_open_position(
        limit_level    = new_limit_level,
        stop_level     = new_stop_level,
        deal_id        = deal_id,
        guaranteed_stop= False,
        trailing_stop  = False,
        trailing_stop_distance  = None,
        trailing_stop_increment = None,
    )
    print(f"\n  Reponse IG :")
    for k in ("dealStatus", "reason", "dealId", "stopLevel", "limitLevel"):
        if k in response:
            print(f"    {k:14s} = {response.get(k)}")
    return response


def close_position(
    ig: IGService, deal_id: str, direction_open: str, size: float,
) -> dict:
    banner("4. CLOTURE POSITION")
    inverse = "SELL" if direction_open == "BUY" else "BUY"
    print(f"  deal_id          : {deal_id}")
    print(f"  Direction ouvert : {direction_open}")
    print(f"  Ordre cloture    : {inverse} {size}")
    response = ig.close_open_position(
        deal_id    = deal_id,
        direction  = inverse,
        epic       = None,    # IG cloture par deal_id
        expiry     = None,
        level      = None,
        order_type = "MARKET",
        quote_id   = None,
        size       = size,
    )
    print(f"\n  Reponse IG :")
    for k in ("dealStatus", "reason", "dealId", "level", "size", "profit"):
        if k in response:
            print(f"    {k:14s} = {response.get(k)}")
    return response


def run_test(epic: str, size: float, direction: str, auto: bool) -> int:
    banner(f"BONAZA - TEST CYCLE ORDRE - {datetime.now(timezone.utc).isoformat()}")

    if config.ig.account_type.upper() != "DEMO":
        print(f"\nREFUSE : compte non-DEMO ({config.ig.account_type}). Aborter.")
        return 2

    ig = connect_ig()
    fetch_market_snapshot(ig, epic)

    if not confirm(f"\nOuvrir un {direction} {size} sur {epic} (DEMO) ?", auto):
        print("Abandon avant ouverture.")
        return 0

    deal_id      = None
    open_level   = None
    open_resp    = None
    overall_ok   = True

    try:
        open_resp = open_position(
            ig, epic, direction, size,
            sl_distance=SL_DISTANCE_INIT, tp_distance=TP_DISTANCE_INIT,
        )
        if open_resp.get("dealStatus") != "ACCEPTED":
            print(f"\nOuverture REJETEE : {open_resp.get('reason')}")
            return 3
        deal_id    = open_resp.get("dealId")
        open_level = float(open_resp.get("level", 0) or 0)

        # Petit delai pour que la position soit visible
        time.sleep(2)

        banner("2. CONSULTATION POSITION")
        pos = fetch_position(ig, deal_id)
        print_position("Etat apres ouverture", pos)

        if not confirm("\nModifier SL et TP ?", auto):
            print("Skip modification, on enchaine sur cloture.")
        else:
            new_sl = round(open_level - SL_DISTANCE_NEW, 2) if direction == "BUY" \
                     else round(open_level + SL_DISTANCE_NEW, 2)
            new_tp = round(open_level + TP_DISTANCE_NEW, 2) if direction == "BUY" \
                     else round(open_level - TP_DISTANCE_NEW, 2)
            try:
                modify_stops(ig, deal_id, new_sl, new_tp)
                time.sleep(2)
                pos2 = fetch_position(ig, deal_id)
                print_position("Etat apres modification", pos2)
            except Exception as e:
                print(f"\nERREUR modification (on continue): {e}")
                overall_ok = False

        if not confirm("\nFermer la position maintenant ?", auto):
            print(f"\nPosition LAISSEE OUVERTE. deal_id={deal_id}. A fermer manuellement.")
            return 0

    except Exception as e:
        print(f"\nERREUR INATTENDUE : {e}")
        import traceback; traceback.print_exc()
        overall_ok = False

    finally:
        if deal_id is not None:
            try:
                close_position(ig, deal_id, direction, size)
            except Exception as e:
                print(f"\nERREUR CLOTURE : {e}")
                print(f"Position {deal_id} possiblement encore ouverte. Verifier dans IG.")
                overall_ok = False

    banner("RESULTAT GLOBAL : " + ("OK" if overall_ok else "PARTIEL / ERREURS"))
    return 0 if overall_ok else 4


def main():
    p = argparse.ArgumentParser(description="Test cycle ordre IG Markets")
    p.add_argument("--epic",      default=DEFAULT_EPIC)
    p.add_argument("--size",      type=float, default=DEFAULT_SIZE)
    p.add_argument("--direction", default=DEFAULT_DIRECTION, choices=["BUY", "SELL"])
    p.add_argument("--no-confirm", action="store_true",
                   help="Pas de prompt interactif (auto)")
    args = p.parse_args()
    code = run_test(args.epic, args.size, args.direction, auto=args.no_confirm)
    sys.exit(code)


if __name__ == "__main__":
    main()
