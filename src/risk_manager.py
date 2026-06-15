"""
risk_manager.py - M04 : Gestion du risque Bonaza
=================================================
Responsabilites :
  - Validation d'un signal avant ouverture de position
  - Calcul de la taille de position (% capital / distance SL)
  - Suivi des positions ouvertes et du P&L de session
  - Drawdown journalier : arret automatique si limite atteinte
  - Kill switch global (manuel ou automatique)
  - Trailing stop et break-even
  - Historique des trades fermes (session)

Architecture :
  RiskManager
    |-- validate_signal()      <- appele avant envoi d'ordre
    |-- on_fill()              <- appele apres confirmation broker
    |-- on_close()             <- appele apres fermeture confirmee
    |-- update_trailing_stop() <- appele sur chaque nouveau tick
    |-- get_metrics()          <- snapshot etat courant
    |-- reset_session()        <- appele au debut de chaque session

Regles non-negociables :
  - SL est TOUJOURS calcule et place
  - Taille jamais superieure a max_capital_pct
  - Pas de nouveau trade si drawdown journalier depasse la limite
  - Pas de nouveau trade si kill switch actif
  - Mode LIVE necessite une validation explicite du risque
"""
from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from enum import Enum
from typing import Dict, List, Optional

from loguru import logger


# -----------------------------------------------------------------------
# Constantes par defaut (surchargeables via RiskConfig)
# -----------------------------------------------------------------------

DEFAULT_RISK_PCT         = 1.0    # % du capital risque par trade
DEFAULT_MAX_CAPITAL_PCT  = 1.0    # % max du capital engage par trade
DEFAULT_MAX_DAILY_DD_PCT = 3.0    # % max drawdown journalier
DEFAULT_MAX_OPEN_TRADES  = 2      # nombre max de positions simultanees
DEFAULT_MIN_RR           = 1.5    # ratio R:R minimum accepte
DEFAULT_MAX_SPREAD_PTS   = 3.0    # spread maximum accepte (points)

# Trailing stop
DEFAULT_TRAIL_TRIGGER_MULT  = 1.0  # activer le trailing si gain >= 1.0 * ATR
DEFAULT_TRAIL_DISTANCE_MULT = 0.5  # trailing SL = current - 0.5 * ATR

# Break-even
DEFAULT_BE_TRIGGER_MULT  = 0.75   # activer BE si gain >= 0.75 * ATR
DEFAULT_BE_BUFFER_PTS    = 2.0    # points de marge au-dessus de l'entree

# Valeur d'un point par contrat (parametre broker-specifique)
DEFAULT_POINT_VALUE      = 1.0    # 1 EUR/point pour 1 mini contrat DAX


# -----------------------------------------------------------------------
# Enumerations
# -----------------------------------------------------------------------

class RiskDecisionType(str, Enum):
    APPROVED    = "APPROVED"     # Signal valide, ordre autorise
    REJECTED    = "REJECTED"     # Signal rejete, raison fournie
    KILL_SWITCH = "KILL_SWITCH"  # Bloque par kill switch


class PositionStatus(str, Enum):
    OPEN    = "OPEN"
    CLOSED  = "CLOSED"
    STOPPED = "STOPPED"  # Ferme par SL


class KillSwitchReason(str, Enum):
    MANUAL           = "MANUAL"           # Declenche manuellement
    DAILY_DRAWDOWN   = "DAILY_DRAWDOWN"   # Drawdown journalier depasse
    MAX_LOSS_TRADE   = "MAX_LOSS_TRADE"   # Perte excessive sur un trade
    EXTERNAL         = "EXTERNAL"         # Depuis .env ou config


# -----------------------------------------------------------------------
# Structures de donnees
# -----------------------------------------------------------------------

@dataclass
class RiskConfig:
    """
    Parametres de risque. Peut etre construit depuis TradingConfig.
    Toutes les valeurs ont des defaults conservateurs.
    """
    # Risque par trade
    risk_pct:         float = DEFAULT_RISK_PCT
    max_capital_pct:  float = DEFAULT_MAX_CAPITAL_PCT

    # Limites journalieres
    max_daily_dd_pct: float = DEFAULT_MAX_DAILY_DD_PCT
    max_open_trades:  int   = DEFAULT_MAX_OPEN_TRADES
    daily_target_pct: float = 0.0   # Stopper apres +X% de gain (0 = desactive)

    # Qualite du signal
    min_rr:           float = DEFAULT_MIN_RR
    max_spread_pts:   float = DEFAULT_MAX_SPREAD_PTS

    # Trailing stop
    trail_trigger_mult:  float = DEFAULT_TRAIL_TRIGGER_MULT
    trail_distance_mult: float = DEFAULT_TRAIL_DISTANCE_MULT

    # Break-even
    be_trigger_mult:  float = DEFAULT_BE_TRIGGER_MULT
    be_buffer_pts:    float = DEFAULT_BE_BUFFER_PTS

    # Valeur d'un point (depends du broker et du contrat)
    point_value:      float = DEFAULT_POINT_VALUE

    # Sizing : granularite (step) et plafond dur
    # size_step = 0 -> taille entiere (floor a 1). Sinon : multiple de size_step.
    # max_position_size = 0 -> pas de plafond. Sinon : plafond dur.
    size_step:         float = 0.0
    max_position_size: float = 0.0

    # Mode
    is_live:          bool  = False

    @classmethod
    def from_trading_config(cls, trading_cfg) -> "RiskConfig":
        """Construit un RiskConfig depuis le TradingConfig de config.py."""
        return cls(
            risk_pct         = trading_cfg.max_capital_pct,
            max_capital_pct  = trading_cfg.max_capital_pct,
            max_daily_dd_pct = trading_cfg.max_daily_dd_pct,
            is_live          = trading_cfg.is_live(),
        )


@dataclass
class Position:
    """
    Representation d'une position ouverte ou fermee.
    Creee par RiskManager.on_fill(), mise a jour par update_trailing_stop().
    """
    # Identification
    position_id:  str
    setup_name:   str
    instrument:   str
    direction:    str          # "LONG" ou "SHORT"

    # Prix
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    current_sl:   float        # SL actuel (peut bouger avec trailing)
    size:         float        # Nombre de contrats / lots

    # Risque initial
    risk_pts:     float        # entry - SL initial (points)
    risk_amount:  float        # Montant risque en devise

    # Timing
    open_time:    datetime
    close_time:   Optional[datetime] = None

    # Cloture
    exit_price:   Optional[float]  = None
    realized_pnl: Optional[float]  = None
    status:       PositionStatus   = PositionStatus.OPEN

    # Flags de gestion
    be_activated:    bool = False   # Break-even active ?
    trail_activated: bool = False   # Trailing stop active ?
    atr_at_entry:    float = 0.0    # ATR au moment de l'entree

    def unrealized_pnl(self, current_price: float) -> float:
        """P&L flottant en devise."""
        if self.status != PositionStatus.OPEN:
            return 0.0
        pts = (current_price - self.entry_price
               if self.direction == "LONG"
               else self.entry_price - current_price)
        return pts * self.size

    def unrealized_pts(self, current_price: float) -> float:
        """P&L flottant en points."""
        if self.direction == "LONG":
            return current_price - self.entry_price
        return self.entry_price - current_price

    def max_adverse_excursion(self, current_price: float) -> float:
        """Distance au SL actuel (points). Positif = pas encore touche."""
        if self.direction == "LONG":
            return current_price - self.current_sl
        return self.current_sl - current_price

    def to_dict(self) -> dict:
        return {
            "id":          self.position_id,
            "setup":       self.setup_name,
            "instrument":  self.instrument,
            "direction":   self.direction,
            "entry":       round(self.entry_price, 4),
            "sl":          round(self.current_sl, 4),
            "tp":          round(self.take_profit, 4),
            "size":        self.size,
            "risk_pts":    round(self.risk_pts, 4),
            "risk_amount": round(self.risk_amount, 2),
            "status":      self.status.value,
            "pnl":         round(self.realized_pnl, 2) if self.realized_pnl is not None else None,
            "be":          self.be_activated,
            "trail":       self.trail_activated,
            "open_time":   self.open_time.isoformat(),
            "close_time":  self.close_time.isoformat() if self.close_time else None,
        }


@dataclass
class RiskDecision:
    """
    Resultat de la validation d'un signal par RiskManager.
    Si approved, contient la taille de position calculee.
    """
    decision:      RiskDecisionType
    reason:        str
    size:          float  = 0.0    # Nombre de contrats (0 si rejete)
    sl:            float  = 0.0    # Stop loss valide
    tp:            float  = 0.0    # Take profit valide
    risk_amount:   float  = 0.0    # Montant risque en devise
    risk_pct_used: float  = 0.0    # % du capital effectivement risque

    @property
    def approved(self) -> bool:
        return self.decision == RiskDecisionType.APPROVED

    def __repr__(self) -> str:
        if self.approved:
            return (f"RiskDecision[APPROVED] "
                    f"size={self.size:.2f} sl={self.sl:.2f} tp={self.tp:.2f} "
                    f"risk={self.risk_pct_used:.2f}% ({self.risk_amount:.2f})")
        return f"RiskDecision[{self.decision.value}] {self.reason}"


@dataclass
class RiskMetrics:
    """Snapshot de l'etat du gestionnaire de risque a un instant T."""
    # Session
    session_date:       date
    session_start_equity: float
    current_equity:     float
    realized_pnl:       float
    unrealized_pnl:     float
    total_pnl:          float

    # Drawdown
    daily_dd_pct:       float    # Drawdown journalier en %
    daily_dd_limit:     float    # Limite configuree
    dd_remaining_pct:   float    # Marge restante avant kill switch

    # Positions
    open_positions:     int
    max_open_positions: int
    closed_today:       int
    wins_today:         int
    losses_today:       int

    # Kill switch
    kill_switch_active: bool
    kill_switch_reason: Optional[str]

    @property
    def win_rate_today(self) -> Optional[float]:
        total = self.wins_today + self.losses_today
        return self.wins_today / total if total > 0 else None

    def to_dict(self) -> dict:
        return {
            "date":             self.session_date.isoformat(),
            "equity":           round(self.current_equity, 2),
            "realized_pnl":     round(self.realized_pnl, 2),
            "unrealized_pnl":   round(self.unrealized_pnl, 2),
            "total_pnl":        round(self.total_pnl, 2),
            "daily_dd_pct":     round(self.daily_dd_pct, 3),
            "dd_limit_pct":     round(self.daily_dd_limit, 3),
            "dd_remaining_pct": round(self.dd_remaining_pct, 3),
            "open_positions":   self.open_positions,
            "closed_today":     self.closed_today,
            "wins_today":       self.wins_today,
            "losses_today":     self.losses_today,
            "win_rate":         round(self.win_rate_today * 100, 1) if self.win_rate_today else None,
            "kill_switch":      self.kill_switch_active,
            "kill_reason":      self.kill_switch_reason,
        }


# -----------------------------------------------------------------------
# RiskManager - classe principale
# -----------------------------------------------------------------------

class RiskManager:
    """
    Gestionnaire de risque Bonaza.

    Thread-safe : toutes les mutations sont protegees par un Lock.
    Peut etre partage entre plusieurs strategies actives.

    Usage standard :
        rm = RiskManager(config=RiskConfig(), capital=10000.0)

        # Avant d'envoyer l'ordre :
        decision = rm.validate_signal(
            direction="LONG", entry=18080, sl=18050, tp=18140,
            atr=20.0, spread=1.2, setup_name="SETUP_B"
        )
        if decision.approved:
            # Envoyer l'ordre avec decision.size, decision.sl, decision.tp
            ...

        # Apres confirmation du broker :
        pos = rm.on_fill(position_id="...", direction="LONG",
                         entry=18080, sl=18050, tp=18140,
                         size=3.0, setup_name="SETUP_B",
                         instrument="DAX", atr=20.0)

        # Sur chaque tick :
        new_sl = rm.update_trailing_stop("position_id", current_price=18110)

        # A la fermeture :
        rm.on_close("position_id", exit_price=18140)
    """

    def __init__(
        self,
        config:  RiskConfig,
        capital: float,
    ) -> None:
        if capital <= 0:
            raise ValueError(f"Capital invalide : {capital}")
        if not isinstance(config, RiskConfig):
            raise TypeError("config doit etre un RiskConfig")

        self._config              = config
        self._lock                = threading.Lock()

        # Equity
        self._start_equity        = capital
        self._current_equity      = capital

        # Session
        self._session_date        = date.today()
        self._realized_pnl        = 0.0
        self._closed_today:  List[Position] = []
        self._open:          Dict[str, Position] = {}

        # Kill switch
        self._kill_switch_active  = False
        self._kill_switch_reason: Optional[KillSwitchReason] = None

        logger.info(
            "RiskManager initialise",
            capital=capital,
            max_risk_pct=config.risk_pct,
            max_dd_pct=config.max_daily_dd_pct,
            max_open=config.max_open_trades,
            mode="LIVE" if config.is_live else "PAPER",
        )

    # ---------------------------------------------------------------
    # API principale
    # ---------------------------------------------------------------

    def validate_signal(
        self,
        direction:  str,
        entry:      float,
        sl:         float,
        tp:         float,
        atr:        float,
        spread:     float = 0.0,
        setup_name: str   = "",
        instrument: str   = "",
        min_rr_override: float = None,
    ) -> RiskDecision:
        """
        Valide un signal et calcule la taille de position.

        Verifications dans l'ordre :
          1. Kill switch actif ?
          2. Drawdown journalier depasse ?
          3. Objectif journalier atteint ?
          4. Nombre max de positions ouvertes ?
          5. Spread acceptable ?
          6. SL et TP coherents (bonne direction) ?
          7. Ratio R:R suffisant ?
          8. Capital suffisant ?
          9. Taille de position calculable ?

        Returns:
            RiskDecision avec decision APPROVED ou REJECTED/KILL_SWITCH
        """
        with self._lock:
            reject = self._check_pre_conditions(spread)
            if reject:
                return reject

            # Valider la geometrie SL/TP
            geo_check = self._validate_geometry(direction, entry, sl, tp)
            if geo_check:
                return geo_check

            # Calcul de la taille
            sl_pts = abs(entry - sl)
            if sl_pts <= 0:
                return RiskDecision(
                    decision=RiskDecisionType.REJECTED,
                    reason=f"SL_DISTANCE_NULLE (entry={entry:.2f} sl={sl:.2f})",
                )

            # R:R check (override possible pour separer Bv3 1:3 et agent IA 1:1.5+)
            tp_pts = abs(tp - entry)
            rr     = tp_pts / sl_pts
            min_rr_eff = min_rr_override if min_rr_override is not None else self._config.min_rr
            if rr < min_rr_eff:
                return RiskDecision(
                    decision=RiskDecisionType.REJECTED,
                    reason=f"RR_INSUFFISANT {rr:.2f} < min={min_rr_eff:.2f}",
                )

            # Taille de position
            size, risk_amount = self._calc_size(sl_pts)
            if size <= 0:
                return RiskDecision(
                    decision=RiskDecisionType.REJECTED,
                    reason="TAILLE_NULLE capital insuffisant ou sl trop large",
                )

            risk_pct_used = (risk_amount / self._current_equity) * 100

            logger.info(
                "Signal approuve",
                setup=setup_name, direction=direction,
                entry=round(entry, 2), sl=round(sl, 2), tp=round(tp, 2),
                sl_pts=round(sl_pts, 2), tp_pts=round(tp_pts, 2),
                rr=round(rr, 2), size=size,
                risk_amount=round(risk_amount, 2),
                risk_pct=round(risk_pct_used, 3),
            )

            return RiskDecision(
                decision      = RiskDecisionType.APPROVED,
                reason        = "OK",
                size          = size,
                sl            = sl,
                tp            = tp,
                risk_amount   = risk_amount,
                risk_pct_used = risk_pct_used,
            )

    def on_fill(
        self,
        position_id: str,
        direction:   str,
        entry:       float,
        sl:          float,
        tp:          float,
        size:        float,
        setup_name:  str   = "",
        instrument:  str   = "",
        atr:         float = 0.0,
    ) -> Position:
        """
        Enregistre une position apres confirmation du broker.
        Appele par order_executor.py apres fill confirme.
        """
        sl_pts      = abs(entry - sl)
        risk_amount = sl_pts * size * self._config.point_value

        pos = Position(
            position_id  = position_id,
            setup_name   = setup_name,
            instrument   = instrument,
            direction    = direction,
            entry_price  = entry,
            stop_loss    = sl,
            take_profit  = tp,
            current_sl   = sl,
            size         = size,
            risk_pts     = sl_pts,
            risk_amount  = risk_amount,
            open_time    = datetime.now(tz=timezone.utc),
            atr_at_entry = atr,
        )

        with self._lock:
            self._open[position_id] = pos

        logger.info(
            "Position ouverte",
            **{k: v for k, v in pos.to_dict().items()
               if k in ("id","setup","direction","entry","sl","tp","size","risk_amount")}
        )
        return pos

    def on_close(
        self,
        position_id: str,
        exit_price:  float,
        reason:      str = "TP_HIT",
    ) -> Optional[Position]:
        """
        Enregistre la fermeture d'une position et met a jour le P&L.
        Appele par order_executor.py apres cloture confirmee.
        """
        with self._lock:
            pos = self._open.pop(position_id, None)
            if pos is None:
                logger.warning("on_close: position inconnue", id=position_id)
                return None

            pts = (exit_price - pos.entry_price
                   if pos.direction == "LONG"
                   else pos.entry_price - exit_price)
            pnl = pts * pos.size * self._config.point_value

            pos.exit_price   = exit_price
            pos.realized_pnl = pnl
            pos.close_time   = datetime.now(tz=timezone.utc)
            pos.status       = (PositionStatus.STOPPED
                                if reason == "SL_HIT"
                                else PositionStatus.CLOSED)

            self._realized_pnl       += pnl
            self._current_equity     += pnl
            self._closed_today.append(pos)

            # Verifier si le drawdown journalier est maintenant depasse
            self._check_daily_drawdown()

            logger.info(
                "Position fermee",
                id=position_id, reason=reason,
                exit=round(exit_price, 2),
                pts=round(pts, 2), pnl=round(pnl, 2),
                session_pnl=round(self._realized_pnl, 2),
                equity=round(self._current_equity, 2),
            )

        return pos

    def update_trailing_stop(
        self,
        position_id:   str,
        current_price: float,
    ) -> Optional[float]:
        """
        Calcule et applique le trailing stop et le break-even.
        Appele sur chaque tick de prix.

        Returns:
            Nouveau SL si modifie, None sinon.
            L'appelant doit envoyer un ordre de modification au broker
            si la valeur retournee est non-None.
        """
        with self._lock:
            pos = self._open.get(position_id)
            if pos is None:
                return None

            cfg = self._config
            atr = pos.atr_at_entry
            if atr <= 0:
                return None

            gain_pts    = pos.unrealized_pts(current_price)
            new_sl:     Optional[float] = None

            if pos.direction == "LONG":
                # Break-even
                if (not pos.be_activated
                        and gain_pts >= atr * cfg.be_trigger_mult):
                    candidate = pos.entry_price + cfg.be_buffer_pts
                    if candidate > pos.current_sl:
                        new_sl           = candidate
                        pos.current_sl   = candidate
                        pos.be_activated = True
                        logger.info("Break-even active",
                                    id=position_id,
                                    new_sl=round(candidate, 2),
                                    gain_pts=round(gain_pts, 2))

                # Trailing stop
                if gain_pts >= atr * cfg.trail_trigger_mult:
                    candidate = current_price - atr * cfg.trail_distance_mult
                    if candidate > pos.current_sl:
                        new_sl              = candidate
                        pos.current_sl      = candidate
                        pos.trail_activated = True
                        logger.debug("Trailing stop mis a jour",
                                     id=position_id,
                                     new_sl=round(candidate, 2),
                                     current=round(current_price, 2))

            else:  # SHORT
                # Break-even
                if (not pos.be_activated
                        and gain_pts >= atr * cfg.be_trigger_mult):
                    candidate = pos.entry_price - cfg.be_buffer_pts
                    if candidate < pos.current_sl:
                        new_sl           = candidate
                        pos.current_sl   = candidate
                        pos.be_activated = True
                        logger.info("Break-even active SHORT",
                                    id=position_id,
                                    new_sl=round(candidate, 2))

                # Trailing stop
                if gain_pts >= atr * cfg.trail_trigger_mult:
                    candidate = current_price + atr * cfg.trail_distance_mult
                    if candidate < pos.current_sl:
                        new_sl              = candidate
                        pos.current_sl      = candidate
                        pos.trail_activated = True

        return new_sl

    def activate_kill_switch(
        self,
        reason: KillSwitchReason = KillSwitchReason.MANUAL,
    ) -> None:
        """
        Active le kill switch. Aucun nouveau trade ne sera autorise.
        Les positions ouvertes doivent etre fermees manuellement ou
        par le strategy_engine.
        """
        with self._lock:
            if not self._kill_switch_active:
                self._kill_switch_active  = True
                self._kill_switch_reason  = reason
                logger.critical(
                    "KILL SWITCH ACTIVE",
                    reason=reason.value,
                    open_positions=len(self._open),
                    realized_pnl=round(self._realized_pnl, 2),
                    equity=round(self._current_equity, 2),
                )

    def deactivate_kill_switch(self) -> None:
        """
        Desactive le kill switch.
        ATTENTION : uniquement si le kill switch a ete active manuellement.
        Un kill switch declenche par drawdown ne se desactive pas seul.
        """
        with self._lock:
            if self._kill_switch_reason == KillSwitchReason.DAILY_DRAWDOWN:
                logger.warning(
                    "Impossible de desactiver : kill switch declenche "
                    "par drawdown. Attendre reset_session()."
                )
                return
            self._kill_switch_active = False
            self._kill_switch_reason = None
            logger.info("Kill switch desactive")

    def reset_session(
        self,
        new_equity: Optional[float] = None,
    ) -> None:
        """
        Remet a zero les compteurs de session.
        Appeler au debut de chaque nouvelle journee de trading.

        Args:
            new_equity: equity de debut de session (ex: solde du compte apres cloture)
                        Si None, utilise l'equity courante.
        """
        with self._lock:
            equity = new_equity if new_equity is not None else self._current_equity

            old_date = self._session_date
            self._session_date        = date.today()
            self._start_equity        = equity
            self._current_equity      = equity
            self._realized_pnl        = 0.0
            self._closed_today.clear()

            # Remettre le kill switch a zero si declenche par drawdown
            if self._kill_switch_reason == KillSwitchReason.DAILY_DRAWDOWN:
                self._kill_switch_active = False
                self._kill_switch_reason = None

            logger.info(
                "Session remise a zero",
                old_date=old_date.isoformat(),
                new_date=self._session_date.isoformat(),
                equity=round(equity, 2),
            )

    def get_metrics(self, current_prices: Optional[Dict[str, float]] = None) -> RiskMetrics:
        """
        Retourne un snapshot complet de l'etat du risk manager.

        Args:
            current_prices: dict {position_id: current_price} pour le P&L flottant.
                            Si None, le P&L flottant est 0.
        """
        with self._lock:
            unrealized = 0.0
            if current_prices:
                for pid, pos in self._open.items():
                    price = current_prices.get(pid)
                    if price:
                        unrealized += pos.unrealized_pnl(price)

            total_pnl  = self._realized_pnl + unrealized
            dd_pct     = self._daily_drawdown_pct()
            dd_remain  = max(0.0, self._config.max_daily_dd_pct - dd_pct)

            wins    = sum(1 for p in self._closed_today
                          if p.realized_pnl is not None and p.realized_pnl > 0)
            losses  = sum(1 for p in self._closed_today
                          if p.realized_pnl is not None and p.realized_pnl <= 0)

            return RiskMetrics(
                session_date          = self._session_date,
                session_start_equity  = self._start_equity,
                current_equity        = self._current_equity + unrealized,
                realized_pnl          = self._realized_pnl,
                unrealized_pnl        = unrealized,
                total_pnl             = total_pnl,
                daily_dd_pct          = dd_pct,
                daily_dd_limit        = self._config.max_daily_dd_pct,
                dd_remaining_pct      = dd_remain,
                open_positions        = len(self._open),
                max_open_positions    = self._config.max_open_trades,
                closed_today          = len(self._closed_today),
                wins_today            = wins,
                losses_today          = losses,
                kill_switch_active    = self._kill_switch_active,
                kill_switch_reason    = (self._kill_switch_reason.value
                                         if self._kill_switch_reason else None),
            )

    # ---------------------------------------------------------------
    # Proprietes en lecture
    # ---------------------------------------------------------------

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def open_positions(self) -> Dict[str, Position]:
        with self._lock:
            return dict(self._open)

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def current_equity(self) -> float:
        return self._current_equity

    def get_position(self, position_id: str) -> Optional[Position]:
        with self._lock:
            return self._open.get(position_id)

    # ---------------------------------------------------------------
    # Utilitaires internes
    # ---------------------------------------------------------------

    def _check_pre_conditions(self, spread: float) -> Optional[RiskDecision]:
        """Verifications rapides avant tout calcul. None = OK."""
        # Kill switch
        if self._kill_switch_active:
            return RiskDecision(
                decision = RiskDecisionType.KILL_SWITCH,
                reason   = f"KILL_SWITCH:{(self._kill_switch_reason.value if self._kill_switch_reason else 'ACTIF')}",
            )

        # Drawdown journalier
        dd_pct = self._daily_drawdown_pct()
        if dd_pct >= self._config.max_daily_dd_pct:
            self.activate_kill_switch(KillSwitchReason.DAILY_DRAWDOWN)
            return RiskDecision(
                decision = RiskDecisionType.KILL_SWITCH,
                reason   = f"DAILY_DRAWDOWN {dd_pct:.2f}% >= limite {self._config.max_daily_dd_pct:.2f}%",
            )

        # Objectif journalier atteint
        if (self._config.daily_target_pct > 0
                and self._realized_pnl >= self._start_equity * self._config.daily_target_pct / 100):
            return RiskDecision(
                decision = RiskDecisionType.REJECTED,
                reason   = f"DAILY_TARGET_REACHED pnl={self._realized_pnl:.2f}",
            )

        # Nombre max de positions
        if len(self._open) >= self._config.max_open_trades:
            return RiskDecision(
                decision = RiskDecisionType.REJECTED,
                reason   = f"MAX_OPEN_TRADES {len(self._open)}/{self._config.max_open_trades}",
            )

        # Spread
        if spread > self._config.max_spread_pts:
            return RiskDecision(
                decision = RiskDecisionType.REJECTED,
                reason   = f"SPREAD_TROP_LARGE {spread:.2f} > {self._config.max_spread_pts:.2f}",
            )

        return None

    def _validate_geometry(
        self, direction: str, entry: float, sl: float, tp: float
    ) -> Optional[RiskDecision]:
        """Verifie la coherence geometrique du signal. None = OK."""
        if direction == "LONG":
            if sl >= entry:
                return RiskDecision(
                    decision=RiskDecisionType.REJECTED,
                    reason=f"SL_INVALIDE_LONG sl={sl:.2f} >= entry={entry:.2f}",
                )
            if tp <= entry:
                return RiskDecision(
                    decision=RiskDecisionType.REJECTED,
                    reason=f"TP_INVALIDE_LONG tp={tp:.2f} <= entry={entry:.2f}",
                )
        elif direction == "SHORT":
            if sl <= entry:
                return RiskDecision(
                    decision=RiskDecisionType.REJECTED,
                    reason=f"SL_INVALIDE_SHORT sl={sl:.2f} <= entry={entry:.2f}",
                )
            if tp >= entry:
                return RiskDecision(
                    decision=RiskDecisionType.REJECTED,
                    reason=f"TP_INVALIDE_SHORT tp={tp:.2f} >= entry={entry:.2f}",
                )
        else:
            return RiskDecision(
                decision=RiskDecisionType.REJECTED,
                reason=f"DIRECTION_INCONNUE: {direction}",
            )
        return None

    def _calc_size(self, sl_pts: float) -> tuple[float, float]:
        """
        Calcule la taille de position et le montant risque.

        Formule :
            risk_amount = equity * risk_pct / 100
            size = risk_amount / (sl_pts * point_value)
            size = arrondi vers le bas selon size_step (granularite IG)
            size = min(size, max_position_size) si plafond defini
            size >= size_step (taille min IG)

        Returns:
            (size, risk_amount) - size=0 si calcul impossible
        """
        risk_amount = self._current_equity * self._config.risk_pct / 100
        denom       = sl_pts * self._config.point_value

        if denom <= 0:
            return 0.0, 0.0

        size_raw = risk_amount / denom

        # Granularite : 0 = entier (legacy), sinon multiple de size_step
        step = self._config.size_step
        if step and step > 0:
            size_snap = math.floor(size_raw / step) * step
            # arrondi pour eviter les flottants 0.30000000000004
            size_snap = round(size_snap, 4)
            min_size = step
        else:
            size_snap = math.floor(size_raw)
            min_size = 1.0

        # Plafond dur
        if self._config.max_position_size and self._config.max_position_size > 0:
            size_snap = min(size_snap, self._config.max_position_size)

        # Plancher : si on est sous le minimum IG, verifier qu'1 lot minimum est acceptable
        if size_snap < min_size:
            risk_one_min = min_size * sl_pts * self._config.point_value
            max_acceptable = self._current_equity * self._config.max_capital_pct / 100
            if risk_one_min <= max_acceptable:
                size_snap   = min_size
                risk_amount = risk_one_min
            else:
                # Rejet rendu VISIBLE : 1 lot minimum risquerait plus que le
                # plafond autorise. Frequent sur DAX/CAC40 (min 0.5 lot) des que
                # SL est large. Pour trader ces instruments avec des SL typiques,
                # relever max_capital_pct au-dessus de risk_pct dans le .env.
                logger.warning(
                    "TAILLE_NULLE : 1 lot min ({:.2f}) risque {:.2f} EUR > plafond "
                    "{:.2f} EUR (sl={:.1f}pts, equity={:.2f}) -> signal rejete".format(
                        min_size, risk_one_min, max_acceptable,
                        sl_pts, self._current_equity,
                    )
                )
                return 0.0, 0.0

        actual_risk = size_snap * sl_pts * self._config.point_value
        return float(size_snap), actual_risk

    def _daily_drawdown_pct(self) -> float:
        """Calcule le drawdown journalier en %."""
        if self._start_equity <= 0:
            return 0.0
        loss = self._start_equity - self._current_equity
        return max(0.0, loss / self._start_equity * 100)

    def _check_daily_drawdown(self) -> None:
        """Verifie et declenche le kill switch si drawdown depasse."""
        dd = self._daily_drawdown_pct()
        if dd >= self._config.max_daily_dd_pct and not self._kill_switch_active:
            logger.critical(
                "Drawdown journalier depasse",
                dd_pct=round(dd, 2),
                limit_pct=self._config.max_daily_dd_pct,
                equity=round(self._current_equity, 2),
            )
            # Note: on appelle sans le lock car on est deja dans on_close()
            self._kill_switch_active = True
            self._kill_switch_reason = KillSwitchReason.DAILY_DRAWDOWN


# -----------------------------------------------------------------------
# Fonctions utilitaires standalone
# -----------------------------------------------------------------------

def calc_sl_atr(
    entry:      float,
    atr:        float,
    direction:  str,
    multiplier: float = 1.5,
) -> float:
    """
    Calcule un stop loss base sur l'ATR.
    Equivalent de la formule utilisee dans kasper_setups.py.
    """
    dist = atr * multiplier
    return entry - dist if direction == "LONG" else entry + dist


def calc_tp_rr(
    entry:    float,
    sl:       float,
    rr:       float = 2.0,
    direction: str  = "LONG",
) -> float:
    """Calcule un take profit base sur un ratio R:R."""
    risk = abs(entry - sl)
    return entry + rr * risk if direction == "LONG" else entry - rr * risk


def new_position_id(setup_name: str = "") -> str:
    """Genere un ID unique pour une position."""
    prefix = setup_name[:6].upper().replace(" ", "_") if setup_name else "POS"
    return f"{prefix}_{uuid.uuid4().hex[:8].upper()}"
