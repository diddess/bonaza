"""SHADOW 'Groupe Prive' (-1001553010649) : copie PAPIER, AUCUN ordre reel.
Objectif : evaluer le groupe sur plusieurs seances (y compris jours de hausse)
avant toute decision. ENTRY ferme -> 4 jambes papier (TP1/TP2/TP3/RUNNER).
On trace MFE/MAE en continu pour pouvoir chiffrer ensuite n'importe quelle
regle de sortie (dont le verrou +6/+5 du copieur, calcule virtuellement).
"""
import os, json, re, asyncio
from datetime import datetime, timezone
from loguru import logger

SHADOW_GROUP_ID = -1001553010649
SIZE       = 0.5      # lot papier par jambe (comparable au copieur TRADAMAX)
TICK_SEC   = 2.0
STATE_F    = "/app/data/shadow_gp_state.json"
TRADES_F   = "/app/data/shadow_gp_trades.jsonl"
MSGS_F     = "/app/data/shadow_gp_messages.jsonl"
LOCK_ARM, LOCK_EXIT = 6.0, 5.0   # verrou VIRTUEL (comparaison, ne ferme rien)

NUM = r"(\d+(?:\.\d+)?)"
RX_ZONE = re.compile(r"Zone d.entr[ée]e\s*:\s*" + NUM + r"\s*-\s*" + NUM, re.I)
RX_TP   = {k: re.compile(r"TP%d\s*:\s*" % k + NUM) for k in (1, 2, 3)}
RX_SL   = re.compile(r"SL\s*:?\s*" + NUM)
RX_BE   = re.compile(r"(?:mettez|on met|mets)[^\n]*\bBE\b"
                     r"|(?:^|\n)\s*BE\s*(?:$|\n)|J.ai\s+(?:ça\s+à\s+)?BE", re.I)
PREV_MARKERS = ("PRÉVISION", "PREVISION", "pas une exécution", "pas une execution")


def parse_gp(text: str):
    """Classe un message du Groupe Prive. PREVISION loggee mais PAS tradee."""
    t = text or ""
    is_setup = ("XAUUSD" in t) and RX_ZONE.search(t)
    if is_setup:
        z = RX_ZONE.search(t)
        sig = {
            "zone_lo": min(float(z.group(1)), float(z.group(2))),
            "zone_hi": max(float(z.group(1)), float(z.group(2))),
            "direction": "SHORT" if "SELL" in t.upper() else "LONG",
            "sl": float(RX_SL.search(t).group(1)) if RX_SL.search(t) else 0.0,
        }
        for k, rx in RX_TP.items():
            m = rx.search(t)
            sig["tp%d" % k] = float(m.group(1)) if m else 0.0
        if any(m in t for m in PREV_MARKERS):
            sig["type"] = "PREVISION"
        else:
            sig["type"] = "ENTRY"
        return sig
    if RX_BE.search(t):                # AVANT le test stop ("ceux qui n'ont pas
        return {"type": "BREAKEVEN"}   #  ete stoppes, mettez a BE" = BE)
    if "stopp" in t.lower():           # "Nous avons ete stoppe !"
        return {"type": "STOP"}
    return None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _append(path, obj):
    try:
        with open(path, "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("[SHADOW] append %s : %s" % (path, e))


class ShadowTracker:
    """Suit les jambes papier au prix live. Ne passe JAMAIS d'ordre."""

    def __init__(self, executor, instrument="XAUUSD"):
        self.executor = executor
        self.instrument = instrument
        self._next_id = 1
        self.legs = []          # jambes papier (OPEN)
        self._load_state()

    # ---------------- etat persistant (survit aux restarts) ----------------
    def _load_state(self):
        try:
            if os.path.exists(STATE_F):
                st = json.load(open(STATE_F))
                self.legs = st.get("legs", [])
                self._next_id = st.get("next_id", 1)
                if self.legs:
                    logger.info("[SHADOW] etat recharge : %d jambe(s) papier ouverte(s)"
                                % len(self.legs))
        except Exception as e:
            logger.warning("[SHADOW] load_state : %s" % e)

    def _save_state(self):
        try:
            json.dump({"legs": self.legs, "next_id": self._next_id},
                      open(STATE_F, "w"), ensure_ascii=False)
        except Exception as e:
            logger.warning("[SHADOW] save_state : %s" % e)

    # ---------------- helpers ----------------
    def _price(self):
        feed = getattr(self.executor, "_feed", None)
        if feed is None:
            return None
        try:
            from instruments import INSTRUMENTS
            epic = INSTRUMENTS[self.instrument].epic
            p = feed.get_price(epic)
            return float(p) if p and p > 0 else None
        except Exception:
            return None

    def _alert(self, txt):
        try:
            from telegram_alerts import alerts
            alerts().send("👁 SHADOW GP : " + txt, parse_mode=None, silent=True)
        except Exception:
            pass

    def _close_leg(self, leg, exit_price, reason):
        long = leg["direction"] == "LONG"
        pts = (exit_price - leg["entry"]) if long else (leg["entry"] - exit_price)
        leg["status"] = "CLOSED"
        leg["ts_close"] = _now()
        leg["exit"] = round(exit_price, 2)
        leg["exit_reason"] = reason
        leg["pnl_pts"] = round(pts, 2)
        leg["pnl_eur"] = round(pts * SIZE, 2)
        _append(TRADES_F, leg)
        self.legs = [l for l in self.legs if l["id"] != leg["id"]]
        self._save_state()
        logger.info("[SHADOW] CLOSE #%d %s %s %s @ %.2f -> %s %+.2f EUR (papier)"
                    % (leg["id"], leg["tag"], leg["direction"], self.instrument,
                       exit_price, reason, leg["pnl_eur"]))

    # ---------------- evenements du groupe ----------------
    async def handle_text(self, text):
        _append(MSGS_F, {"ts": _now(), "text": (text or "")[:2000]})
        sig = parse_gp(text)
        if not sig:
            return
        logger.info("[SHADOW] message classe : %s" % sig["type"])
        if sig["type"] == "PREVISION":
            _append(TRADES_F, {"ts": _now(), "event": "PREVISION", **{
                k: sig.get(k) for k in ("direction", "zone_lo", "zone_hi",
                                        "sl", "tp1", "tp2", "tp3")}})
            return
        if sig["type"] == "ENTRY":
            await self._on_entry(sig)
        elif sig["type"] == "BREAKEVEN":
            n = 0
            for leg in self.legs:
                leg["sl"] = leg["entry"]; leg["be"] = True; n += 1
            self._save_state()
            logger.info("[SHADOW] BREAKEVEN -> %d jambe(s) papier" % n)
            if n:
                self._alert("BREAKEVEN applique sur %d jambe(s) papier" % n)
        elif sig["type"] == "STOP":
            price = self._price()
            if price:
                for leg in list(self.legs):
                    self._close_leg(leg, price, "GROUP_STOP")

    async def _on_entry(self, sig):
        price = self._price()
        entry = price or (sig["zone_lo"] + sig["zone_hi"]) / 2.0
        targets = [("TP1", sig.get("tp1", 0.0)), ("TP2", sig.get("tp2", 0.0)),
                   ("TP3", sig.get("tp3", 0.0)), ("RUN", 0.0)]
        for tag, tp in targets:
            leg = {"id": self._next_id, "tag": tag, "direction": sig["direction"],
                   "entry": round(entry, 2), "entry_src": "live" if price else "zone_mid",
                   "zone": [sig["zone_lo"], sig["zone_hi"]],
                   "sl": sig["sl"], "tp": tp or 0.0, "size": SIZE,
                   "ts_open": _now(), "status": "OPEN", "be": False,
                   "mfe_pts": 0.0, "mae_pts": 0.0,
                   "lock_exit_pts": None}     # verrou +6/+5 virtuel
            self._next_id += 1
            self.legs.append(leg)
        self._save_state()
        logger.info("[SHADOW] ENTREE papier %s 4 jambes @ %.2f (%s) SL=%s "
                    "TP1=%s TP2=%s TP3=%s RUN=open"
                    % (sig["direction"], entry, "live" if price else "zone_mid",
                       sig["sl"], sig.get("tp1"), sig.get("tp2"), sig.get("tp3")))
        self._alert("ENTREE papier %s XAUUSD @ %.2f | SL %s | TP %s/%s/%s/open"
                    % (sig["direction"], entry, sig["sl"],
                       sig.get("tp1"), sig.get("tp2"), sig.get("tp3")))

    # ---------------- tick de simulation (prix live) ----------------
    async def _tick(self):
        if not self.legs:
            return
        price = self._price()
        if not price:
            return
        dirty = False
        for leg in list(self.legs):
            long = leg["direction"] == "LONG"
            profit = (price - leg["entry"]) if long else (leg["entry"] - price)
            if profit > leg["mfe_pts"]:
                leg["mfe_pts"] = round(profit, 2); dirty = True
            if profit < leg["mae_pts"]:
                leg["mae_pts"] = round(profit, 2); dirty = True
            # verrou virtuel +6/+5 : on note ou il AURAIT ferme (sans fermer)
            if leg["lock_exit_pts"] is None and leg["mfe_pts"] >= LOCK_ARM \
                    and profit <= LOCK_EXIT:
                leg["lock_exit_pts"] = round(profit, 2); dirty = True
                logger.info("[SHADOW] verrou virtuel #%d %s aurait ferme a %+.2f pts"
                            % (leg["id"], leg["tag"], profit))
            # SL papier (ou BE si remonte)
            if leg["sl"] and ((long and price <= leg["sl"]) or
                              (not long and price >= leg["sl"])):
                self._close_leg(leg, leg["sl"],
                                "BE" if leg.get("be") else "SL")
                continue
            # TP papier
            if leg["tp"] and ((long and price >= leg["tp"]) or
                              (not long and price <= leg["tp"])):
                self._close_leg(leg, leg["tp"], "TP")
        if dirty:
            self._save_state()

    async def run(self):
        logger.info("[SHADOW] tracker papier Groupe Prive demarre "
                    "(groupe %s | tick %.0fs | 4 jambes | verrou virtuel +%.0f/+%.0f)"
                    % (SHADOW_GROUP_ID, TICK_SEC, LOCK_ARM, LOCK_EXIT))
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.error("[SHADOW] tick erreur : %s" % e)
            await asyncio.sleep(TICK_SEC)
