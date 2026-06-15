"""Parser des signaux TRADAMAX PREMIUM (Telegram).
Entree type :
    🚨 ACHAT XAUUSD / VENTE XAUUSD
    ENTRY: 4463 (ou fourchette 4466-68)
    SL: 4458
    TP1: 4467 / TP2: 4475 / TP3: open
Gestion : breakeven / supprimer / cloturer / SL.

Types retournes (fix faux-CLOSE du 2026-06-11) :
    ENTRY          nouvelle entree (direction, entry, sl, tp1, tp2)
    BREAKEVEN      deplacer les SL a l'entree
    CLOSE          fermeture TOTALE, formes explicites uniquement (reason)
    CLOSE_PARTIAL  fermeture de la SEULE jambe TPn (leg=1..3)
    CANCEL_PENDING annulation d'un ordre EN ATTENTE (ne touche jamais une
                   position ouverte ; copieur market-only => no-op + info)
    AMBIGUOUS      mot-cle de gestion present mais forme non reconnue ou
                   conditionnelle -> AUCUNE action automatique, confirmation
                   operateur demandee (texte original dans 'text')
"""
import re
import unicodedata

INSTR_MAP = {"XAUUSD": "XAUUSD", "GOLD": "XAUUSD", "OR": "XAUUSD"}

# mots-cles qui declenchaient l'ancien CLOSE fourre-tout (apres strip accents)
_CLOSE_KEYWORDS = ("SUPPRIM", "CLOTUR", "FERMEZ", "FERMER")

# phrase conditionnelle / future => jamais d'action immediate
_CONDITIONAL_RX = re.compile(
    r"\bSI\b|\bQUAND\b|\bLORSQU|PLUS\s+TARD|CLOTURERA|FERMERA|\bON\s+VA\b|PENSEZ\s+A|PREVOIR")

# fermeture partielle d'une jambe : "CLOTUREZ (LE) TP2", "CLOTURER LE TP 3",
# "FERMEZ TP1"... (\w* couvre imperatif ET infinitif)
_PARTIAL_RX = re.compile(r"(?:CLOTUR\w*|FERM\w*)\s+(?:LE\s+)?TP\s*([1-3])")

# annulation d'un ordre en attente : "SUPPRIMER L'ORDRE", "SUPPRIMEZ L ORDRE EN ATTENTE"
_CANCEL_RX = re.compile(r"SUPPRIM\w*\s+(?:L['’]?\s*)?ORDRE")

# fermeture TOTALE : formes explicites uniquement
_CLOSE_ALL_RX = [re.compile(rx) for rx in (
    r"\bFERMEZ?\s+TOUT\b",
    r"\bCLOTUREZ?\s+TOUT\b",
    r"\bTOUT\s+FERMER\b",
    r"\bTOUT\s+CLOTURER\b",
    r"\bFERMEZ?\s+(?:LA\s+|LES\s+)?POSITIONS?\b",
    r"\bCLOTUREZ?\s+(?:LA\s+|LES\s+)?POSITIONS?\b",
    r"\bFERMEZ?\s+(?:LE\s+)?TRADES?\b",
    r"\bCLOTUREZ?\s+(?:LE\s+)?TRADES?\b",
    r"SUPPRIM\w*\s+(?:LA\s+|LES\s+)?POSITIONS?\b",   # "supprimez la position" = fermer
)]


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _num(s):
    try: return float(s.replace(",", "."))
    except Exception: return None


def parse_signal(text):
    if not text or not text.strip():
        return None
    t = _strip_accents(text.upper())   # CLÔTUREZ -> CLOTUREZ, etc.

    # --- ENTRY (inchange) ---
    has_dir = ("ACHAT" in t) or ("VENTE" in t)
    if has_dir and "ENTRY" in t and "SL" in t and "TP1" in t:
        direction = "LONG" if "ACHAT" in t else "SHORT"
        instrument = next((v for k, v in INSTR_MAP.items() if k in t), None)
        m_e = re.search(r"ENTRY\s*[:：]?\s*([0-9]+(?:[.,][0-9]+)?)", t)
        m_sl = re.search(r"\bSL\s*[:：]?\s*([0-9]+(?:[.,][0-9]+)?)", t)
        m_t1 = re.search(r"TP1\s*[:：]?\s*([0-9]+(?:[.,][0-9]+)?)", t)
        m_t2 = re.search(r"TP2\s*[:：]?\s*([0-9]+(?:[.,][0-9]+)?)", t)
        return {
            "type": "ENTRY", "direction": direction, "instrument": instrument,
            "entry": _num(m_e.group(1)) if m_e else None,
            "sl":    _num(m_sl.group(1)) if m_sl else None,
            "tp1":   _num(m_t1.group(1)) if m_t1 else None,
            "tp2":   _num(m_t2.group(1)) if m_t2 else None,
        }

    # --- BREAKEVEN (inchange) ---
    if "BREAKEVEN" in t:
        return {"type": "BREAKEVEN"}

    # --- message "SL" seul = stop touche, tout fermer (inchange) ---
    if t.strip() == "SL":
        return {"type": "CLOSE", "reason": "SL"}

    # --- instructions de fermeture : classification fine (fix faux-CLOSE) ---
    if not any(k in t for k in _CLOSE_KEYWORDS):
        return None                          # bruit (TP HIT, commentaires...)

    if _CONDITIONAL_RX.search(t):
        # phrase conditionnelle/future ("si...", "on cloturera...") :
        # JAMAIS d'action immediate, on demande a l'operateur.
        return {"type": "AMBIGUOUS", "hint": "conditionnel", "text": text[:400]}

    m = _PARTIAL_RX.search(t)
    if m:
        return {"type": "CLOSE_PARTIAL", "leg": int(m.group(1))}

    if _CANCEL_RX.search(t):
        return {"type": "CANCEL_PENDING"}

    for rx in _CLOSE_ALL_RX:
        if rx.search(t):
            return {"type": "CLOSE", "reason": "instruction"}

    # mot-cle present mais forme inconnue -> aucune action automatique
    return {"type": "AMBIGUOUS", "hint": "forme_inconnue", "text": text[:400]}
