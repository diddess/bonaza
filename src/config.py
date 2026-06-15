"""
config.py — Chargement sécurisé de la configuration Bonaza
Ne jamais mettre de valeurs en dur dans ce fichier.
Toutes les valeurs viennent du fichier .env
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv
from loguru import logger

# Chemin racine du projet (2 niveaux au-dessus de config.py)
BONAZA_ROOT = Path(__file__).parent.parent.resolve()
ENV_FILE = BONAZA_ROOT / ".env"


def _load_env() -> None:
    """Charge le fichier .env. Avertissement si absent."""
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
        logger.info(f"Config chargée depuis {ENV_FILE}")
    else:
        logger.warning(
            f".env introuvable à {ENV_FILE}. "
            "Copie .env.example vers .env et remplis tes clés."
        )


@dataclass
class IGConfig:
    """Paramètres de connexion IG Markets."""
    api_key:      str = field(default_factory=lambda: os.getenv("IG_API_KEY", ""))
    identifier:   str = field(default_factory=lambda: os.getenv("IG_IDENTIFIER", ""))
    password:     str = field(default_factory=lambda: os.getenv("IG_PASSWORD", ""))
    account_type: str = field(default_factory=lambda: os.getenv("IG_ACCOUNT_TYPE", "DEMO"))
    account_id:   str = field(default_factory=lambda: os.getenv("IG_ACCOUNT_ID", ""))

    def is_valid(self) -> bool:
        """Vérifie que les credentials sont renseignés."""
        return bool(self.api_key and self.identifier and self.password)

    def __repr__(self) -> str:
        # Ne jamais logger le mot de passe complet
        masked_key = self.api_key[:4] + "****" if self.api_key else "NON_CONFIGURE"
        return (
            f"IGConfig(api_key={masked_key}, "
            f"identifier={self.identifier}, "
            f"account_type={self.account_type})"
        )


@dataclass
class TradingConfig:
    """Paramètres de trading et gestion du risque."""
    mode:            str   = field(default_factory=lambda: os.getenv("BONAZA_MODE", "PAPER"))
    max_capital_pct: float = field(default_factory=lambda: float(os.getenv("BONAZA_MAX_CAPITAL_PCT", "1.0")))
    max_daily_dd_pct: float = field(default_factory=lambda: float(os.getenv("BONAZA_MAX_DAILY_DD_PCT", "3.0")))
    kill_switch:     bool  = field(default_factory=lambda: os.getenv("BONAZA_KILL_SWITCH", "FALSE").upper() == "TRUE")
    # Plafond dur sur la taille de position (en lots). None = pas de plafond.
    max_position_size: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_SIZE", "0") or "0"))

    def is_live(self) -> bool:
        return self.mode.upper() == "LIVE"

    def is_paper(self) -> bool:
        return self.mode.upper() == "PAPER"


@dataclass
class DBConfig:
    """Configuration base de données."""
    path: str = field(
        default_factory=lambda: os.getenv(
            "BONAZA_DB_PATH",
            str(BONAZA_ROOT / "data" / "bonaza.db")
        )
    )

    @property
    def url(self) -> str:
        return f"sqlite:///{self.path}"


@dataclass
class LogConfig:
    """Configuration des logs."""
    level: str = field(default_factory=lambda: os.getenv("BONAZA_LOG_LEVEL", "INFO"))
    path:  str = field(
        default_factory=lambda: os.getenv(
            "BONAZA_LOG_PATH",
            str(BONAZA_ROOT / "logs")
        )
    )


@dataclass
class AgentConfig:
    """Configuration de l'agent IA Claude (scalping XAUUSD)."""
    enabled:         bool  = field(default_factory=lambda: os.getenv("AI_AGENT_ENABLED", "false").lower() == "true")
    api_key:         str   = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    model:           str   = field(default_factory=lambda: os.getenv("AI_AGENT_MODEL", "claude-opus-4-7"))
    interval_sec:    int   = field(default_factory=lambda: int(os.getenv("AI_AGENT_INTERVAL_SEC", "60")))
    max_trades_h:    int   = field(default_factory=lambda: int(os.getenv("AI_AGENT_MAX_TRADES_PER_HOUR", "3")))
    min_rr:          float = field(default_factory=lambda: float(os.getenv("AI_AGENT_MIN_RR", "1.5")))
    instrument:      str   = field(default_factory=lambda: os.getenv("AI_AGENT_INSTRUMENT", "XAUUSD"))
    # Effort Claude : low / medium / high / xhigh / max
    effort:          str   = field(default_factory=lambda: os.getenv("AI_AGENT_EFFORT", "medium"))
    # Plage horaire UTC pendant laquelle l'agent appelle Claude.
    # Hors plage : tick skip (pas de gaspillage tokens). 0/0 ou -1/-1 = 24/24.
    session_start_h: int   = field(default_factory=lambda: int(os.getenv("AI_AGENT_SESSION_START", "-1")))
    session_end_h:   int   = field(default_factory=lambda: int(os.getenv("AI_AGENT_SESSION_END",   "-1")))
    # Cooldown : si N SL consecutifs aujourd'hui UTC, skip jusqu'au prochain jour UTC.
    # 0 = desactive. Defaut 3 (decision 27/05 apres -27.67 EUR sur 22 trades).
    sl_cooldown_threshold: int = field(default_factory=lambda: int(os.getenv("AI_AGENT_SL_COOLDOWN", "3")))
    # Filtre M15 strict : skip SHORT si M15 ABOVE EMA20 + slope UP (et inverse pour LONG).
    # Decision 28/05 apres analyse proxy C : auraient sauve +13.93 EUR sur 27/05.
    m15_filter_enabled: bool = field(default_factory=lambda: os.getenv("AI_AGENT_M15_FILTER", "true").lower() == "true")
    # Tente temperature=0 (deterministe). Fallback si incompatible avec thinking.
    temperature_zero: bool = field(default_factory=lambda: os.getenv("AI_AGENT_TEMPERATURE_ZERO", "true").lower() == "true")
    # Log detaille snapshot+response+thinking dans /app/data/ai_decisions.jsonl
    log_decisions: bool = field(default_factory=lambda: os.getenv("AI_AGENT_LOG_DECISIONS", "true").lower() == "true")

    def is_ready(self) -> bool:
        """Vrai si l'agent est activable (clé + activation explicite)."""
        return self.enabled and bool(self.api_key)


@dataclass
class SecurityConfig:
    """Double verrou anti-live trading."""
    allow_live:      bool  = field(default_factory=lambda: os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true")
    confirm_live:    str   = field(default_factory=lambda: os.getenv("CONFIRM_LIVE_TRADING", ""))

    @property
    def live_authorized(self) -> bool:
        """Live nécessite ALLOW_LIVE_TRADING=true ET CONFIRM_LIVE_TRADING='I_UNDERSTAND_THE_RISK'."""
        return self.allow_live and self.confirm_live == "I_UNDERSTAND_THE_RISK"


@dataclass
class BonazaConfig:
    """Configuration globale agrégée."""
    ig:       IGConfig       = field(default_factory=IGConfig)
    trading:  TradingConfig  = field(default_factory=TradingConfig)
    db:       DBConfig       = field(default_factory=DBConfig)
    logs:     LogConfig      = field(default_factory=LogConfig)
    agent:    AgentConfig    = field(default_factory=AgentConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


# Singleton : chargé une seule fois à l'import
_load_env()
config = BonazaConfig()


# --- Validation au démarrage ---
def validate_config() -> bool:
    """
    Valide la configuration au démarrage.
    Retourne True si tout est OK, False sinon.
    Lève une RuntimeError en mode LIVE si des paramètres critiques manquent.
    """
    issues = []

    if not config.ig.is_valid():
        issues.append("Credentials IG Markets incomplets (vérifier .env)")

    if config.trading.kill_switch:
        logger.warning("KILL SWITCH ACTIVÉ — Aucun trade ne sera exécuté.")

    if config.trading.is_live():
        if issues:
            logger.error("Mode LIVE avec configuration invalide !")
            for issue in issues:
                logger.error(f"  - {issue}")
            raise RuntimeError("Configuration invalide pour le mode LIVE. Aucun trade exécuté.")
        logger.warning("Mode LIVE actif — Trades réels seront envoyés !")
    else:
        logger.info(f"Mode PAPER — Simulation uniquement ({config.ig.account_type})")
        for issue in issues:
            logger.warning(f"  Config incomplète : {issue}")

    logger.info(f"Config trading : max {config.trading.max_capital_pct}% capital/trade, "
                f"max {config.trading.max_daily_dd_pct}% drawdown journalier")

    return len(issues) == 0


if __name__ == "__main__":
    # Test rapide
    print(f"IG     : {config.ig}")
    print(f"Trading: {config.trading}")
    print(f"DB     : {config.db.url}")
    print(f"Logs   : {config.logs.level}")
    validate_config()
