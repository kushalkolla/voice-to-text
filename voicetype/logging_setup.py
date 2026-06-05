"""Central logging setup: console + a rotating file under ``logs/``."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LOG_DIR

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with a console and a rotating log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers if called twice.
    if root.handlers:
        return

    # Under pythonw.exe (no console) sys.stderr is None — skip the console
    # handler so it doesn't error on every record; the file log still works.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_FMT))
        root.addHandler(console)

    try:
        logfile: Path = LOG_DIR / "voicetype.log"
        fileh = RotatingFileHandler(
            logfile, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fileh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fileh)
    except Exception:  # noqa: BLE001 - logging to file is best-effort
        root.warning("Could not open log file; logging to console only.")
