"""
mt5_poc.py - Proof of Concept : connexion XM via MetaTrader5 (Python).
======================================================================
A LANCER SUR WINDOWS (le package MetaTrader5 exige un terminal MT5 installe).

Prerequis (sur la machine Windows) :
  1. Installer le terminal MetaTrader 5 de XM (depuis my.xm.com -> Plateformes -> MT5),
     et s'y connecter une fois avec le compte DEMO XM.
  2. pip install MetaTrader5
  3. Lancer ce script (le terminal MT5 peut etre ouvert ou non : on l'initialise).

Ce que valide ce PoC (sans risque) :
  - connexion + que le compte est bien un DEMO ;
  - les NOMS de symboles XM pour DAX / CAC40 / OR (inconnus a priori) ;
  - lecture prix live (bid/ask) + dernieres bougies M1 ;
  - (optionnel, --order) un aller-retour d'ordre 0.01 lot sur le compte demo.

Usage :
  python mt5_poc.py --login 12345678 --password "MOT2PASSE" --server "XMGlobal-Demo"
  python mt5_poc.py --login ... --password ... --server ... --order      # teste 1 ordre demo
  (si MT5 est deja connecte au bon compte, --login/--password/--server sont optionnels)

Trouver le 'server' exact : dans MT5, menu Outils -> Options -> Serveur, ou a la
connexion du compte (ex. "XMGlobal-Demo", "XMGlobal-Demo 2", "XM.COM-Demo"...).
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERREUR: 'pip install MetaTrader5' requis (Windows + terminal MT5 installe).")
    sys.exit(1)

# Candidats de noms de symboles cote XM (on cherche par sous-chaine, insensible casse)
SYMBOL_CANDIDATES = {
    "DAX":   ["GER40", "DE40", "GERMANY40", "GER30", "DE30", "DAX"],
    "CAC40": ["FRA40", "FR40", "FRANCE40", "CAC40", "CAC"],
    "GOLD":  ["GOLD", "XAUUSD"],
}

TRADE_MODE = {0: "DEMO", 1: "CONCOURS", 2: "REEL"}


def init(args) -> bool:
    if args.login and args.password and args.server:
        ok = mt5.initialize(login=int(args.login), password=args.password,
                            server=args.server, path=args.path or None)
    else:
        ok = mt5.initialize(path=args.path or None)
    if not ok:
        print("ECHEC mt5.initialize :", mt5.last_error())
        return False
    return True


def show_account() -> int:
    ai = mt5.account_info()
    if ai is None:
        print("account_info indisponible :", mt5.last_error())
        return -1
    mode = TRADE_MODE.get(ai.trade_mode, f"? ({ai.trade_mode})")
    print("\n=== COMPTE ===")
    print(f"  login={ai.login} | serveur={ai.server} | type={mode}")
    print(f"  societe={ai.company}")
    print(f"  solde={ai.balance} {ai.currency} | equity={ai.equity} | levier=1:{ai.leverage}")
    if mode != "DEMO":
        print("  !!! ATTENTION : compte NON-DEMO -> aucun ordre ne sera teste.")
    return ai.trade_mode


def resolve_symbols() -> dict:
    """Trouve les vrais noms de symboles XM pour DAX/CAC/OR."""
    allsyms = mt5.symbols_get()
    print(f"\n=== SYMBOLES ({len(allsyms)} disponibles) ===")
    names = [s.name for s in allsyms]
    resolved = {}
    for key, cands in SYMBOL_CANDIDATES.items():
        matches = []
        for n in names:
            up = n.upper()
            for c in cands:
                if c in up:
                    matches.append(n)
                    break
        # dedup en gardant l'ordre
        seen = set(); matches = [m for m in matches if not (m in seen or seen.add(m))]
        print(f"  {key:6s} -> candidats: {matches[:8]}")
        resolved[key] = matches[0] if matches else None
    return resolved


def show_prices(resolved: dict) -> None:
    print("\n=== PRIX & BOUGIES ===")
    for key, sym in resolved.items():
        if not sym:
            print(f"  {key}: AUCUN symbole trouve")
            continue
        if not mt5.symbol_select(sym, True):
            print(f"  {key} ({sym}): symbol_select echoue : {mt5.last_error()}")
            continue
        info = mt5.symbol_info(sym)
        tick = mt5.symbol_info_tick(sym)
        if info is None or tick is None:
            print(f"  {key} ({sym}): info/tick indisponible")
            continue
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 3)
        nb = 0 if rates is None else len(rates)
        print(f"  {key} ({sym}): bid={tick.bid} ask={tick.ask} "
              f"| point={info.point} digits={info.digits} "
              f"| lot_min={info.volume_min} step={info.volume_step} "
              f"| stops_level={info.trade_stops_level}pts | bougies M1={nb}")


def test_order(resolved: dict, trade_mode: int) -> None:
    if trade_mode != 0:
        print("\n[ORDRE] ignore : compte non-DEMO.")
        return
    sym = resolved.get("GOLD") or next((s for s in resolved.values() if s), None)
    if not sym:
        print("\n[ORDRE] aucun symbole pour tester.")
        return
    info = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)
    vol = info.volume_min
    price = tick.ask
    pt = info.point
    dist = max(info.trade_stops_level, 200) * pt   # SL/TP larges, surs
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(vol),
        "type": mt5.ORDER_TYPE_BUY, "price": price,
        "sl": round(price - dist, info.digits), "tp": round(price + dist * 2, info.digits),
        "deviation": 20, "magic": 990099, "comment": "bonaza_poc",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    print(f"\n[ORDRE] envoi BUY {vol} {sym} @~{price} SL/TP attaches ...")
    res = mt5.order_send(req)
    print(f"  retcode={res.retcode} ({'OK' if res.retcode==mt5.TRADE_RETCODE_DONE else 'KO'}) "
          f"| deal={getattr(res,'deal',None)} order={getattr(res,'order',None)} | {res.comment}")
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        print("  (si 'Unsupported filling mode' : tester ORDER_FILLING_FOK ou RETURN)")
        return
    time.sleep(2)
    # fermeture immediate (aller-retour de validation)
    poss = mt5.positions_get(symbol=sym)
    for p in poss or []:
        if p.magic != 990099:
            continue
        t = mt5.symbol_info_tick(sym)
        close = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL, "position": p.ticket, "price": t.bid,
            "deviation": 20, "magic": 990099, "comment": "bonaza_poc_close",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        r = mt5.order_send(close)
        print(f"  fermeture ticket {p.ticket}: retcode={r.retcode}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login")
    ap.add_argument("--password")
    ap.add_argument("--server")
    ap.add_argument("--path", help="chemin terminal64.exe (optionnel)")
    ap.add_argument("--order", action="store_true", help="teste un aller-retour d'ordre demo")
    args = ap.parse_args()

    print("MetaTrader5 package version:", mt5.__version__ if hasattr(mt5, "__version__") else "?")
    if not init(args):
        sys.exit(1)
    try:
        ti = mt5.terminal_info()
        print("terminal:", ti.name, "| connecte:", ti.connected, "| trade_allowed:", ti.trade_allowed)
        mode = show_account()
        resolved = resolve_symbols()
        show_prices(resolved)
        if args.order:
            test_order(resolved, mode)
        print("\nPoC TERMINE.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
