# search_epic2.py - Cherche l'EPIC Gold avec tokens de session existants
# Usage : python src\search_epic2.py
# Attend 10 secondes entre les tentatives pour eviter le rate limit IG
import sys, os, time, requests
sys.path.insert(0, os.path.dirname(__file__))
from config import config

print("Attente 10s pour eviter le rate limit IG...")
time.sleep(10)

from trading_ig import IGService

# Une seule session, utilisee pour tout
ig = IGService(
    username   = config.ig.identifier,
    password   = config.ig.password,
    api_key    = config.ig.api_key,
    acc_type   = config.ig.account_type,
    acc_number = config.ig.account_id or None,
)

try:
    session = ig.create_session()
    print(f"Session OK | Account: {session.get('currentAccountId')}")
except Exception as e:
    print(f"Session ECHOUEE : {e}")
    print("Attends encore 2-3 minutes (rate limit IG) puis relance.")
    sys.exit(1)

# Tokens
cst   = ig.crud_session.CLIENT_TOKEN
token = ig.crud_session.SECURITY_TOKEN
base  = ig.crud_session.BASE_URL

headers = {
    "X-IG-API-KEY": config.ig.api_key,
    "CST": cst,
    "X-SECURITY-TOKEN": token,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Version": "1",
}

print()
print("=== Recherche EPICs Gold avec streaming ===")
for term in ["gold", "xauusd", "or"]:
    url = f"{base}/markets?searchTerm={term}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            markets = r.json().get("markets", [])
            streaming = [m for m in markets if m.get("streamingPricesAvailable")]
            if streaming:
                print(f"\nTerm '{term}' -> {len(streaming)} avec streaming :")
                for m in streaming[:10]:
                    print(f"  EPIC: {m.get('epic','?'):40s} | {m.get('instrumentName','?')}")
        else:
            print(f"Erreur {r.status_code} pour '{term}'")
        time.sleep(1)  # Pause entre requetes
    except Exception as e:
        print(f"Erreur pour '{term}': {e}")

ig.logout()
print("\nTermine.")
