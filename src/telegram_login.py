"""
telegram_login.py — connexion interactive du compte Telegram (Telethon).
=========================================================================
A LANCER UNE FOIS, en interactif (TTY), via SSH sur le VPS :

    ssh bonaza
    docker exec -it bonaza_main python /app/src/telegram_login.py

Le script demande api_id / api_hash / numero (ou les lit dans les variables
d'env TG_API_ID / TG_API_HASH / TG_PHONE), puis Telegram envoie un CODE dans
ton app -> tu le saisis. Si tu as la 2FA, mot de passe demande aussi.

Resultat : une SESSION persistante dans data/tg_signals.session (acces complet
au compte -> fichier protege). Et la liste de tes groupes/canaux pour qu'on
identifie le groupe de signaux (note son id).
"""
import os

SESSION = "/app/data/tg_signals"   # -> /app/data/tg_signals.session (volume persistant)

def main():
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        print("ERREUR : telethon non installe dans l'image (rebuild requis).")
        return

    api_id  = os.getenv("TG_API_ID")  or input("api_id (my.telegram.org) : ").strip()
    api_hash = os.getenv("TG_API_HASH") or input("api_hash : ").strip()
    phone   = os.getenv("TG_PHONE")   or input("Numero international (ex +596...) : ").strip()

    client = TelegramClient(SESSION, int(api_id), api_hash)
    client.start(phone=phone)   # demande le CODE Telegram + 2FA si active
    me = client.get_me()
    print("\n>>> Connecte : %s %s (@%s)" % (
        me.first_name or "", me.last_name or "", me.username or "?"))
    print("\n=== GROUPES / CANAUX (repere celui des signaux + note son id) ===")
    for d in client.iter_dialogs():
        if getattr(d, "is_group", False) or getattr(d, "is_channel", False):
            print("  id=%-16s | %s" % (d.id, d.name))
    client.disconnect()
    print("\n>>> Session sauvegardee : %s.session" % SESSION)
    print(">>> Donne-moi l'id du groupe de signaux, je construis le lecteur.")

if __name__ == "__main__":
    main()
