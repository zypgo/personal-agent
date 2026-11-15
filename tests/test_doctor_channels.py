"""What `pa doctor` says about chat channels."""

from __future__ import annotations

import shutil

import pytest

from personal_agent.channels.pairing import PairingStore
from personal_agent.clock import ManualClock
from personal_agent.db import open_database
from personal_agent.doctor import capability_checks
from personal_agent.paths import Paths


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


def checks(tmp_path, toml: str = "", *, approve: tuple[str, str] | None = None):
    paths = Paths.resolve(tmp_path / "home")
    paths.ensure()
    if toml:
        paths.config_file.write_text(toml)
    if approve:
        db = open_database(paths.db_file)
        PairingStore(db, ManualClock()).allow(*approve)
        db.close()
    return {c.name: c for c in capability_checks(paths)}


def ffmpeg(monkeypatch, present: bool):
    real = shutil.which

    def which(name, *args, **kw):
        if name == "ffmpeg":
            return "/usr/bin/ffmpeg" if present else None
        return real(name, *args, **kw)

    monkeypatch.setattr("shutil.which", which)


ON = "[telegram]\nenabled = true\n"


def test_a_channel_that_is_off_says_nothing(tmp_path):
    found = checks(tmp_path)
    assert "telegram" not in found and "voice notes" not in found and "notify channel" not in found


def test_switched_on_without_a_token_is_a_failure_that_says_where_to_put_it(tmp_path):
    check = checks(tmp_path, ON)["telegram"]
    assert check.status == "fail"
    assert "TELEGRAM_BOT_TOKEN" in check.detail and "BotFather" in check.fix
    assert ".env" in check.fix


def test_a_token_without_an_approved_account_is_a_warning_that_says_what_to_do(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    check = checks(tmp_path, ON)["telegram"]
    assert check.status == "warn" and "no Telegram account is approved" in check.detail
    assert "pa channels approve telegram" in check.fix


def test_an_approved_account_makes_it_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    check = checks(tmp_path, ON, approve=("telegram", "7"))["telegram"]
    assert check.status == "ok" and "1 account(s) approved" in check.detail


def test_accounts_approved_on_another_channel_do_not_count(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    check = checks(tmp_path, ON, approve=("web", "7"))["telegram"]
    assert check.status == "warn"


def test_a_proxy_is_mentioned_but_never_its_password(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    toml = ON + 'proxy = "socks5://user:hunter2@127.0.0.1:1080"\n'
    check = checks(tmp_path, toml, approve=("telegram", "7"))["telegram"]
    assert "127.0.0.1:1080" in check.detail
    assert "hunter2" not in check.detail and "user" not in check.detail


def test_voice_notes_need_ffmpeg_and_the_check_says_so(tmp_path, monkeypatch):
    ffmpeg(monkeypatch, present=False)
    check = checks(tmp_path, ON)["voice notes"]
    assert check.status == "warn" and "brew install ffmpeg" in check.fix


def test_no_ffmpeg_warning_when_it_is_installed_or_voice_is_off(tmp_path, monkeypatch):
    ffmpeg(monkeypatch, present=True)
    assert "voice notes" not in checks(tmp_path, ON)
    ffmpeg(monkeypatch, present=False)
    assert "voice notes" not in checks(tmp_path / "other", ON + "\n[media]\nvoice = false\n")


def test_a_notify_channel_that_will_never_run_is_flagged(tmp_path):
    check = checks(tmp_path, '[notify]\nchannel = "telegram"\n')["notify channel"]
    assert check.status == "warn" and "'telegram'" in check.detail


@pytest.mark.parametrize("channel", ["auto", "cli"])
def test_the_built_in_notify_choices_are_fine(tmp_path, channel):
    assert "notify channel" not in checks(tmp_path, f'[notify]\nchannel = "{channel}"\n')


def test_notifying_through_telegram_is_fine_once_telegram_is_on(tmp_path):
    assert "notify channel" not in checks(tmp_path, ON + '\n[notify]\nchannel = "telegram"\n')
