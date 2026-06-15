#!/bin/bash
# Verification one-shot a la reouverture XAUUSD (dim 22h UTC) sur VPS2.
# Verifie : conteneur main, etat marche TRADEABLE, feed vivant, copieur connecte,
# activite TG depuis 22h. Envoie un recap Telegram. S'auto-supprime du crontab.
set -uo pipefail
cd /opt/bonaza
LOG=logs/bonaza_$(date -u +%F).log
TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')
CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')

# 1. conteneur main
MAIN=$(docker ps --filter name=bonaza_main --format '{{.Status}}' | head -1)
[ -n "$MAIN" ] && MAIN_OK="OK ($MAIN)" || MAIN_OK="ARRETE !"

# 2. etat marche XAUUSD (dernier state du gold epic dans le log du jour)
GOLD=$(grep -a 'CS.D.CFEGOLD.CFE.IP state' "$LOG" 2>/dev/null | tail -1 | grep -oE '\-> [A-Z_]+' | tail -1)
[ -z "$GOLD" ] && GOLD="(inconnu)"

# 3. feed vivant : fraicheur status.json (< 5 min)
STTS=$(python3 -c "import json,datetime;s=json.load(open('data/status.json'));t=datetime.datetime.fromisoformat(s['ts']);age=(datetime.datetime.now(datetime.timezone.utc)-t).total_seconds();print('frais (%ds)'%age if age<300 else 'PERIME (%ds)'%age)" 2>/dev/null || echo "illisible")

# 4. copieur + incidents SUR LA DERNIERE HEURE (fenetre reouverture, pas tout le jour)
COP=$(grep -a 'Copieur connecte' "$LOG" 2>/dev/null | tail -1 | grep -oE 'groupe -?[0-9]+' || echo "non connecte ?")
read ERR TGN <<<"$(python3 - "$LOG" <<'PY'
import json,sys,datetime,re
log=sys.argv[1]; now=datetime.datetime.now(datetime.timezone.utc).timestamp()
err=tg=0
try:
    for line in open(log,encoding='utf-8',errors='replace'):
        try: r=json.loads(line)['record']
        except: continue
        if now-r['time']['timestamp']>3900: continue   # ~65 min
        m=r['message']
        if re.search('oauth-token-invalid|Echec AUTH|connexion perdue',m): err+=1
        if '[TG] message classe' in m: tg+=1
except: pass
print(err,tg)
PY
)"
[ -z "$ERR" ] && ERR=0; [ -z "$TGN" ] && TGN=0

MSG=$(printf '🔔 Reouverture marche — VPS2\n----------------------------------\nmain      : %s\nXAUUSD    : %s\nfeed      : %s\ncopieur   : %s\nmsgs TG (60 min) : %s\nerreurs auth/feed (60 min) : %s\n\n(verif auto a la reouverture, %s UTC)' \
  "$MAIN_OK" "$GOLD" "$STTS" "$COP" "$TGN" "$ERR" "$(date -u +%H:%M)")

curl -s -o /dev/null --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=${MSG}" \
  "https://api.telegram.org/bot${TOKEN}/sendMessage"

# auto-suppression du cron (one-shot)
crontab -l 2>/dev/null | grep -v '_reopen_check.sh' | crontab - 2>/dev/null
echo "$(date -u) reopen_check envoye | XAUUSD=$GOLD feed=$STTS err=$ERR"
