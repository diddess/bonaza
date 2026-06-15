# get_account_id.py - Recupere les infos de session IG Markets
# Usage : python src\get_account_id.py
import os, sys, pprint
sys.path.insert(0, os.path.dirname(__file__))
from config import config

print(f"Connexion avec {config.ig.identifier} ({config.ig.account_type})...")
print(f"API key (4 premiers chars) : {config.ig.api_key[:4]}...")

try:
    from trading_ig import IGService

    # acc_type doit etre "CFD" ou "SPREADBET", pas "DEMO"
    # Le mode DEMO/LIVE est determine par l'account number
    for acc_type in ["CFD", "SPREADBET"]:
        print(f"\n--- Tentative avec acc_type='{acc_type}' ---")
        try:
            ig = IGService(
                username = config.ig.identifier,
                password = config.ig.password,
                api_key  = config.ig.api_key,
                acc_type = acc_type,
            )
            session = ig.create_session()
            print(f"SUCCES avec acc_type='{acc_type}'")
            print("\nInfos de session :")
            pprint.pprint(session)

            # Recuperer les comptes
            try:
                accounts = ig.fetch_accounts()
                print("\nComptes disponibles :")
                pprint.pprint(accounts)
            except Exception as e2:
                print(f"fetch_accounts : {e2}")

            ig.logout()
            print(f"\n=== RESULTAT : utiliser acc_type='{acc_type}' dans .env ===")
            print(f"Mettre IG_ACCOUNT_TYPE={acc_type} dans .env")
            break

        except Exception as e:
            print(f"Echec avec acc_type='{acc_type}' : {e}")

except Exception as e:
    print(f"Erreur fatale : {e}")
    import traceback; traceback.print_exc()
