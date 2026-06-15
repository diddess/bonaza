# test_chart.py - Diagnostic abonnement CHART Lightstreamer
# Teste differents formats d'EPIC et SCALE
# Usage : python src\test_chart.py
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
from config import config
from trading_ig import IGService, IGStreamService

try:
    from lightstreamer.client import Subscription, SubscriptionListener
    LS_OK = True
except ImportError:
    print("ERREUR : lightstreamer-client non installe")
    LS_OK = False

if not LS_OK:
    sys.exit(1)

class DiagListener(SubscriptionListener):
    def __init__(self, name):
        self.name = name
        self.ok = False
        self.error_code = None
        self.error_msg = None

    def onSubscription(self):
        self.ok = True
        print(f"  [OK] {self.name} -> abonnement ACCEPTE")

    def onSubscriptionError(self, code, message):
        self.error_code = code
        self.error_msg = message
        print(f"  [KO] {self.name} -> ERREUR code={code} message='{message}'")

    def onItemUpdate(self, update):
        print(f"  [DATA] {self.name} -> update recu !")

    def onUnsubscription(self):
        pass

# Connexion REST
print("Connexion REST...")
ig = IGService(
    username   = config.ig.identifier,
    password   = config.ig.password,
    api_key    = config.ig.api_key,
    acc_type   = config.ig.account_type,
    acc_number = config.ig.account_id or None,
)
session = ig.create_session()
print(f"REST OK | Account: {session.get('currentAccountId')} | LS: {session.get('lightstreamerEndpoint')}")

# Connexion Lightstreamer
print("\nConnexion Lightstreamer...")
ig_stream = IGStreamService(ig)
ig_stream.create_session(version="3")
print("Lightstreamer OK")

# Test differents formats
test_cases = [
    ("CHART:CS.D.USCGOLD.CFD.IP:5MINUTE",  ["CONS_END","BID_CLOSE","OFR_CLOSE","UTM"]),
    ("CHART:CS.D.USCGOLD.CFD.IP:1MINUTE",  ["CONS_END","BID_CLOSE","OFR_CLOSE","UTM"]),
    ("CHART:CS.D.USCGOLD.MINI.IP:5MINUTE", ["CONS_END","BID_CLOSE","OFR_CLOSE","UTM"]),
]

listeners = []
print("\nTest des abonnements CHART (attente 5 secondes)...")
for item_name, fields in test_cases:
    print(f"\n  Tentative : {item_name}")
    listener = DiagListener(item_name)
    sub = Subscription(mode="MERGE", items=[item_name], fields=fields)
    sub.addListener(listener)
    ig_stream.subscribe(sub)
    listeners.append((listener, sub))

time.sleep(5)

print("\n=== RESULTATS ===")
for listener, sub in listeners:
    status = "OK" if listener.ok else f"KO (code={listener.error_code} msg='{listener.error_msg}')"
    print(f"  {listener.name[:50]:50s} -> {status}")

ig_stream.disconnect()
ig.logout()
print("\nDiagnostic termine.")
