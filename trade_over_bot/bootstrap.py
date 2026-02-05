from __future__ import annotations
from pathlib import Path
import sys
from prog.utils.telegram import Telegram
import logging
from prog.proxy_server.proxy_driver import ProxyDriver


def prepare_paths(project_root: Path | None = None):
    """Compute and create project paths (LOG, STATE, CONFIG).

    Returns tuple: (project_root, log_dir, state_dir, config_dir)
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent

    global log_dir, state_dir, config_dir
    log_dir = project_root / "data" / "log"
    state_dir = project_root / "data" / "state"
    config_dir = project_root / "data" / "config"

    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

def setup_logger(config_tag: str, log_dir: Path) -> logging.Logger:
    logger = logging.getLogger(config_tag)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        "%d %H:%M:%S"
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    fh = logging.FileHandler(log_dir / f"{config_tag}.log", encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    websocket_logger = logging.getLogger("websocket")
    websocket_logger.setLevel(logging.WARNING)

    pybit_logger = logging.getLogger("pybit")
    pybit_logger.setLevel(logging.WARNING)

    return logger

def create_logger_telegram_driver(config_tag: str):
    """Create and return (logger, telegram, bybit_driver).

    This centralizes creation of logging, Telegram client and Bybit driver.
    """
    global logger, telegram, full_config_path, proxy_driver
    logger = setup_logger(config_tag, log_dir)

    telegram_config_path = config_dir / "telegram_config.txt"
    telegram = Telegram(logger=logger, config_path=telegram_config_path)

    proxy_driver = ProxyDriver(logger=logger)

    full_config_path = config_dir / f"{config_tag}.toml"


