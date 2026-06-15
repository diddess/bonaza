# test_feed.py - Diagnostic connexion Lightstreamer IG Markets
# Usage : python src\test_feed.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from config import config
from trading_ig import IGService, IGStreamService

print(f"Identifier : {config.ig.identifier}")
print(f"Account type : {config.ig.account_type}")
print(f"Account ID   : {config.ig.account_id}")
print(f"API key (4) : {config.ig.api_key[:4]}...")
print()

# Etape 1 : session REST
print("=== Etape 1 : Session REST ===")
try:
    ig = IGService(
        username   = config.ig.identifier,
        password   = config.ig.password,
        api_key    = config.ig.api_key,
        acc_type   = config.ig.account_type,
        acc_number = config.ig.account_id or None,
    )
    import pprint
    session = ig.create_session()
    print("REST OK")
    ls_endpoint = session.get("lightstreamerEndpoint", "?")
    print(f"Lightstreamer endpoint : {ls_endpoint}")
    cst   = ig.session.headers.get("CST")
    token = ig.session.headers.get("X-SECURITY-TOKEN")
    print(f"CST   (4 chars): {str(cst)[:4]}...")
    print(f"TOKEN (4 chars): {str(token)[:4]}...")
    account_id = session.get("currentAccountId", "?")
    print(f"Current account : {account_id}")
except Exception as e:
    print(f"ECHEC REST : {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

print()

# Etape 2 : session Lightstreamer
print("=== Etape 2 : Session Lightstreamer (streaming) ===")
try:
    ig_stream = IGStreamService(ig)
    ig_stream.create_session(version="3")
    print("Lightstreamer OK")
    ig_stream.disconnect()
except Exception as e:
    print(f"ECHEC Lightstreamer : {type(e).__name__} : {e}")
    import traceback; traceback.print_exc()
    # Essayer sans version
    print()
    print("Tentative sans version explicite...")
    try:
        ig_stream2 = IGStreamService(ig)
        ig_stream2.create_session()
        print("Lightstreamer OK (sans version)")
        ig_stream2.disconnect()
    except Exception as e2:
        print(f"ECHEC aussi : {e2}")

ig.logout()
print("\nDiagnostic termine.")
