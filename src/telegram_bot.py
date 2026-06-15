"""
telegram_bot.py - Bot Telegram admin pour Bonaza
=================================================
Tourne dans un container dedie. Repond aux commandes envoyees par
l'utilisateur autorise (TELEGRAM_CHAT_ID).

Sources de donnees :
  - /app/data/bonaza.db          (SQLite, signaux + trades)
  - /app/logs/bonaza_YYYY-MM-DD.log  (logs Bonaza JSON)
  - /app/data/status.json         (snapshot ecrit toutes les 30s par main.py)

Commandes :
  /status      etat global (engines, agent IA, RM)
  /signals [N] N derniers signaux (default 10)
  /trades [N]  N derniers trades fermes (default 10)
  /logs [N]    N dernieres lignes log (default 20)
  /agent       detail agent IA
  /help        liste des commandes

Securite : seul TELEGRAM_CHAT_ID est autorise. Toute autre commande -> ignoree.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Charge .env si dispo (test local)
sys.path.insert(0, os.path.dirname(__file__))
try:
    from config import config  # noqa
except Exception:
    pass

TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH    = Path(os.getenv("BONAZA_DB_PATH", "/app/data/bonaza.db"))
LOG_DIR    = Path(os.getenv("BONAZA_LOG_PATH", "/app/logs"))
STATUS         = Path("/app/data/status.json")
MODEL_FILE     = Path("/app/data/current_model.txt")
OVERRIDE_FILE  = Path("/app/data/boost_override.json")  # lu par boost_manager
KILL_FILE      = Path("/app/data/kill_switch.flag")     # lu par order_executor

# Modeles Claude autorises pour hot-swap (cf doc Anthropic).
# Haiku 4.5 BANNI le 2026-05-25 : raisonnement trop faible sur contexte
# horaire/setup XAUUSD, a genere des pertes en direction inverse de la
# tendance jour. Sonnet 4.6 est le minimum acceptable.
ALLOWED_MODELS = {
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}

if not TOKEN or not CHAT_ID:
    print("[BOT] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID absent, exit.")
    sys.exit(0)

API = f"https://api.telegram.org/bot{TOKEN}"
print(f"[BOT] Demarrage. Chat autorise = {CHAT_ID}")


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def send(text: str, parse_mode: str = "Markdown") -> bool:
    """Envoie un message au chat autorise."""
    try:
        payload = {
            "chat_id": CHAT_ID,
            "text":    text[:4000],
        }
        # Telegram rejette parse_mode=null : on omet la cle si non definie.
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = requests.post(f"{API}/sendMessage", json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[BOT] send error: {e}")
        return False


def read_status() -> dict:
    if not STATUS.exists():
        return {}
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return {}


# -----------------------------------------------------------------------
# Commandes
# -----------------------------------------------------------------------

def cmd_help(args) -> str:
    return (
        "*Bonaza Admin Bot*\n\n"
        "`/status`         Etat global du systeme\n"
        "`/signals N`      N derniers signaux (default 10)\n"
        "`/trades N`       N derniers trades fermes (default 10)\n"
        "`/agent`          Detail agent IA Claude\n"
        "`/model`          Modele courant + options\n"
        "`/model sonnet`   Hot-swap -> Sonnet 4.6 (defaut)\n"
        "`/model opus`     Hot-swap -> Opus 4.7   (annonces critiques)\n"
        "_Haiku 4.5 BANNI (raisonnement insuffisant pour XAUUSD)._\n"
        "`/calendar [N]`   N prochains events HIGH (default 7)\n"
        "`/boost`          Statut boost auto (modele actuel + raison)\n"
        "`/boost opus 60`  Forcer Opus pendant 60 min (override manuel)\n"
        "`/boost haiku 60` Forcer Haiku pendant 60 min\n"
        "`/boost off`      Lever l'override, retour automatique\n"
        "`/engines`        Etat des moteurs (Bv3 / Agent IA)\n"
        "`/engines bv3`    Active Bv3 seul (IA off)\n"
        "`/engines ai`     Active IA seule (Bv3 off)\n"
        "`/engines both`   Active les deux (defaut)\n"
        "`/engines off`    Desactive les deux moteurs\n"
        "`/kill [raison]`  🛑 STOP d'urgence : refuse tout nouvel ordre\n"
        "`/unkill`         Lever le kill switch, retour normal\n"
        "`/tg closeall`    Copieur : fermer toutes les positions TG (confirmation)\n"
        "`/tg ignore`      Copieur : ignorer l'instruction ambigue\n"
        "`/jour [date]`    Bilan du jour 100% clotures IG (source broker)\n"
        "`/logs N`         N dernieres lignes log (default 20)\n"
        "`/help`           Cette aide\n"
        "\n_Reponses limitees aux commandes du chat autorise._"
    )


def cmd_calendar(args) -> str:
    """Affiche les N prochains events economiques HIGH."""
    n = int(args[0]) if args and args[0].isdigit() else 7
    n = min(max(n, 1), 20)
    try:
        from economic_calendar import EconomicCalendar
        import asyncio as _asyncio
        cal = EconomicCalendar()
        _asyncio.run(cal.refresh())     # utilise le cache si frais
    except Exception as e:
        return f"_Calendrier indispo : {e}_"
    evs = cal.next_events(n=n, only_high=True)
    if not evs:
        return "_Aucun event HIGH a venir cette semaine._"
    out = [f"*Prochains {len(evs)} events HIGH* "
           f"(filtre {','.join(sorted(cal.countries))})"]
    for e in evs:
        out.append(f"  • `{e.dt_utc.strftime('%a %d/%m %H:%M UTC')}` "
                   f"*[{e.country}]* {e.title}")
    out.append(f"\n_Source : ForexFactory (cache 12h)_")
    return "\n".join(out)


def cmd_boost(args) -> str:
    """Statut boost ou override manuel."""
    s = read_status()
    boost = (s.get("boost") or {}) if s else {}

    # Sous-commande : off / opus / haiku / sonnet
    if args:
        sub = args[0].lower()
        # off / clear / auto
        if sub in ("off", "clear", "auto"):
            try:
                if OVERRIDE_FILE.exists():
                    OVERRIDE_FILE.unlink()
                return "✅ Override leve. Retour mode automatique au prochain tick (<60s)."
            except Exception as e:
                return f"❌ Erreur : {e}"
        # force opus|haiku [duree_min]
        if sub in ALLOWED_MODELS:
            model = ALLOWED_MODELS[sub]
            duration = 60
            if len(args) >= 2 and args[1].isdigit():
                duration = min(max(int(args[1]), 5), 1440)
            until = datetime.now(tz=timezone.utc) + timedelta(minutes=duration)
            try:
                OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
                OVERRIDE_FILE.write_text(json.dumps({
                    "target":    model,
                    "until_utc": until.isoformat(),
                    "set_by":    "telegram",
                }, indent=2), encoding="utf-8")
                return (f"🎛 Override actif : `{model}` pendant {duration} min "
                        f"(jusqu'a `{until.strftime('%H:%M UTC')}`).\n"
                        f"_Effectif au prochain tick boost_manager (<60s)._")
            except Exception as e:
                return f"❌ Erreur ecriture : {e}"
        return f"Sous-commande inconnue : `{sub}`. Voir `/help`."

    # Sans argument : statut
    if not boost:
        return "_Section boost absente du status.json (boost_manager pas encore tick)._"
    cur = boost.get("current_model", "?")
    is_b = boost.get("is_boosted", False)
    reason = boost.get("last_reason", "")
    sources = boost.get("sources", [])
    nxt_ev = boost.get("next_event")
    nxt_start = boost.get("next_window_start_utc")
    ovr = boost.get("override_active", False)

    icon = "🚀" if is_b else "💤"
    out = [f"{icon} *Boost manager*"]
    out.append(f"Modele actif : `{cur}`")
    out.append(f"Mode boost : `{'OUI' if is_b else 'NON'}`")
    if sources:
        out.append(f"Sources : `{', '.join(sources)}`")
    if reason:
        out.append(f"Raison : _{reason[:200]}_")
    if ovr:
        out.append(f"\n🎛 *Override manuel actif*")
        out.append(f"  cible : `{boost.get('override_target','?')}`")
        out.append(f"  jusqu'a : `{(boost.get('override_until') or '?')[:19]}`")
    if nxt_ev and nxt_start:
        out.append(f"\n*Prochaine fenetre auto* : `{nxt_start[:16]}` {nxt_ev}")
    return "\n".join(out)


def cmd_model(args) -> str:
    """Affiche ou modifie le modele Claude utilise par l'agent IA."""
    # Sans argument : afficher l'etat actuel
    if not args:
        if MODEL_FILE.exists():
            try:
                current = MODEL_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                current = "?"
        else:
            current = os.getenv("AI_AGENT_MODEL", "(default config)")
        options = "\n".join(f"  `/model {k}` → `{v}`"
                            for k, v in ALLOWED_MODELS.items())
        return (
            f"*Modele actif* : `{current}`\n\n"
            f"*Pour switcher* (hot-swap, ~60s d'effet) :\n{options}\n"
            f"\n_Le bot ecrit /app/data/current_model.txt, "
            f"l'agent IA le lit a chaque tick. Aucun restart._"
        )
    # Avec argument : tenter le switch
    key = args[0].lower()
    if key not in ALLOWED_MODELS:
        return (f"Modele inconnu : `{key}`.\n"
                f"Options : `{', '.join(ALLOWED_MODELS.keys())}`")
    model = ALLOWED_MODELS[key]
    try:
        MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODEL_FILE.write_text(model + "\n", encoding="utf-8")
        return (f"✅ Modele -> `{model}`\n"
                f"Effectif au prochain tick agent IA (max 60s).\n"
                f"_Aucun restart necessaire._")
    except Exception as e:
        return f"❌ Erreur ecriture : {e}"


def cmd_status(args) -> str:
    s = read_status()
    if not s:
        return "_status.json absent (bonaza_main n'a pas encore demarre)_"
    ts = s.get("ts", "?")
    lines = [f"*Status Bonaza* (`{ts[:19]}`)"]
    # Engines
    for name, st in (s.get("engines") or {}).items():
        lines.append(f"\n*{name}*")
        lines.append(f"  bars: `{st.get('bar_count')}`  warmup: `{st.get('warmup_pct'):.0f}%`")
        lines.append(f"  signals: `{st.get('signals_emitted')}` "
                     f"(oos:{st.get('signals_blocked_oos',0)} "
                     f"adx:{st.get('signals_blocked_adx',0)})")
    # Compte LIVE : PnL du jour reconstruit depuis les clotures IG (OPU)
    try:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        ig = _ig_closes()
        sizes = {}
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            for did, sz in conn.execute("SELECT position_id, size FROM trades"):
                sizes[did] = float(sz or 0.5)
            conn.close()
        todays = [(did, o) for did, o in ig.items() if o["ts"][:10] == today]
        pnl_ig = sum(_ig_pnl(o, sizes.get(did, 0.5)) for did, o in todays)
        lines.append("\n*Compte LIVE (clotures IG)*")
        lines.append(f"  PnL jour : `{pnl_ig:+.2f} EUR` sur `{len(todays)}` cloture(s)")
        lines.append("  _detail : /jour_")
    except Exception as e:
        lines.append(f"\n_Clotures IG indisponibles : {e}_")
    # Risk managers (XAUUSD principal) - compteurs INTERNES (paper, remis a
    # zero a chaque restart ; le copieur TG ne passe pas par ce RM)
    rms = s.get("rm_metrics") or {}
    if rms:
        lines.append("\n*Risk Manager interne (XAUUSD)* _(hors copieur TG)_")
        rm = rms.get("XAUUSD", {})
        lines.append(f"  equity: `{rm.get('equity', 0):.2f} EUR`")
        lines.append(f"  realized P&L: `{rm.get('realized_pnl', 0):+.2f}`")
        lines.append(f"  daily DD: `{rm.get('daily_dd_pct', 0):.2f}%` "
                     f"/ limite `{rm.get('dd_limit_pct', 0):.2f}%`")
        lines.append(f"  open: `{rm.get('open_positions', 0)}/{rm.get('max_open_positions','?')}`")
        lines.append(f"  closed today: `{rm.get('closed_today', 0)}` "
                     f"(W:{rm.get('wins_today',0)} L:{rm.get('losses_today',0)})")
        if rm.get("kill_switch"):
            lines.append(f"  🛑 *KILL SWITCH ACTIVE* ({rm.get('kill_reason')})")
    # AI agent
    ai = s.get("ai_agent")
    if ai:
        lines.append("\n*Agent IA*")
        lines.append(f"  appels Claude: `{ai.get('calls_made',0)}` "
                     f"(emis:{ai.get('signals_emitted',0)} skip:{ai.get('signals_skipped',0)})")
        lines.append(f"  derniere decision: `{ai.get('last_decision','-')}`")
        lines.append(f"  marche ouvert: `{ai.get('market_open', '?')}`")
    return "\n".join(lines)


def cmd_signals(args) -> str:
    n = int(args[0]) if args and args[0].isdigit() else 10
    n = min(max(n, 1), 50)
    if not DB_PATH.exists():
        return "_DB absente_"
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT ts, instrument, direction, entry, stop_loss, take_profit, "
            "rr_ratio, mode FROM signals ORDER BY id DESC LIMIT ?", (n,)
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return f"_DB erreur : {e}_"
    if not rows:
        return "_Aucun signal_"
    out = [f"*Derniers {len(rows)} signaux*"]
    for ts, inst, d, e, sl, tp, rr, mode in rows:
        emoji = "🟢" if d == "LONG" else "🔴"
        out.append(f"{emoji} `{ts[11:19]}` {inst} *{d}* E={e} SL={sl} TP={tp} "
                   f"RR={rr:.2f} [{mode}]")
    return "\n".join(out)


# ---- Clotures IG (source de verite : flux OPU collecte par main.py) ----
TRADE_EVENTS = Path(os.getenv("BONAZA_TRADE_EVENTS", "/app/data/trade_events.jsonl"))

def _ig_closes(max_lines=4000) -> dict:
    """dealId -> cloture IG reelle (OPU status=DELETED) : open/close/dir/ts.
    Lit la fin du fichier uniquement (le fichier grossit)."""
    out = {}
    if not TRADE_EVENTS.exists():
        return out
    try:
        from collections import deque
        lines = deque(open(TRADE_EVENTS, encoding="utf-8", errors="replace"),
                      maxlen=max_lines)
    except Exception:
        return out
    for line in lines:
        try:
            e = json.loads(line)
            raw = e.get("opu")
            if not raw:
                continue
            d = json.loads(raw)
            if d.get("status") != "DELETED":
                continue
            out[d.get("dealId", "")] = {
                "close": float(d.get("level") or 0.0),
                "open":  float(d.get("openLevel") or 0.0),
                "dir":   d.get("direction", ""),          # BUY/SELL
                "ts":    str(d.get("timestamp", "")),      # heure IG reelle
                "stop":  float(d["stopLevel"]) if d.get("stopLevel") else None,
                "limit": float(d["limitLevel"]) if d.get("limitLevel") else None,
            }
        except Exception:
            continue
    return out


def _ig_pnl(o: dict, size: float) -> float:
    sign = -1.0 if o["dir"] == "SELL" else 1.0
    return (o["close"] - o["open"]) * sign * size


def _ig_mechanism(o: dict, db_reason: str = "") -> str:
    """Mecanisme reel de cloture, deduit des niveaux IG au moment du DELETE :
    cloture ~limite => TP ; ~stop => SL, sous-classe selon la position du stop
    (zone de perte = SL, ~entree = BE, zone de gain = SL cliquet).
    Les fermetures directes par le moteur portent deja leur raison (TG_LOCK...)."""
    r = str(db_reason or "")
    if "TG_LOCK" in r:
        return "🔒cliquet direct"
    if "TG_PARTIAL" in r:
        return "📡groupe (TP partiel)"
    if "TG_BREAKEVEN" in r:
        return "⚖BE moteur"
    if "MANUAL_CONFIRM" in r:
        return "👤manuel (/tg closeall)"
    if r.startswith("TG_"):
        return "📡groupe"
    close, stop, lim, op = o["close"], o.get("stop"), o.get("limit"), o["open"]
    short = o["dir"] == "SELL"
    cands = []
    if lim:
        cands.append(("TP", abs(close - lim)))
    if stop:
        cands.append(("STOP", abs(close - stop)))
    if not cands:
        return "❔"
    kind, dist = min(cands, key=lambda x: x[1])
    if dist > 1.5:
        return "❔manuel/autre"
    if kind == "TP":
        return "🎯TP"
    profit_side = (op - stop) if short else (stop - op)   # >0 : stop en zone de gain
    if profit_side > 0.3:
        return "🔒SL cliquet"
    if profit_side >= -0.3:
        return "⚖SL breakeven"
    return "🛑SL"


def cmd_trades(args) -> str:
    n = int(args[0]) if args and args[0].isdigit() else 10
    n = min(max(n, 1), 30)
    if not DB_PATH.exists():
        return "_DB absente_"
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT ts_close, direction, size, entry_price, exit_price, "
            "pnl_eur, exit_reason, position_id FROM trades WHERE status='CLOSED' "
            "ORDER BY id DESC LIMIT ?", (n,)
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return f"_DB erreur : {e}_"
    if not rows:
        return "_Aucun trade ferme_"
    ig = _ig_closes()
    out = [f"*Derniers {len(rows)} trades* (✓ = confirme IG, heure IG)"]
    total_pnl = 0.0
    for ts, d, sz, entry, exit_p, pnl, reason, deal_id in rows:
        o = ig.get(deal_id or "")
        src, mech = "", str(reason or "")
        if o:
            pnl_ig = _ig_pnl(o, float(sz or 0.5))
            db_stale = (pnl is None) or (abs(float(pnl or 0)) < 0.005
                                         and abs(float(exit_p or 0) - float(entry or 0)) < 0.005)
            if db_stale or abs(float(pnl or 0) - pnl_ig) > 0.01:
                # DB pas encore consolidee (close_resolver) -> on affiche IG
                exit_p, pnl, src = o["close"], pnl_ig, " 📡IG"
            else:
                src = " ✓"
            ts = o["ts"] or ts          # heure de cloture IG reelle
            mech = _ig_mechanism(o, reason)
        emoji = "💰" if (pnl or 0) > 0 else "🩸" if (pnl or 0) < 0 else "⚪"
        try: total_pnl += float(pnl or 0)
        except: pass
        out.append(f"{emoji} `{(ts or '')[11:19]}` {d} {sz}L "
                   f"{entry}->{exit_p} = `{(pnl or 0):+.2f}`{src} {mech}")
    out.append(f"\n*Total : `{total_pnl:+.2f} EUR`*")
    return "\n".join(out)


def cmd_jour(args) -> str:
    """Bilan du jour reconstruit DIRECTEMENT depuis les clotures IG (flux OPU),
    independant de la DB et du close_resolver. /jour [AAAA-MM-JJ]"""
    day = args[0] if args and len(args[0]) == 10 else \
        datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    ig = _ig_closes()
    # tailles + raisons depuis la DB (l'OPU de cloture porte size=0)
    sizes, reasons = {}, {}
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(DB_PATH)
            for did, sz, rs in conn.execute(
                    "SELECT position_id, size, exit_reason FROM trades"):
                sizes[did] = float(sz or 0.5)
                reasons[did] = rs or ""
            conn.close()
        except Exception:
            pass
    closes = [(did, o) for did, o in ig.items() if o["ts"][:10] == day]
    if not closes:
        return f"_Aucune cloture IG le {day}_"
    closes.sort(key=lambda x: x[1]["ts"])
    out = [f"*Bilan {day} — clotures IG (source broker)*"]
    total = wins = losses = 0.0
    for did, o in closes:
        sz = sizes.get(did, 0.5)
        pnl = _ig_pnl(o, sz)
        total += pnl
        wins, losses = wins + (1 if pnl > 0 else 0), losses + (1 if pnl < 0 else 0)
        emoji = "💰" if pnl > 0 else "🩸" if pnl < 0 else "⚪"
        sens = "SHORT" if o["dir"] == "SELL" else "LONG"
        out.append(f"{emoji} `{o['ts'][11:19]}` {sens} {sz}L "
                   f"{o['open']:.2f}->{o['close']:.2f} = `{pnl:+.2f}` "
                   f"{_ig_mechanism(o, reasons.get(did, ''))}")
    wr = 100.0 * wins / max(1, wins + losses)
    out.append(f"\n*Total IG : `{total:+.2f} EUR`* "
               f"({int(wins)}W/{int(losses)}L, WR {wr:.0f}%)")
    # comparaison avec la DB (detection de retard du close_resolver)
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_eur),0), COUNT(*) FROM trades "
                "WHERE status='CLOSED' AND substr(ts_close,1,10)=?", (day,)).fetchone()
            conn.close()
            ecart = total - float(row[0] or 0)
            flag = "✓ concordant" if abs(ecart) < 0.05 else \
                f"⚠ ecart `{ecart:+.2f}` (close_resolver en retard ?)"
            out.append(f"_DB : {row[1]} trade(s), `{float(row[0] or 0):+.2f} EUR` — {flag}_")
        except Exception:
            pass
    return "\n".join(out)


def cmd_agent(args) -> str:
    s = read_status()
    ai = (s.get("ai_agent") or {}) if s else {}
    if not ai:
        return "_Agent IA non actif ou status absent_"
    lines = ["*Agent IA Claude*"]
    for k, v in ai.items():
        lines.append(f"`{k}` : {v}")
    return "\n".join(lines)


def cmd_logs(args) -> str:
    n = int(args[0]) if args and args[0].isdigit() else 20
    n = min(max(n, 1), 50)
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    log = LOG_DIR / f"bonaza_{today}.log"
    if not log.exists():
        return "_Pas de log aujourd'hui_"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"_log lecture erreur : {e}_"
    out = []
    for line in lines[-(n * 2):]:
        try:
            j = json.loads(line)
            t = j['record']['time']['repr'][11:19]
            lvl = j['record']['level']['name'][:4]
            msg = j['record']['message'][:80]
            out.append(f"`{t}` [{lvl}] {msg}")
        except Exception:
            continue
    return "*Logs recents*\n" + "\n".join(out[-n:]) if out else "_Aucune entree parsable_"


def cmd_engines(args) -> str:
    """Toggle Bv3 / Agent IA runtime via data/active_engines.json.
    Usage : /engines              -> affiche etat
            /engines bv3          -> Bv3 seul (ai off)
            /engines ai           -> Agent IA seul (bv3 off)
            /engines both         -> les deux ON
            /engines off          -> aucun (= equivalent kill sans bloquer ordres existants)
    """
    try:
        from engines_control import get_state, set_state
    except Exception as e:
        return f"_engines_control indisponible : {e}_"
    if not args:
        s = get_state()
        bv3 = "🟢" if s["bv3"] else "🔴"
        ai  = "🟢" if s["ai"]  else "🔴"
        return (f"*Moteurs actifs*\n"
                f"  {bv3} Bv3 : `{s['bv3']}`\n"
                f"  {ai} Agent IA : `{s['ai']}`\n\n"
                f"Sous-commandes : `bv3` | `ai` | `both` | `off`")
    sub = args[0].lower()
    presets = {
        "bv3":  (True,  False),
        "ai":   (False, True),
        "both": (True,  True),
        "off":  (False, False),
        "all":  (True,  True),
        "none": (False, False),
    }
    if sub not in presets:
        return f"Sous-commande inconnue : `{sub}`. Options : `bv3` | `ai` | `both` | `off`"
    bv3, ai = presets[sub]
    s = set_state(bv3=bv3, ai=ai)
    bv3i = "🟢" if s["bv3"] else "🔴"
    aii  = "🟢" if s["ai"]  else "🔴"
    return (f"✅ Moteurs MAJ :\n"
            f"  {bv3i} Bv3 : `{s['bv3']}`\n"
            f"  {aii} Agent IA : `{s['ai']}`\n"
            f"_Effet immediat (cache 5s côté engines)._")


def cmd_kill(args) -> str:
    """STOP d'urgence : refuse tout nouvel ordre. Les positions ouvertes
    restent ouvertes (SL/TP existants continuent de proteger).
    Format flag : 'YYYY-MM-DD HH:MM UTC | raison'."""
    reason = " ".join(args) if args else "manuel"
    now    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text(f"{now} | {reason[:200]}", encoding="utf-8")
    except Exception as e:
        return f"❌ Erreur ecriture : {e}"
    return (f"🛑 *KILL SWITCH ACTIF*\n"
            f"Raison : `{reason[:100]}`\n"
            f"Heure : `{now}`\n\n"
            f"⚠ Plus aucun nouvel ordre ne sera passe.\n"
            f"_Les positions deja ouvertes restent ouvertes (SL/TP en place)._\n\n"
            f"Pour reactiver : `/unkill`")


def cmd_unkill(args) -> str:
    if not KILL_FILE.exists():
        return "_Kill switch deja inactif._"
    try:
        KILL_FILE.unlink()
    except Exception as e:
        return f"❌ Erreur suppression : {e}"
    return ("✅ *Kill switch leve*\n"
            "Les nouveaux ordres sont a nouveau autorises (sous reserve des autres verrous).")


TG_CLOSEALL_FLAG = Path("/app/data/tg_closeall.flag")   # lu par telegram_reader (tick 2s)

def cmd_tg(args) -> str:
    """Confirmation operateur pour le copieur TRADAMAX (instructions ambigues).
    Usage : /tg closeall  -> ferme TOUTES les positions du copieur (TG_*)
            /tg ignore    -> ignore l'instruction ambigue (aucune action)"""
    sub = args[0].lower() if args else ""
    if sub == "closeall":
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        try:
            TG_CLOSEALL_FLAG.parent.mkdir(parents=True, exist_ok=True)
            TG_CLOSEALL_FLAG.write_text(now, encoding="utf-8")
        except Exception as e:
            return f"❌ Erreur ecriture flag : {e}"
        return ("🟠 *Copieur* : fermeture de TOUTES les positions TG demandee.\n"
                "_Prise en compte par le copieur sous ~2 s._")
    if sub == "ignore":
        return "✅ Instruction ambigue ignoree. Aucune action sur les positions."
    return ("Usage :\n"
            "`/tg closeall`  ferme toutes les positions du copieur\n"
            "`/tg ignore`    ignore l'instruction ambigue")


COMMANDS = {
    "status":   cmd_status,
    "signals":  cmd_signals,
    "trades":   cmd_trades,
    "agent":    cmd_agent,
    "model":    cmd_model,
    "calendar": cmd_calendar,
    "boost":    cmd_boost,
    "engines":  cmd_engines,
    "kill":     cmd_kill,
    "unkill":   cmd_unkill,
    "tg":       cmd_tg,
    "jour":     cmd_jour,
    "logs":     cmd_logs,
    "help":     cmd_help,
    "start":    cmd_help,   # /start = alias /help (Telegram default)
}


# -----------------------------------------------------------------------
# Boucle principale (long polling getUpdates)
# -----------------------------------------------------------------------

def handle_update(upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != CHAT_ID:
        print(f"[BOT] msg ignoré (chat {chat_id} != autorise {CHAT_ID})")
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text[1:].split()
    cmd_name = parts[0].split("@")[0].lower()
    args = parts[1:]
    fn = COMMANDS.get(cmd_name)
    print(f"[BOT] /{cmd_name} {args}")
    if not fn:
        send(f"Commande inconnue `/{cmd_name}`. Tape `/help`")
        return
    try:
        reply = fn(args)
        send(reply)
    except Exception as e:
        send(f"Erreur execution `/{cmd_name}` : {e}")


def main() -> None:
    # Annonce demarrage
    send("🤖 *Bonaza Admin Bot en ligne*\nTape `/help` pour les commandes.")
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40)
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                handle_update(upd)
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"[BOT] poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
