"""Strategies prod-ready pour Bonaza (v2 - 30/05/2026).

Strategies validees walk-forward + sensitivity + filtre volume sur XAUUSD/CAC40/DAX M1 :
  - S8_RegimeAdaptive    : ADX regime switch (trend / range)         CAC40 M15
  - S5_ToDMomentum       : Time-of-day momentum (heures favorables)  XAUUSD M15
  - S3_ORB               : Opening Range Breakout + filtre volume    XAUUSD M15
  - S8_VolumeTrendDAX    : ADX trend + volume confirme               DAX M5     (NEW)

Portfolio combine (4 strats equal-weight, fees realistes 10% sizing) :
  Sharpe 2.62, DD 0.12%, ret 1.18%/an
  Vs v1 (3 strats) : Sharpe 2.10, DD 0.22%, ret 1.41%
  -> v2 ameliore ratio Sharpe/DD de 9.5 a 21.8 (+130%)
"""
from strategies.portfolio_runner import (
    S8RegimeAdaptive,
    S5TodMomentum,
    S3ORB,
    S8VolumeTrendDAX,
    PortfolioRunner,
    build_portfolio_runner,
)

__all__ = [
    "S8RegimeAdaptive",
    "S5TodMomentum",
    "S3ORB",
    "S8VolumeTrendDAX",
    "PortfolioRunner",
    "build_portfolio_runner",
]
