# find_gold_epic.py - Trouve l'EPIC Gold disponible sur ce compte DEMO
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

try:
    session = ig.create_session()
    print(f"Session OK | {session.get('currentAccountId')}\n")
except Exception as e:
    print(f"ECHEC SESSION : {e}")
    print("Attends encore 3 minutes puis relance.")
    sys.exit(1)

# Inspecter les attributs disponibles pour trouver les tokens
print("=== Attributs crud_session ===")
attrs = [a for a in dir(ig.crud_session) if not a.startswith('__')]
for a in attrs:
    try:
        val = getattr(ig.crud_session, a)
        if isinstance(val, str) and len(val) > 5:
            print(f"  {a} = {val[:20]}...")
    except Exception:
        pass

# Recuperer les headers depuis la session directement
# trading_ig stocke les tokens dans la session requests
s = ig.crud_session.session  # session requests
cst   = s.headers.get("CST", "")
token = s.headers.get("X-SECURITY-TOKEN", "")
base  = ig.crud_session.BASE_URL

print(f"\nCST   (4 chars) : {cst[:4]}...")
print(f"TOKEN (4 chars) : {token[:4]}...")
print(f"BASE URL        : {base}")

if not cst or not token:
    print("\nTokens non trouves dans les headers. Tentative methode alternative...")
    # Chercher dans les attributs
    for a in attrs:
        val = str(getattr(ig.crud_session, a, ""))
        if len(val) > 20 and any(c.isalpha() for c in val):
            print(f"  possible token: {a} = {val[:30]}")

headers = {
    "X-IG-API-KEY"    : config.ig.api_key,
    "CST"             : cst,
    "X-SECURITY-TOKEN": token,
    "Accept"          : "application/json",
    "Version"         : "1",
}

print("\n=== Recherche Gold via REST ===")
for term in ["gold", "xauusd"]:
    try:
        r = requests.get(f"{base}/markets?searchTerm={term}", headers=headers, timeout=10)
        if r.status_code == 200:
            markets = r.json().get("markets", [])
            print(f"\nTerm '{term}' -> {len(markets)} resultats :")
            for m in markets[:15]:
                streaming = m.get("streamingPricesAvailable", False)
                flag = "STREAMING" if streaming else "no-stream"
                print(f"  [{flag}] {m.get('epic','?'):40s} | {m.get('instrumentName','?')}")
        else:
            print(f"Erreur {r.status_code}: {r.text[:150]}")
        time.sleep(0.5)
    except Exception as e:
        print(f"Erreur '{term}': {e}")

# Aussi essayer les watchlists
print("\n=== Watchlists ===")
try:
    r = requests.get(f"{base}/watchlists", headers=headers, timeout=10)
    if r.status_code == 200:
        for w in r.json().get("watchlists", [])[:5]:
            print(f"  Watchlist: {w['id']} | {w['name']}")
            r2 = requests.get(f"{base}/watchlists/{w['id']}", headers=headers, timeout=10)
            if r2.status_code == 200:
                for m in r2.json().get("markets", []):
                    name = m.get("instrumentName","").lower()
                    if any(k in name for k in ["gold","xau","or"]):
                        print(f"    >> {m.get('epic'):40s} | {m.get('instrumentName')} | streaming={m.get('streamingPricesAvailable')}")
            time.sleep(0.3)
except Exception as e:
    print(f"Erreur watchlists: {e}")

ig.logout()
print("\nTermine.")
