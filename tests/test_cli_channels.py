"""`pa channels`: who may use the chat channels, and checking that Telegram is reachable."""

from __future__ import annotations

import httpx
import pytest

from helpers import invoke
from personal_agent.channels.telegram import channel as telegram_channel
from personal_agent.channels.telegram.api import TelegramApi
from personal_agent.config import Config, TelegramConfig
from telegram_fake import TOKEN, Fail, FakeTelegram


def flat(output: str) -> str:
    return " ".join(output.split())


@pytest.fixture
def ready(wired, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def go(**telegram):
        return wired(config=Config(telegram=TelegramConfig(**telegram)))

    return go


# ---- the overview -----------------------------------------------------------------------------
def test_the_overview_says_telegram_is_off_and_how_to_turn_it_on(ready):
    ready()
    out = flat(invoke("channels").output)
    assert "telegram" in out and "off" in out and "enabled = true" in out


def test_switched_on_without_a_token_the_overview_says_what_is_missing(ready):
    ready(enabled=True)
    assert "TELEGRAM_BOT_TOKEN is not set" in flat(invoke("channels").output)


def test_switched_on_with_a_token_the_overview_says_on(ready, monkeypatch):
    ready(enabled=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    out = flat(invoke("channels").output)
    assert " on " in out and "not set" not in out
    assert TOKEN not in out  # the value never leaves the environment


def test_the_overview_counts_approved_accounts_and_flags_waiting_ones(ready):
    app = ready(enabled=True)
    app.pairing.allow("telegram", "7")
    app.pairing.request("telegram", "99", "Eve")
    out = flat(invoke("channels").output)
    assert "pa channels pending" in out


# ---- pairing ----------------------------------------------------------------------------------
def test_pending_lists_who_is_waiting_and_with_which_code(ready):
    app = ready()
    request = app.pairing.request("telegram", "99", "Eve")
    out = flat(invoke("channels", "pending").output)
    assert request.code in out and "Eve" in out and "telegram" in out
    assert "your own phone" in out


def test_pending_says_when_nobody_is_waiting(ready):
    ready()
    assert "Nobody is waiting" in invoke("channels", "pending").output


def test_a_name_that_looks_like_markup_is_shown_as_typed(ready):
    app = ready()
    app.pairing.request("telegram", "99", "[bold red]Eve[/bold red]")
    out = invoke("channels", "pending").output
    assert "[bold red]Eve[/bold red]" in out


def test_approving_a_code_lets_the_account_in(ready):
    app = ready()
    request = app.pairing.request("telegram", "99", "Eve")
    result = invoke("channels", "approve", "telegram", request.code.lower().replace("-", ""))
    assert result.exit_code == 0 and "Approved Eve on telegram" in result.output
    assert app.pairing.is_approved("telegram", "99")
    assert "Eve" in invoke("channels", "senders").output


def test_a_code_that_does_not_exist_approves_nobody(ready):
    app = ready()
    app.pairing.request("telegram", "99")
    result = invoke("channels", "approve", "telegram", "ZZZZ-ZZZZ")
    assert result.exit_code == 1 and "No such code" in result.output
    assert not app.pairing.is_approved("telegram", "99")


def test_a_code_for_another_channel_is_not_accepted_here(ready):
    app = ready()
    request = app.pairing.request("web", "99")
    result = invoke("channels", "approve", "telegram", request.code)
    assert result.exit_code == 1 and "belongs to web" in result.output
    assert not app.pairing.is_approved("web", "99")
    assert app.pairing.peek(request.code) is not None  # still there for the right channel


def test_an_expired_code_cannot_be_approved(ready):
    app = ready()
    request = app.pairing.request("telegram", "99")
    app.clock.advance(7200)
    result = invoke("channels", "approve", "telegram", request.code)
    assert result.exit_code == 1 and "No such code" in result.output


def test_denying_discards_a_code(ready):
    app = ready()
    request = app.pairing.request("telegram", "99")
    assert invoke("channels", "deny", request.code).exit_code == 0
    assert app.pairing.peek(request.code) is None and not app.pairing.is_approved("telegram", "99")
    assert invoke("channels", "deny", request.code).exit_code == 1


def test_an_account_can_be_approved_by_id_without_a_code(ready):
    app = ready()
    result = invoke("channels", "allow", "Telegram", "12345", "--label", "me")
    assert result.exit_code == 0
    assert app.pairing.is_approved("telegram", "12345")
    assert "me" in invoke("channels", "senders").output


def test_senders_says_when_nobody_is_approved(ready):
    ready()
    assert "No account is approved" in invoke("channels", "senders").output


def test_revoking_takes_access_away(ready):
    app = ready()
    app.pairing.allow("telegram", "7")
    assert invoke("channels", "revoke", "telegram", "7").exit_code == 0
    assert not app.pairing.is_approved("telegram", "7")
    again = invoke("channels", "revoke", "telegram", "7")
    assert again.exit_code == 1 and "was not approved" in again.output


# ---- `pa channels test` --------------------------------------------------------------------
@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(
        telegram_channel,
        "TelegramApi",
        lambda token, **kw: TelegramApi(token, client=fake.client()),
    )
    return fake


def test_the_test_command_needs_a_token(ready, telegram):
    app = ready(enabled=True)
    result = invoke("channels", "test")
    assert result.exit_code == 1
    out = flat(result.output)
    assert "TELEGRAM_BOT_TOKEN is not set" in out and "Put TELEGRAM_BOT_TOKEN=..." in out
    assert "".join(str(app.paths.env_file).split()) in "".join(
        out.split()
    )  # rich may wrap a long path
    assert telegram.calls == []


def test_the_test_command_reports_the_bots_name_and_what_to_do_next(ready, telegram, monkeypatch):
    ready(enabled=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    result = invoke("channels", "test", "telegram")
    out = flat(result.output)
    assert result.exit_code == 0 and "the bot is @test_bot" in out
    assert "No account is approved yet" in out
    assert TOKEN not in result.output


def test_the_test_command_is_quiet_about_pairing_once_someone_is_approved(
    ready, telegram, monkeypatch
):
    app = ready(enabled=True)
    app.pairing.allow("telegram", "7")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert "No account is approved" not in invoke("channels", "test").output


def test_a_refused_token_is_reported_without_echoing_it(ready, telegram, monkeypatch):
    ready(enabled=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    telegram.script("getMe", Fail(401, "Unauthorized"))
    result = invoke("channels", "test")
    assert result.exit_code == 1 and "refused the token" in result.output
    assert TOKEN not in result.output


def test_a_network_failure_suggests_a_proxy(ready, telegram, monkeypatch):
    ready(enabled=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    telegram.script("getMe", httpx.ConnectError("blocked"))
    result = invoke("channels", "test")
    assert result.exit_code == 1 and "proxy" in flat(result.output)


def test_other_telegram_errors_are_shown(ready, telegram, monkeypatch):
    ready(enabled=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    telegram.script("getMe", Fail(502, "Bad Gateway"))
    result = invoke("channels", "test")
    assert result.exit_code == 1 and "502" in result.output


def test_there_is_no_test_for_other_channels(ready):
    ready()
    assert invoke("channels", "test", "web").exit_code == 2
