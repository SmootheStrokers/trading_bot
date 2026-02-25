"""
logger.py — Structured logging setup.
Configures ROOT logger so ALL module logs (position_manager, executor, etc.)
go to both console (terminal) and bot.log — dashboard and terminal show identical output.
"""

import logging
import sys
from typing import Optional

_ROOT_CONFIGURED = False


def setup_logger(name: str, log_file: Optional[str] = "bot.log") -> logging.Logger:
    """Setup logging. Configures root so every module writes to console + file."""
    global _ROOT_CONFIGURED
    root = logging.getLogger()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # One-time root configuration — ensures ALL modules log to file
    if not _ROOT_CONFIGURED:
        root.setLevel(logging.DEBUG)
        # Console (terminal / journald)
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        root.addHandler(console)
        # File (bot.log — dashboard reads this)
        if log_file:
            try:
                fh = logging.FileHandler(log_file)
                fh.setLevel(logging.DEBUG)
                fh.setFormatter(formatter)
                root.addHandler(fh)
            except Exception:
                pass
        _ROOT_CONFIGURED = True

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    # Don't add handlers to child loggers — they propagate to root
    return logger
