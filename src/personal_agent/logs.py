"""Logging: a private rotating file, with secrets scrubbed from every line (tracebacks included).

Interactive commands keep the terminal clean (the user sees friendly messages instead); the
long-running service also logs to the console so `pa service logs` and launchd capture it.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from personal_agent.paths import Paths
from personal_agent.security.redact import known_secret_values, redact

LOGGER_NAME = "personal_agent"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
OWN_LOG = "pa.log"
"""The agent's own log. It rotates itself; the other files in the log directory do not."""
CAP_BYTES = 5_000_000
"""A log that nothing rotates is cut back once it is longer than this."""
KEEP_BYTES = 1_000_000
"""... to about this much of its end."""


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
        log_file = paths.logs_dir / OWN_LOG
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


def trim_logs(paths: Paths, *, cap: int = CAP_BYTES, keep: int = KEEP_BYTES) -> dict[str, int]:
    """Cut every log that nothing rotates back to its end, and say how much was dropped, by file.

    Those are what launchd keeps of the service's output (the same lines as `pa.log`, which would
    otherwise be the only one of the two with a limit) and what each MCP server says on its error
    stream. An agent that is meant to run for months must not fill the disk with them."""
    dropped: dict[str, int] = {}
    for path in sorted(paths.logs_dir.glob("*.log")):
        if path.name != OWN_LOG:
            lost = trim(path, cap=cap, keep=keep)
            if lost:
                dropped[path.name] = lost
    return dropped


def trim(path: Path, *, cap: int, keep: int) -> int:
    """`path` cut to roughly its last `keep` bytes, from the start of a line, if it is longer than
    `cap`; the number of bytes dropped.

    Done in the file itself: whatever is writing to it (launchd, an MCP server) holds it open for
    appending, and keeps appending to this file, which a rename or a new file would not be."""
    try:
        size = path.stat().st_size
        if size <= cap:
            return 0
        with path.open("r+b") as handle:
            handle.seek(size - keep)
            tail = handle.read()
            if b"\n" in tail:
                tail = tail.split(b"\n", 1)[1]
            handle.seek(0)
            handle.write(tail)
            handle.truncate()
        return size - len(tail)
    except OSError:
        return 0  # a log that cannot be trimmed now is tried again later; it is not a failure
