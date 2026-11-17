"""User-facing contracts: configuration, pairing, a non-sending probe, and notification routing."""

from __future__ import annotations

import httpx
import pytest

from helpers import invoke
from personal_agent.channels.dingtalk.api import OPEN_PATH, SEND_PATH, TOKEN_PATH, DingTalkApi
from personal_agent.channels.dingtalk.channel import DingTalkChannel
from personal_agent.channels.dingtalk.config import DingTalkConfig
from personal_agent.channels.registry import build_channels
from personal_agent.config import Config, NotifyConfig, TelegramConfig
from personal_agent.doctor import _channel_checks
from personal_agent.plugins.registry import ChannelSpec
from test_dingtalk_api import FakeDingTalk
from test_dingtalk_channel import OWNER


def flat(text):
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
    monkeypatch.delenv("DINGTALK_CLIENT_SECRET", raising=False)


@pytest.fixture
def configured(wired, monkeypatch):
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "offline-app")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "offline-secret")
    return wired(config=Config(dingtalk=DingTalkConfig(enabled=True)))


@pytest.fixture
def transport(monkeypatch):
    fake = FakeDingTalk()
    clients = []

    def api(client_id, secret):
        client = fake.client()
        clients.append(client)
        return DingTalkApi(client_id, secret, client=client)

    monkeypatch.setattr("personal_agent.channels.dingtalk.channel.DingTalkApi", api)
    yield fake
    assert all(client.is_closed for client in clients)


def test_disabled_by_default_and_can_be_enabled_without_loading_network_clients(make_app):
    assert not Config().dingtalk.enabled
    app = make_app()
    assert "dingtalk" not in build_channels(app).channels
    enabled = make_app(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    selected = build_channels(enabled)
    channel = selected.channels["dingtalk"]
    assert isinstance(channel, DingTalkChannel) and channel in selected.services
    assert channel._api is None and channel.health()["state"] == "stopped"


def test_overview_says_configured_not_live_and_counts_dingtalk_pairings(configured):
    app = configured
    app.pairing.allow("dingtalk", OWNER, "Phone")
    app.pairing.request("dingtalk", '["offline-app","corp","other"]', "Tablet")
    result = invoke("channels")
    output = flat(result.output)
    assert result.exit_code == 0
    assert "dingtalk on (configured; connection not probed) 1 1" in output
    assert "not live connectivity" in output and "pa channels pending" in output
    assert "offline-app" not in output and "offline-secret" not in output


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_secret_lists_variable_name_without_exposing_the_present_id(
    wired, monkeypatch, value
):
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "must-not-be-printed")
    if value is not None:
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", value)
    app = wired(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    result = invoke("channels")
    check = {c.name: c for c in _channel_checks(app.paths, app.config)}["dingtalk"]
    assert result.exit_code == 0 and "DINGTALK_CLIENT_SECRET" in result.output
    assert check.status == "fail" and "DINGTALK_CLIENT_SECRET" in check.detail
    assert "must-not-be-printed" not in result.output + repr(check)


def test_capabilities_show_the_supported_subset_without_network_or_plugin_execution(configured):
    def must_not_run(app):
        pytest.fail("listing declared capabilities must not execute plugin code")

    configured.plugin_channels["custom"] = ChannelSpec("custom", must_not_run, True, "example")
    result = invoke("channels", "capabilities")
    assert result.exit_code == 0
    dingtalk = next(line for line in result.output.splitlines() if "dingtalk" in line)
    assert all(word in dingtalk for word in ("text", "Markdown", "push"))
    assert all(
        word not in dingtalk for word in ("images", "voice", "buttons", "live answer", "files out")
    )
    assert "plugin-defined; metadata not inspected" in flat(result.output)
    assert "offline-secret" not in result.output


def test_cli_pairing_and_revocation_use_dingtalk_scope(configured):
    request = configured.pairing.request("dingtalk", OWNER, "Phone")
    wrong_channel = invoke("channels", "approve", "telegram", request.code)
    assert wrong_channel.exit_code == 1 and not configured.pairing.is_approved("dingtalk", OWNER)
    assert invoke("channels", "approve", "dingtalk", request.code).exit_code == 0
    assert configured.pairing.is_approved("dingtalk", OWNER)
    assert invoke("channels", "revoke", "dingtalk", OWNER).exit_code == 0
    assert not configured.pairing.is_approved("dingtalk", OWNER)


def test_probe_only_checks_gateway_auth_never_sends_or_prints_ticket(configured, transport):
    result = invoke("channels", "test", "dingtalk")
    output = flat(result.output)
    assert result.exit_code == 0
    assert "gateway authentication passed" in output
    assert "does not prove the daemon is connected" in output and "permission to send" in output
    assert "pa channels approve dingtalk CODE" in output
    assert len(transport.calls(OPEN_PATH)) == 1
    assert not transport.calls(SEND_PATH) and not transport.calls(TOKEN_PATH)
    for value in ("offline-ticket", "offline-app", "offline-secret", "wss://"):
        assert value not in result.output


def test_probe_without_keys_is_actionable_and_does_not_start_a_client(wired, transport):
    wired(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    result = invoke("channels", "test", "dingtalk")
    assert result.exit_code == 1 and "DINGTALK_CLIENT_SECRET" in result.output
    assert "DINGTALK_CLIENT_ID" in result.output and not transport.requests


@pytest.mark.parametrize(
    "result",
    [
        httpx.Response(
            401, json={"code": "offline-secret", "message": "offline-secret offline-ticket"}
        ),
        httpx.ConnectError("offline-secret offline-ticket"),
    ],
)
def test_probe_failure_hides_raw_service_or_transport_details(configured, transport, result):
    transport.script(OPEN_PATH, result)
    outcome = invoke("channels", "test", "dingtalk")
    assert outcome.exit_code == 1 and "DINGTALK.md" in outcome.output
    assert "offline-secret" not in outcome.output and "offline-ticket" not in outcome.output
    assert not transport.calls(SEND_PATH)


def test_doctor_distinguishes_credential_pairing_and_live_connectivity(configured):
    app = configured
    before = {c.name: c for c in _channel_checks(app.paths, app.config)}["dingtalk"]
    assert before.status == "warn" and "0 account(s) approved" in before.detail
    app.pairing.allow("dingtalk", OWNER)
    after = {c.name: c for c in _channel_checks(app.paths, app.config)}["dingtalk"]
    assert after.status == "ok" and "1 account(s) approved" in after.detail
    assert "connection not probed" in after.detail and "pa channels test dingtalk" in after.fix
    assert "offline-secret" not in repr(after)


def test_auto_notify_follows_the_recent_enabled_push_channel_not_just_configuration(make_app):
    app = make_app(
        config=Config(dingtalk=DingTalkConfig(enabled=True), telegram=TelegramConfig(enabled=True))
    )
    assert app.push_channels() == {"dingtalk", "telegram"} and app.notify_channel() == "cli"
    app.transcript.add_in("telegram", "earlier")
    app.clock.advance(1)
    app.transcript.add_in("dingtalk", "later")
    assert app.notify_channel() == "dingtalk"
    without_dingtalk = make_app(config=Config(telegram=TelegramConfig(enabled=True)))
    assert "dingtalk" not in without_dingtalk.push_channels()
    assert without_dingtalk.notify_channel() == "telegram"


def test_explicit_dingtalk_notify_setting_is_flagged_if_the_channel_is_disabled(make_app):
    app = make_app(config=Config(notify=NotifyConfig(channel="dingtalk")))
    assert {c.name: c for c in _channel_checks(app.paths, app.config)}[
        "notify channel"
    ].status == "warn"
    enabled = make_app(
        config=Config(
            notify=NotifyConfig(channel="dingtalk"), dingtalk=DingTalkConfig(enabled=True)
        )
    )
    assert "notify channel" not in {
        c.name: c for c in _channel_checks(enabled.paths, enabled.config)
    }
