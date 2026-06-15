"""
logger_setup.py — Configuration centralisée des logs Bonaza
Utilise loguru pour des logs structurés, colorés et rotatifs.
"""
import sys
from pathlib import Path
from loguru import logger


def setup_logger(log_level: str = "INFO", log_path: str | None = None) -> None:
    """
    Configure loguru pour Bonaza.
    - Console : coloré, lisible
    - Fichier : JSON structuré, rotation quotidienne, rétention 30 jours
    """
    # Supprime le handler par défaut
    logger.remove()

    # Handler console — format lisible
    logger.add(
        sys.stderr,
        level=log_level,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
    )

    # Handler fichier — JSON structuré, rotation journalière
    if log_path:
        log_dir = Path(log_path)
        log_dir.mkdir(parents=True, exist_ok=True)

        logger.add(
            str(log_dir / "bonaza_{time:YYYY-MM-DD}.log"),
            level=log_level,
            rotation="00:00",       # Nouveau fichier chaque jour à minuit
            retention="30 days",    # Garde 30 jours
            compression="zip",      # Compresse les anciens fichiers
            serialize=True,         # Format JSON pour analyse
            encoding="utf-8",
        )

        # Fichier erreurs séparé pour supervision
        logger.add(
            str(log_dir / "bonaza_errors.log"),
            level="ERROR",
            rotation="10 MB",
            retention="90 days",
            encoding="utf-8",
        )

        logger.info(f"Logs configurés — fichiers dans {log_dir}")


# Usage direct si lancé seul
if __name__ == "__main__":
    setup_logger("DEBUG", "C:/Claude/bonaza/logs")
    logger.debug("Message DEBUG")
    logger.info("Message INFO")
    logger.warning("Message WARNING")
    logger.error("Message ERROR")
    logger.success("Logger Bonaza opérationnel")
