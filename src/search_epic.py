# search_epic.py - Trouve les EPICs disponibles sur compte DEMO Z5GGQ7
import sys, os, time, requests
sys.path.insert(0, os.path.dirname(__file__))
from config import config
from trading_ig import IGService

ig = IGService(
    username   = config.ig.identifier,
    password   = config.ig.password,
    api_key    = config.ig.api_key,
    acc_type   = config.ig.account_type,
    acc_number = config.ig.account_id or None,
)
session = ig.create_session()
print(f"Connecte : {session.get('currentAccountId')}\n")

s     = ig.crud_session.session
cst   = s.headers.get("CST", "")
token = s.headers.get("X-SECURITY-TOKEN", "")
base  = ig.crud_session.BASE_URL

headers = {
    "X-IG-API-KEY"    : config.ig.api_key,
    "CST"             : cst,
    "X-SECURITY-TOKEN": token,
    "Accept"          : "application/json",
    "Version"         : "1",
}

for term in ["DAX", "france", "cac", "gold"]:
    print(f"=== '{term}' ===")
    try:
        r = requests.get(
            f"{base}/markets?searchTerm={term}",
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            markets = r.json().get("markets", [])
            streaming = [m for m in markets if m.get("streamingPricesAvailable")]
            for m in streaming[:8]:
                print(f"  {m.get('epic','?'):40s} | {m.get('instrumentName','?')}")
            if not streaming:
                print("  (aucun avec streaming)")
        else:
            print(f"  Erreur {r.status_code}")
    except Exception as e:
        print(f"  Timeout/erreur : {e}")
    time.sleep(1)
    print()

ig.logout()
