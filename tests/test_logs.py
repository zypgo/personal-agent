"""Logging: private, scrubbed, and quiet about the addresses the HTTP library visits."""

from __future__ import annotations

import logging

from helpers import our_log_handlers
from personal_agent.logs import LOGGER_NAME, setup_logging, trim, trim_logs
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


# ---- logs that nothing rotates ---------------------------------------------------------------------
def lines(count: int, width: int = 99) -> bytes:
    return b"".join(f"{i:0{width}d}\n".encode() for i in range(count))


def test_a_log_that_is_not_too_long_is_left_alone(tmp_path):
    log = tmp_path / "service.err.log"
    log.write_bytes(lines(10))
    assert trim(log, cap=1000, keep=200) == 0
    assert log.read_bytes() == lines(10)


def test_a_long_log_is_cut_back_to_its_end_from_the_start_of_a_line(tmp_path):
    log = tmp_path / "service.err.log"
    log.write_bytes(lines(100))  # 10,000 bytes
    dropped = trim(log, cap=5000, keep=1000)
    kept = log.read_bytes()
    assert dropped == 10_000 - len(kept) and 900 <= len(kept) <= 1000
    assert kept.endswith(lines(100)[-100:]) and kept.startswith(b"0")  # whole lines only
    assert set(kept.splitlines()) <= set(lines(100).splitlines())


def test_a_log_with_no_line_breaks_is_still_cut(tmp_path):
    log = tmp_path / "service.out.log"
    log.write_bytes(b"x" * 10_000)
    assert trim(log, cap=5000, keep=1000) == 9000 and log.read_bytes() == b"x" * 1000


def test_a_writer_that_keeps_appending_carries_on_at_the_new_end(tmp_path):
    """launchd and the MCP servers hold the file open for appending: no hole, no old length."""
    log = tmp_path / "service.err.log"
    with log.open("ab") as writer:
        writer.write(lines(100))
        writer.flush()
        trim(log, cap=5000, keep=1000)
        writer.write(b"after\n")
        writer.flush()
    assert log.read_bytes().endswith(b"99\n" + b"after\n") and b"\0" not in log.read_bytes()


def test_a_log_that_cannot_be_trimmed_is_not_an_error(tmp_path):
    assert trim(tmp_path / "missing.log", cap=1, keep=1) == 0


def test_the_agents_own_log_is_left_to_rotate_itself_and_the_rest_are_cut(paths):
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    for name in ("pa.log", "service.err.log", "mcp-files.log", "notes.txt"):
        (paths.logs_dir / name).write_bytes(lines(100))

    dropped = trim_logs(paths, cap=5000, keep=1000)

    assert set(dropped) == {"service.err.log", "mcp-files.log"}
    assert (paths.logs_dir / "pa.log").read_bytes() == lines(100)
    assert (paths.logs_dir / "notes.txt").read_bytes() == lines(100)
    assert len((paths.logs_dir / "service.err.log").read_bytes()) <= 1000
