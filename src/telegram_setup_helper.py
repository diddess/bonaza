"""
Helper pour trouver le bon TELEGRAM_CHAT_ID.
Erreur typique : on met l'ID du bot au lieu de son ID utilisateur.

Lancer ce script :
  - Affiche les infos du bot (getMe) pour valider le token
  - Affiche tous les chat_id qui ont parle au bot recemment
  - Suggere lequel mettre dans TELEGRAM_CHAT_ID
"""
import os
import sys
import json
import requests

sys.path.insert(0, os.path.dirname(__file__))
from config import config  # noqa: charge .env

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TOKEN:
    print("TELEGRAM_BOT_TOKEN absent dans .env")
    sys.exit(1)

API = f"https://api.telegram.org/bot{TOKEN}"

# 1) getMe : verifie que le token est valide + recupere l'ID du bot
print("=" * 60)
print("  1. INFOS DU BOT (getMe)")
print("=" * 60)
try:
    r = requests.get(f"{API}/getMe", timeout=10).json()
    if not r.get("ok"):
        print(f"Token invalide ou bot supprime : {r}")
        sys.exit(1)
    bot = r["result"]
    bot_id = bot.get("id")
    print(f"  Bot ID       : {bot_id}     <-- NE PAS UTILISER comme chat_id")
    print(f"  Bot username : @{bot.get('username')}")
    print(f"  Bot name     : {bot.get('first_name')}")
except Exception as e:
    print(f"Erreur getMe : {e}")
    sys.exit(1)

# 2) getUpdates : recupere les conversations
print()
print("=" * 60)
print("  2. CONVERSATIONS RECENTES (getUpdates)")
print("=" * 60)
try:
    r = requests.get(f"{API}/getUpdates", timeout=10).json()
except Exception as e:
    print(f"Erreur getUpdates : {e}")
    sys.exit(1)

updates = r.get("result", [])
if not updates:
    print()
    print("  Aucune conversation trouvee !")
    print()
    print("  Pour que getUpdates renvoie ton chat_id, il faut :")
    print(f"   1. Sur Telegram, cherche @{bot.get('username')}")
    print( "   2. Clique 'Demarrer' ou envoie /start (ou n'importe quel msg)")
    print( "   3. Relance ce script")
    print()
    print("  Note : getUpdates ne renvoie que les messages des dernieres 24h.")
    sys.exit(2)

chats = {}
for upd in updates:
    msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post")
    if not msg:
        continue
    chat   = msg.get("chat", {})
    sender = msg.get("from", {})
    chat_id   = chat.get("id")
    chat_type = chat.get("type")     # private, group, supergroup, channel
    name      = (chat.get("first_name", "") + " " + chat.get("last_name", "")).strip()
    if not name:
        name = chat.get("title") or chat.get("username") or "?"
    is_bot = sender.get("is_bot", False)
    if chat_id and not is_bot:
        chats[chat_id] = (chat_type, name)

if not chats:
    print("  Aucun chat utilisateur trouve (que des messages de bots).")
    print(f"  Va sur Telegram, parle a @{bot.get('username')} et relance.")
    sys.exit(2)

print()
print(f"  {len(chats)} chat(s) detecte(s) :")
print()
for cid, (ctype, name) in chats.items():
    flag = "<-- UTILISE CE chat_id" if ctype == "private" else ""
    print(f"   chat_id = {cid}    type={ctype}    name='{name}'  {flag}")

# 3) Suggestion
print()
print("=" * 60)
print("  3. ACTION")
print("=" * 60)
# Privilegier un chat private si dispo
private = [(cid, n) for cid, (t, n) in chats.items() if t == "private"]
if private:
    cid, name = private[0]
    print(f"  Recommande : TELEGRAM_CHAT_ID={cid}    ({name})")
    print()
    print("  Edite C:\\Claude\\bonaza\\.env et remplace TELEGRAM_CHAT_ID par cette valeur,")
    print("  puis relance : python src\\telegram_alerts.py")
else:
    print("  Pas de chat 'private' detecte. Pour recevoir les alertes personnellement,")
    print(f"  va sur Telegram, cherche @{bot.get('username')}, envoie /start, relance.")
