"""
ig_rules.py - Specs et regles de dealing IG Markets pour les EPICs Bonaza
==========================================================================
Source : sondage `fetch_market_by_epic` du 2026-05-22.

Pourquoi ce fichier existe :
  IG rejette les ordres si SL/TP/size ne respectent pas les contraintes precises
  du marche. Les decouvrir une par une via des erreurs en prod coute du temps
  et fait rater des signaux. Ce module precharge les regles connues et fournit
  des fonctions d'ajustement automatique.

Comment l'utiliser :
  from ig_rules import rules_for
  rules = rules_for("CAC40")
  sl_dist_safe, motif = rules.adjust_stop_distance(8.0)   # -> (12.0, "min_stop 8.00->12.00")
  size_safe, _        = rules.adjust_size(0.3)             # -> (0.5, "min_size 0.30->0.50")
  margin_eur          = rules.margin_for(0.5, 8100.0)      # -> ~607 EUR

Mise a jour :
  Re-sonder via fetch_market_by_epic si IG modifie les specs (rare mais possible).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional, Tuple


# -----------------------------------------------------------------------
# Structures
# -----------------------------------------------------------------------

@dataclass
class MarginBand:
    """Bande de marge progressive. La marge augmente avec le notional."""
    min_notional: float                  # inclus
    max_notional: Optional[float]        # exclus, None = pas de plafond
    margin_pct:   float                  # ex: 5.0
    currency:     str = "EUR"


@dataclass
class MarketHours:
    """
    Plages horaires de cotation IG pour un instrument (toutes en UTC).

    Modele simplifie :
      - `weekly_open_dow` / `weekly_open_hour`  : ouverture hebdo (ex: dimanche 22h UTC)
      - `weekly_close_dow` / `weekly_close_hour`: fermeture hebdo (ex: vendredi 21h UTC)
      - `daily_break_start_h` / `daily_break_end_h`: pause technique quotidienne (None si pas de pause)
        Ex: XAUUSD 21h-22h UTC chaque jour ouvre
      - `business_days`: tuple de days-of-week (0=lundi..6=dimanche) ou
                        None = utiliser weekly_open/close uniquement

    Note : ne tient pas compte des jours feries specifiques.
    Day-of-week : 0=lundi 1=mardi 2=mercredi 3=jeudi 4=vendredi 5=samedi 6=dimanche
    """
    name:                 str
    timezone:             str   = "UTC"
    # Mode "indice quotidien" (DAX/CAC) : ouvre Lun-Ven a daily_open_h, ferme a daily_close_h
    daily_open_h:         Optional[int] = None     # ex: 7 pour DAX
    daily_close_h:        Optional[int] = None     # ex: 21 pour DAX
    business_days:        Tuple[int, ...] = (0, 1, 2, 3, 4)   # Lun-Ven par defaut
    # Mode "marche 24/5" (XAUUSD, FX) : ouvre Dim soir, ferme Ven soir
    weekly_open_dow:      Optional[int] = None     # 6=dimanche
    weekly_open_hour:     Optional[int] = None     # 22 pour XAUUSD
    weekly_close_dow:     Optional[int] = None     # 4=vendredi
    weekly_close_hour:    Optional[int] = None     # 21 pour XAUUSD
    daily_break_start_h:  Optional[int] = None     # 21 pour XAUUSD (pause maintenance)
    daily_break_end_h:    Optional[int] = None     # 22 pour XAUUSD (1h)
    notes:                str   = ""

    # -------------------------------------------------------------
    # Tests etat
    # -------------------------------------------------------------
    def is_open_at(self, dt_utc: datetime) -> bool:
        """True si le marche est cote a dt_utc (UTC)."""
        dow  = dt_utc.weekday()
        hour = dt_utc.hour

        # Mode 24/5 (XAUUSD)
        if self.weekly_open_dow is not None:
            # Construire fenetre [open_dt, close_dt] de la semaine courante
            week_ref = dt_utc
            open_dt  = self._anchor_in_week(week_ref, self.weekly_open_dow, self.weekly_open_hour)
            close_dt = self._anchor_in_week(week_ref, self.weekly_close_dow, self.weekly_close_hour)
            if close_dt <= open_dt:
                close_dt += timedelta(days=7)
            # Si avant open de cette semaine, comparer a semaine precedente
            if dt_utc < open_dt:
                open_dt -= timedelta(days=7)
                close_dt -= timedelta(days=7)
            if not (open_dt <= dt_utc < close_dt):
                return False
            # Pause quotidienne ?
            if (self.daily_break_start_h is not None
                    and self.daily_break_end_h is not None):
                if self.daily_break_start_h <= hour < self.daily_break_end_h:
                    return False
            return True

        # Mode indice quotidien (DAX, CAC)
        if self.daily_open_h is not None and self.daily_close_h is not None:
            if dow not in self.business_days:
                return False
            return self.daily_open_h <= hour < self.daily_close_h

        return True   # fallback : toujours ouvert

    def is_open_now(self) -> bool:
        return self.is_open_at(datetime.now(tz=timezone.utc))

    def next_close_after(self, dt_utc: datetime) -> Optional[datetime]:
        """Retourne le prochain instant de fermeture apres dt_utc, ou None si inconnu."""
        # Mode 24/5 : la fermeture est weekly_close, ou la prochaine pause quotidienne
        if self.weekly_open_dow is not None:
            candidates = []
            close_dt = self._anchor_in_week(dt_utc, self.weekly_close_dow, self.weekly_close_hour)
            if close_dt <= dt_utc:
                close_dt += timedelta(days=7)
            candidates.append(close_dt)
            if self.daily_break_start_h is not None:
                next_break = dt_utc.replace(hour=self.daily_break_start_h,
                                            minute=0, second=0, microsecond=0)
                if next_break <= dt_utc:
                    next_break += timedelta(days=1)
                candidates.append(next_break)
            return min(candidates)
        # Mode indice quotidien
        if self.daily_close_h is not None:
            close = dt_utc.replace(hour=self.daily_close_h, minute=0, second=0, microsecond=0)
            if close <= dt_utc or dt_utc.weekday() not in self.business_days:
                # avancer au prochain business day
                d = dt_utc
                for _ in range(7):
                    d = d + timedelta(days=1)
                    d = d.replace(hour=self.daily_close_h, minute=0, second=0, microsecond=0)
                    if d.weekday() in self.business_days:
                        return d
            return close
        return None

    @staticmethod
    def _anchor_in_week(ref: datetime, target_dow: int, target_hour: int) -> datetime:
        """Datetime de la semaine de ref correspondant a (target_dow, target_hour:00)."""
        delta = target_dow - ref.weekday()
        anchor = (ref + timedelta(days=delta)).replace(
            hour=target_hour, minute=0, second=0, microsecond=0,
        )
        return anchor


@dataclass
class DealingRules:
    """Toutes les regles IG pour un EPIC, telles que sondees via fetch_market_by_epic."""
    epic:              str
    instrument:        str
    decimal_places:    int       # snapshot.decimalPlacesFactor (precision des prix)
    min_deal_size:     float     # dealingRules.minDealSize.value (POINTS = lots)
    min_step_distance: float     # dealingRules.minStepDistance.value (POINTS)
    min_stop_distance: float     # dealingRules.minNormalStopOrLimitDistance.value
    max_stop_pct:      float     # dealingRules.maxStopOrLimitDistance.value (PERCENTAGE)
    margin_bands:      List[MarginBand]
    value_of_one_pip:  float     # instrument.valueOfOnePip (en devise du contrat)
    contract_size:     float     # instrument.contractSize
    currency:          str       # instrument.currencies[0].code
    # Optionnel : pour eviter de stopper trop pres en cas de slippage
    safety_buffer_pts: float = 0.5
    # Horaires de cotation IG
    market_hours:      Optional[MarketHours] = None

    # -----------------------------------------------------------------
    # Ajustements
    # -----------------------------------------------------------------

    def adjust_stop_distance(self, requested: float) -> Tuple[float, str]:
        """
        Ajuste une distance SL/TP au min_stop + snap au minStep.
        Ajoute un safety_buffer pour eviter ATTACHED_ORDER_LEVEL_ERROR
        en cas de slippage entre calcul et envoi.

        Returns:
            (distance_finale_en_points, motif_humain)
            motif = '' si aucun ajustement necessaire.
        """
        if requested is None or not math.isfinite(requested) or requested <= 0:
            return self.min_stop_distance + self.safety_buffer_pts, "nan/neg->min"

        motifs = []
        d = requested

        # Etape 1 : min_stop + buffer de securite
        threshold = self.min_stop_distance + self.safety_buffer_pts
        if d < threshold:
            motifs.append(f"min_stop {d:.2f}->{threshold:.2f}")
            d = threshold

        # Etape 2 : snap au min_step
        step = self.min_step_distance
        if step > 0:
            snapped = round(d / step) * step
            if snapped < threshold:
                snapped += step
            if abs(snapped - d) > 1e-9:
                motifs.append(f"snap step {d:.2f}->{snapped:.2f}")
                d = snapped

        # Etape 3 : arrondi precision marche
        d = round(d, self.decimal_places)
        return d, ", ".join(motifs)

    def adjust_limit_distance(self, requested: float) -> Tuple[float, str]:
        """
        Ajuste une distance de TP (limit) au min IG + snap au minStep.
        Identique a adjust_stop_distance MAIS sans safety_buffer_pts :
        IG utilise le meme minNormalStopOrLimitDistance pour stop ET limit,
        mais le buffer anti-slippage n'a aucun sens sur un TP (il deforme le R:R).
        """
        if requested is None or not math.isfinite(requested) or requested <= 0:
            return self.min_stop_distance, "nan/neg->min"

        motifs = []
        d = requested
        threshold = self.min_stop_distance
        if d < threshold:
            motifs.append(f"min_limit {d:.2f}->{threshold:.2f}")
            d = threshold
        step = self.min_step_distance
        if step > 0:
            snapped = round(d / step) * step
            if snapped < threshold:
                snapped += step
            if abs(snapped - d) > 1e-9:
                motifs.append(f"snap step {d:.2f}->{snapped:.2f}")
                d = snapped
        d = round(d, self.decimal_places)
        return d, ", ".join(motifs)

    def adjust_size(self, requested: float) -> Tuple[float, str]:
        """
        Ajuste une taille au min_deal_size + snap au pas.
        Suppose que le pas de taille = min_deal_size (vrai pour XAUUSD/DAX/CAC40).

        Returns:
            (taille_finale, motif_humain)
        """
        if requested is None or not math.isfinite(requested) or requested <= 0:
            return self.min_deal_size, "nan/neg->min"

        motifs = []
        s = requested

        if s < self.min_deal_size:
            motifs.append(f"min_size {s:.2f}->{self.min_deal_size:.2f}")
            s = self.min_deal_size

        # Snap au step (= min_deal_size pour ces EPICs)
        step = self.min_deal_size
        snapped = round(s / step) * step
        if snapped < self.min_deal_size:
            snapped = self.min_deal_size
        if abs(snapped - s) > 1e-9:
            motifs.append(f"snap size {s:.2f}->{snapped:.2f}")
            s = snapped

        return round(s, 4), ", ".join(motifs)

    # -----------------------------------------------------------------
    # Marge et notional
    # -----------------------------------------------------------------

    def notional(self, size: float, price: float) -> float:
        """Notional brut = size * prix * valueOfOnePip * contractSize."""
        return size * price * self.value_of_one_pip * self.contract_size

    def margin_for(self, size: float, price: float) -> float:
        """
        Estime la marge requise pour ouvrir size@price selon les bands progressives.
        Retourne la marge en devise du contrat.
        Approximation : on prend la marge de la band correspondant au notional.
        """
        notional = self.notional(size, price)
        margin_pct = self.margin_bands[-1].margin_pct
        for band in self.margin_bands:
            mn = band.min_notional
            mx = band.max_notional if band.max_notional is not None else float("inf")
            if mn <= notional < mx:
                margin_pct = band.margin_pct
                break
        return notional * margin_pct / 100.0

    # -----------------------------------------------------------------
    # Validation d'un setup
    # -----------------------------------------------------------------

    def max_stop_for_price(self, price: float) -> float:
        """Distance SL/TP max autorisee = max_stop_pct % du prix."""
        return price * self.max_stop_pct / 100.0


# -----------------------------------------------------------------------
# Catalogue des regles sondees (2026-05-22)
# -----------------------------------------------------------------------

RULES: dict[str, DealingRules] = {

    "XAUUSD": DealingRules(
        epic               = "CS.D.CFEGOLD.CFE.IP",
        instrument         = "XAUUSD",
        decimal_places     = 2,
        min_deal_size      = 0.5,   # 2026-05-31 re-sonde IG : 0.5 (etait 0.1, faux -> MINIMUM_ORDER_SIZE_ERROR)
        min_step_distance  = 1.0,
        min_stop_distance  = 4.0,   # 2026-05-31 re-sonde IG : 4.0 POINTS (etait 1.0 -> ATTACHED_ORDER_LEVEL_ERROR)
        max_stop_pct       = 75.0,
        margin_bands       = [
            MarginBand(0,    2300, 5.0, "USD"),
            MarginBand(2300, 4600, 5.0, "USD"),
            MarginBand(4600, 6900, 5.0, "USD"),
            MarginBand(6900, None, 7.5, "USD"),
        ],
        value_of_one_pip   = 1.0,
        contract_size      = 1.0,
        currency           = "EUR",
        safety_buffer_pts  = 0.5,
        market_hours       = MarketHours(
            name              = "XAUUSD CFD or",
            weekly_open_dow   = 6,    # dimanche
            weekly_open_hour  = 22,   # 22h UTC = 18h EST (ouverture Wall Street CME Globex)
            weekly_close_dow  = 4,    # vendredi
            weekly_close_hour = 21,   # 21h UTC = 17h EST
            daily_break_start_h = 21, # pause technique 21h-22h UTC chaque jour
            daily_break_end_h   = 22,
            notes = ("Marche 23h/24 du dim 22h UTC au ven 21h UTC. "
                     "Pause maintenance quotidienne 21h-22h UTC. "
                     "Liquidite max sessions Londres 8-17h UTC + NY 13-22h UTC. "
                     "Eviter friday last hour (gap weekend) et premiere heure dimanche soir."),
        ),
    ),

    # DAX : EPIC mini comptant. EPIC futures IFE (min_size 0.2) testé le
    # 25/05/2026 : compte LIVE LUZQM ne possede PAS les permissions futures
    # -> Lightstreamer renvoie 'Insufficient permissions'. Demarche IG requise
    # pour activer les futures avant de re-tenter.
    "DAX": DealingRules(
        epic               = "IX.D.DAX.IFMM.IP",
        instrument         = "DAX",
        decimal_places     = 1,
        min_deal_size      = 0.5,
        min_step_distance  = 1.0,
        min_stop_distance  = 12.0,   # 2026-05-31 re-sonde IG : 12.0 POINTS (etait 5.0 -> ATTACHED_ORDER_LEVEL_ERROR)
        max_stop_pct       = 75.0,
        margin_bands       = [
            MarginBand(0,    350,  5.0, "EUR"),
            MarginBand(350,  1750, 5.0, "EUR"),
            MarginBand(1750, 2800, 5.0, "EUR"),
            MarginBand(2800, None, 15.0, "EUR"),
        ],
        value_of_one_pip   = 1.0,
        contract_size      = 1.0,
        currency           = "EUR",
        safety_buffer_pts  = 1.0,   # plus de buffer car index volatile
        market_hours       = MarketHours(
            name           = "DAX Allemagne 40 (1 EUR/pt)",
            daily_open_h   = 7,     # ouverture cash Frankfurt
            daily_close_h  = 21,    # extension after-hours via IG
            business_days  = (0, 1, 2, 3, 4),
            notes = ("Cash IG : 7h-21h UTC lun-ven (apres-marche inclus). "
                     "Cash sous-jacent Frankfurt : 8h-16h30 UTC. "
                     "Volatilite max ouverture 8h UTC + sortie statistiques US 12h30/13h30 UTC."),
        ),
    ),

    # CAC40 : EPIC mini comptant. Même reason que DAX pour le rollback IFE.
    "CAC40": DealingRules(
        epic               = "IX.D.CAC.IMF.IP",
        instrument         = "CAC40",
        decimal_places     = 1,
        min_deal_size      = 0.5,
        min_step_distance  = 1.0,
        min_stop_distance  = 12.0,    # ! Beaucoup plus large que XAUUSD/DAX
        max_stop_pct       = 75.0,
        margin_bands       = [
            MarginBand(0,    390,  5.0, "EUR"),
            MarginBand(390,  2340, 5.0, "EUR"),
            MarginBand(2340, 3900, 5.0, "EUR"),
            MarginBand(3900, None, 15.0, "EUR"),
        ],
        value_of_one_pip   = 1.0,
        contract_size      = 1.0,
        currency           = "EUR",
        safety_buffer_pts  = 1.0,
        market_hours       = MarketHours(
            name           = "CAC40 France 40 (1 EUR/pt)",
            daily_open_h   = 7,
            daily_close_h  = 19,    # cotation 24h chez IG mais sous-jacent traite 7-19 UTC
            business_days  = (0, 1, 2, 3, 4),
            notes = ("Sous-jacent Euronext Paris 7-19h UTC lun-ven. "
                     "Cotation IG quasi 24h mais spreads larges hors heures cash. "
                     "Privilegier 8-17h UTC."),
        ),
    ),
}


def rules_for(instrument: str) -> Optional[DealingRules]:
    """Retourne les regles pour un nom d'instrument Bonaza, None si inconnu."""
    return RULES.get(instrument)


if __name__ == "__main__":
    # Mini test
    for name, r in RULES.items():
        print(f"\n=== {name} ({r.epic}) ===")
        print(f"  min_stop={r.min_stop_distance} pts | min_size={r.min_deal_size} "
              f"| step={r.min_step_distance} | decimals={r.decimal_places}")
        d, m = r.adjust_stop_distance(8.0)
        print(f"  adjust SL 8.0 -> {d} pts  ({m or 'no change'})")
        d, m = r.adjust_stop_distance(20.0)
        print(f"  adjust SL 20.0 -> {d} pts ({m or 'no change'})")
        s, m = r.adjust_size(0.3)
        print(f"  adjust size 0.3 -> {s} ({m or 'no change'})")
        marg = r.margin_for(0.5, {"XAUUSD": 4520, "DAX": 24800, "CAC40": 8100}[name])
        print(f"  margin for 0.5@market ~= {marg:.0f} EUR")
