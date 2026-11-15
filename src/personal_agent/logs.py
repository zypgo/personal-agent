"""Logging: a private rotating file, with secrets scrubbed from every line (tracebacks included).

Interactive commands keep the terminal clean (the user sees friendly messages instead); the
long-running service also logs to the console so `pa service logs` and launchd capture it.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from personal_agent.paths import Paths
from personal_agent.security.redact import known_secret_values, redact

LOGGER_NAME = "personal_agent"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), known_secret_values(os.environ))


def setup_logging(paths: Paths, *, console: bool = False, level: int = logging.INFO) -> None:
    """Idempotent: calling it again only adds a console handler if one is now wanted."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for noisy in ("httpx", "httpcore"):
        # At INFO, httpx logs every request address, and a Telegram address carries the bot token.
        logging.getLogger(noisy).setLevel(logging.WARNING)
    formatter = RedactingFormatter(_FORMAT)
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = paths.logs_dir / "pa.log"
        handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        try:
            log_file.chmod(0o600)
        except OSError:
            pass
    if console and not any(type(h) is logging.StreamHandler for h in logger.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)
