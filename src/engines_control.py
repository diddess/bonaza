"""
engines_control.py — Toggle runtime des moteurs Bv3 et Agent IA.

Lu par main.py (engine_task Bv3) et ai_trading_agent (tick) à chaque cycle
pour decider si on emet/execute les signaux. Ecrit par le bot Telegram via
la commande /engines.

Fichier source : data/active_engines.json
Format :
{
  "bv3":  true,   # true = Bv3 emet ses signaux, false = trash (engine tourne pour buffer)
  "ai":   true    # true = Agent IA appelle Claude, false = skip (économise tokens)
}

Si fichier absent : valeurs par defaut bv3=True, ai=True (les deux actifs).
Lecture optimisee : cache 5s via mtime check pour eviter I/O excessif (lu
toutes les 60s par les engines en pratique).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from loguru import logger

# Path data accessible des 3 containers
_DATA_DIR  = Path(os.getenv("BONAZA_DB_PATH", "/app/data/bonaza.db")).parent
_FLAG_FILE = _DATA_DIR / "active_engines.json"

# HARD LOCK Bv3 — 2026-05-30 (decision Didier apres bilan LIVE -116 EUR en 5j).
# Tant que cette constante vaut True, Bv3 est force a OFF : le toggle Telegram
# /engines bv3 on n'a aucun effet, et active_engines.json est ignore pour bv3.
# Pour reactiver Bv3 : repasser BV3_HARD_LOCK a False et rebuild le container.
BV3_HARD_LOCK = True

# HARD LOCK Agent IA Claude — 2026-05-30 (decision Didier apres validation du
# nouveau PortfolioRunner deterministe S8+S5+S3). L'agent IA actuel souffrait
# d'un biais SHORT structurel (-116 EUR LIVE en 5j). Remplace par 3 strats
# deterministes validees walk-forward.
# Tant que cette constante vaut True, l'Agent IA est force a OFF (skip ticks).
# Pour le reactiver : repasser AI_HARD_LOCK a False et rebuild le container.
AI_HARD_LOCK = True

# ACTIVATION du nouveau PortfolioRunner (S8 RegimeAdaptive CAC40 M15,
# S5 ToDMomentum XAUUSD M15, S3 ORB XAUUSD M15) — 2026-05-30.
PORTFOLIO_ENABLED = True

_DEFAULT = {"bv3": True, "ai": True}
_cache: dict = dict(_DEFAULT)
_cache_mtime: float = 0.0
_cache_check_ts: float = 0.0
_CACHE_TTL_S = 5.0   # check fichier max 1 fois par 5s


def _reload_if_stale() -> None:
    """Recharge depuis le fichier si mtime a change OU jamais lu."""
    global _cache, _cache_mtime, _cache_check_ts
    now = time.time()
    if now - _cache_check_ts < _CACHE_TTL_S:
        return
    _cache_check_ts = now
    if not _FLAG_FILE.exists():
        _cache = dict(_DEFAULT)
        _cache_mtime = 0.0
        return
    try:
        mt = _FLAG_FILE.stat().st_mtime
        if mt == _cache_mtime:
            return
        data = json.loads(_FLAG_FILE.read_text(encoding="utf-8"))
        _cache = {"bv3": bool(data.get("bv3", True)),
                  "ai":  bool(data.get("ai", True))}
        _cache_mtime = mt
        logger.info(f"[ENGINES] config rechargee : bv3={_cache['bv3']}  ai={_cache['ai']}")
    except Exception as e:
        logger.warning(f"[ENGINES] lecture active_engines.json fail : {e}")


def is_bv3_enabled() -> bool:
    if BV3_HARD_LOCK:
        return False
    _reload_if_stale()
    return _cache["bv3"]


def is_ai_enabled() -> bool:
    if AI_HARD_LOCK:
        return False
    _reload_if_stale()
    return _cache["ai"]


def is_portfolio_enabled() -> bool:
    """Le PortfolioRunner deterministe (S8 + S5 + S3)."""
    return PORTFOLIO_ENABLED


def get_state() -> dict:
    _reload_if_stale()
    return dict(_cache)


def set_state(bv3: Optional[bool] = None, ai: Optional[bool] = None) -> dict:
    """Met a jour le fichier (sans recharger immediatement - le cache TTL le fera).
    Si BV3_HARD_LOCK actif : toute tentative d'activer bv3 est ignoree."""
    global _cache, _cache_mtime
    state = get_state()
    if bv3 is not None:
        if BV3_HARD_LOCK and bool(bv3):
            logger.warning("[ENGINES] tentative d'activer Bv3 ignoree (BV3_HARD_LOCK)")
            state["bv3"] = False
        else:
            state["bv3"] = bool(bv3)
    if ai is not None:
        if AI_HARD_LOCK and bool(ai):
            logger.warning("[ENGINES] tentative d'activer agent IA ignoree (AI_HARD_LOCK)")
            state["ai"] = False
        else:
            state["ai"] = bool(ai)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _FLAG_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    # Update cache immediat
    _cache = state
    _cache_mtime = _FLAG_FILE.stat().st_mtime
    return state


if __name__ == "__main__":
    print("Etat actuel:", get_state())
