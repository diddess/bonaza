"""
instruments.py - Configuration des instruments surveilles par Bonaza
====================================================================
EPICs confirmes (mini-contrats 1 EUR/point, 2026-05-20) :
    XAUUSD : CS.D.CFEGOLD.CFE.IP  (confirme — streaming OK, 1 EUR/pt)
    DAX    : IX.D.DAX.IFMM.IP     (mini 1 EUR/pt — minDealSize 0.5)
    CAC40  : IX.D.CAC.IMF.IP      (mini 1 EUR/pt — minDealSize 0.5)
Anciens EPICs (a ne plus utiliser) :
    DAX    : IX.D.DAX.IMF.IP     (5 EUR/pt — trop gros pour DEMO)
    CAC40  : IX.D.CAC.IFMM.IP    (403 unauthorised, exchange BMU_FUT_1)

Parametres DEMO :
    ma_tolerance = 3.0 x ATR
    → Permet de trader meme si le prix est legerement sous la MA200
    → Sur DEMO uniquement pour valider le pipeline complet (ordres, P&L)
    → Reduire a 0.5 avant passage en LIVE
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "historical"


@dataclass
class InstrumentConfig:
    name:          str
    epic:          str
    scale:         str
    tf:            str
    mode:          str
    session_start: int
    session_end:   int
    currency:      str   = "EUR"
    sl_mult:       float = 1.0
    tp_mult:       float = 3.0
    adx_min:       float = 18.0
    swing_order:   int   = 15
    ma_period:     int   = 2400
    ma_tolerance:  float = 3.0   # DEMO : large pour valider le pipeline
    cooldown_bars: int   = 24
    bv3_enabled:   bool  = True  # Si False, Bv3 alimente le buffer mais n'emet aucun signal

    @property
    def parquet_path(self) -> Path:
        return DATA_DIR / f"{self.name}_{self.tf}.parquet"

    @property
    def is_paper(self) -> bool:
        return self.mode == "PAPER"

    @property
    def label(self) -> str:
        return f"{self.name} {self.tf} [{self.mode}]"


INSTRUMENTS: dict[str, InstrumentConfig] = {

    # XAUUSD : Bv3 ACTIF en PAPER (ordres reels). Agent IA en pause (AI_AGENT_ENABLED=false).
    "XAUUSD": InstrumentConfig(
        name          = "XAUUSD",
        epic          = "CS.D.CFEGOLD.CFE.IP",
        scale         = "5MINUTE",
        tf            = "M5",
        mode          = "PAPER",
        session_start = 16,
        session_end   = 21,
        sl_mult       = 1.0,
        tp_mult       = 1.5,    # 2026-05-26 : divise par 2 (etait 3.0) - TP atteignable
        adx_min       = 18.0,
        swing_order   = 15,
        ma_period     = 2400,
        ma_tolerance  = 3.0,
        cooldown_bars = 24,
        bv3_enabled   = True,    # Bv3 actif sur XAUUSD
    ),

    # DAX : Bv3 en PAPER. EPIC mini IFMM 0.5 lot.
    # ATTENTION : SL 15-30pts * 0.5 = 7.5-15E/trade -> majorite REJETES par RM
    # tant que BONAZA_MAX_CAPITAL_PCT reste a 1.5% (= 7.29E max sur 485E).
    "DAX": InstrumentConfig(
        name          = "DAX",
        epic          = "IX.D.DAX.IFMM.IP",
        scale         = "5MINUTE",
        tf            = "M5",
        mode          = "PAPER",
        session_start = 8,
        session_end   = 16,
        sl_mult       = 1.0,
        tp_mult       = 1.5,    # 2026-05-26 : divise par 2 (etait 3.0) - TP atteignable
        adx_min       = 18.0,
        swing_order   = 15,
        ma_period     = 2400,
        ma_tolerance  = 3.0,
        cooldown_bars = 24,
    ),

    # CAC40 : Bv3 en PAPER. EPIC mini IMF 0.5 lot.
    # SL CAC40 min_stop=12pts + ATR ~10-20 -> 6-15E/trade. Plupart passent avec
    # BONAZA_MAX_CAPITAL_PCT=1.5% (=7.29E). Les trades a SL >= 15pts seront REJETES.
    "CAC40": InstrumentConfig(
        name          = "CAC40",
        epic          = "IX.D.CAC.IMF.IP",
        scale         = "5MINUTE",
        tf            = "M5",
        mode          = "PAPER",
        session_start = 8,
        session_end   = 16,
        sl_mult       = 1.0,
        tp_mult       = 1.5,    # 2026-05-26 : divise par 2 (etait 3.0) - TP atteignable
        adx_min       = 18.0,
        swing_order   = 15,
        ma_period     = 2400,
        ma_tolerance  = 3.0,
        cooldown_bars = 24,
    ),
}

SUBSCRIPTIONS = [
    {"epic": inst.epic, "scale": inst.scale}
    for inst in INSTRUMENTS.values()
]
