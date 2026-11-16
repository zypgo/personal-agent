"""Logging: private, scrubbed, and quiet about the addresses the HTTP library visits."""

from __future__ import annotations

import logging

from helpers import our_log_handlers
from personal_agent.logs import LOGGER_NAME, setup_logging
from telegram_fake import TOKEN


def reset():
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_a_test_starts_without_the_log_handlers_an_earlier_one_left(paths):
    """The suite's own fixture removes them after every test (see tests/conftest.py); if it did not,
    this fails whenever some earlier test of the same process ran a command that opens a home."""
    assert our_log_handlers() == []


def test_the_http_libraries_do_not_log_the_addresses_they_visit(paths):
    # At INFO httpx logs "POST https://api.telegram.org/bot<TOKEN>/getUpdates".
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    try:
        setup_logging(paths)
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        reset()


def test_a_token_in_a_logged_address_never_reaches_the_file(paths):
    try:
        setup_logging(paths)
        logging.getLogger(LOGGER_NAME).warning(
            "request to https://api.telegram.org/bot%s/getMe failed", TOKEN
        )
        for handler in logging.getLogger(LOGGER_NAME).handlers:
            handler.flush()
        written = (paths.logs_dir / "pa.log").read_text()
    finally:
        reset()
    assert "request to https://api.telegram.org/bot" in written
    assert TOKEN not in written and "AAAAAAAAAAAAAAAAAAAA" not in written


def test_the_log_file_is_private(paths):
    import stat

    try:
        setup_logging(paths)
        assert stat.S_IMODE((paths.logs_dir / "pa.log").stat().st_mode) == 0o600
    finally:
        reset()
