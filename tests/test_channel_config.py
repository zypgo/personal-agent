"""Configuration and small pieces of wiring that the chat channels rely on."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from helpers import timer
from personal_agent.config import Config, MediaConfig, NotifyConfig, TelegramConfig


# ---- [telegram] ----------------------------------------------------------------------------------
def test_telegram_is_off_by_default_and_reads_its_token_from_the_environment():
    cfg = TelegramConfig()
    assert cfg.enabled is False and cfg.token_env == "TELEGRAM_BOT_TOKEN"
    assert cfg.api_base == "https://api.telegram.org"


@pytest.mark.parametrize(
    "value", ["123456789:ABCdefGhIJKlmNoPQRsTUVwxyz", "not a name", "1ABC", "", "A-B", "A=B"]
)
def test_the_token_setting_must_be_a_variable_name_so_a_pasted_token_is_refused(value):
    with pytest.raises(ValidationError, match="NAME"):
        TelegramConfig(token_env=value)


def test_an_ordinary_variable_name_is_fine():
    assert TelegramConfig(token_env="MY_BOT_TOKEN").token_env == "MY_BOT_TOKEN"


@pytest.mark.parametrize(
    "base",
    [
        "https://api.telegram.org",
        "https://tg.example.com/",
        "http://127.0.0.1:8081",
        "http://localhost:8081/",
        "http://[::1]:9",
    ],
)
def test_the_api_address_may_be_https_or_this_machine(base):
    assert TelegramConfig(api_base=base).api_base == base.rstrip("/")


@pytest.mark.parametrize(
    "base",
    [
        "http://tg.example.com",  # the token would cross the network in the clear
        "http://192.168.1.5:8081",
        "ftp://example.com",
        "https://user:pass@tg.example.com",
        "api.telegram.org",
        "https://",
        "",
    ],
)
def test_other_api_addresses_are_refused(base):
    with pytest.raises(ValidationError):
        TelegramConfig(api_base=base)


def test_the_poll_length_stays_within_what_telegram_allows():
    assert TelegramConfig(poll_timeout_s=60).poll_timeout_s == 60
    for bad in (0, 61):
        with pytest.raises(ValidationError):
            TelegramConfig(poll_timeout_s=bad)


def test_unknown_telegram_settings_are_an_error_not_silently_ignored():
    with pytest.raises(ValidationError):
        TelegramConfig(tokn_env="X")


# ---- [media] -------------------------------------------------------------------------------------
def test_the_default_limits_are_within_what_the_services_accept():
    media = MediaConfig()
    assert media.images and media.voice
    assert media.max_voice_bytes <= 25_000_000  # the speech API's limit
    assert media.transcribe_model == "gpt-transcribe"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_voice_bytes", 26_000_000),
        ("images_in_context", 11),
        ("max_voice_seconds", 4),
        ("max_image_bytes", 10),
    ],
)
def test_limits_outside_the_sensible_range_are_refused(field, value):
    with pytest.raises(ValidationError):
        MediaConfig(**{field: value})


# ---- where unprompted messages go ----------------------------------------------------------------
def test_auto_is_the_default(make_app):
    assert NotifyConfig().channel == "auto"
    assert make_app().notify_channel() == "cli"  # nobody has used a chat app yet


def test_with_auto_the_owner_is_reached_on_the_chat_app_they_last_wrote_from(make_app):
    app = make_app(config=Config(notify=NotifyConfig(channel="auto")))
    assert app.notify_channel() == "cli"  # no history yet
    app.transcript.add_in("telegram", "hi")
    assert app.notify_channel() == "telegram"
    app.transcript.add_in("web", "from the browser")
    assert app.notify_channel() == "web"


def test_a_chat_in_the_terminal_does_not_pull_background_news_back_to_it(make_app):
    # The terminal is only reachable while it is open; the phone is not.
    app = make_app(config=Config(notify=NotifyConfig(channel="auto")))
    app.transcript.add_in("telegram", "hi")
    app.transcript.add_in("cli", "a quick question at the desk")
    assert app.notify_channel() == "telegram"


def test_a_named_channel_is_used_whatever_was_said_where(make_app):
    app = make_app(config=Config(notify=NotifyConfig(channel="telegram")))
    app.transcript.add_in("cli", "hi")
    assert app.notify_channel() == "telegram"


async def test_background_work_is_delivered_to_that_channel(make_app):
    from agents.testing import ScriptedModel, assistant_message, function_call

    from tooling import drain

    model = ScriptedModel(
        [
            [function_call("send_message", {"text": "Your brief is ready."}, call_id="s1")],
            [assistant_message("Told the owner.")],
        ]
    )
    app = make_app(model, config=Config(notify=NotifyConfig(channel="auto")))
    app.transcript.add_in("telegram", "hi")
    app.inbox.add(timer("brief", "Summarise my inbox."))
    await drain(app)
    [message] = app.outbox.list()
    assert (message.channel, message.recipient, message.text) == (
        "telegram",
        None,
        "Your brief is ready.",
    )


async def test_an_answer_goes_back_to_the_channel_and_chat_that_asked(make_app):
    from agents.testing import ScriptedModel, assistant_message

    from helpers import owner_message
    from tooling import drain

    app = make_app(
        ScriptedModel([[assistant_message("It is sunny.")]]),
        config=Config(notify=NotifyConfig(channel="cli")),
    )
    app.inbox.add(owner_message("weather?", source="telegram", recipient="777"))
    await drain(app)
    [message] = app.outbox.list()
    assert (message.channel, message.recipient) == ("telegram", "777")  # not the notify channel


# ---- the media folder ------------------------------------------------------------------------------
def test_the_media_folder_lives_in_the_agent_home_and_is_private(paths):
    import stat

    assert paths.media_dir == paths.home / "media"
    assert paths.media_dir.is_dir()
    assert stat.S_IMODE(paths.media_dir.stat().st_mode) == 0o700
