"""What `pa doctor` says about chat channels."""

from __future__ import annotations

import shutil
import socket

import pytest

from personal_agent.channels.pairing import PairingStore
from personal_agent.channels.web.auth import WebAuth
from personal_agent.clock import ManualClock
from personal_agent.db import open_database
from personal_agent.doctor import capability_checks
from personal_agent.paths import Paths
from personal_agent.presence import LOCK_NAME, EngineLock


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)


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


def test_notifying_through_the_web_page_needs_it_switched_on(tmp_path):
    toml = '[notify]\nchannel = "web"\n'
    assert checks(tmp_path / "off", toml)["notify channel"].status == "warn"
    assert "notify channel" not in checks(tmp_path / "on", WEB + toml)


def test_a_notify_channel_that_may_come_from_a_plugin_is_flagged_differently(tmp_path):
    toml = '[notify]\nchannel = "signal"\n[plugins]\nenabled = ["signal-bridge"]\n'
    check = checks(tmp_path, toml)["notify channel"]
    assert check.status == "warn" and "plugin provides it" in check.detail
    assert "pa plugins" in check.fix
    plain = checks(tmp_path / "plain", '[notify]\nchannel = "signal"\n')["notify channel"]
    assert "no such channel is switched on" in plain.detail


# ---- the chat page ----------------------------------------------------------------------------
WEB = "[web]\nenabled = true\n"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def web(extra: str = "") -> str:
    """A web section on a port nothing else is using, so the port check has nothing to say."""
    return f"[web]\nenabled = true\nport = {free_port()}\n{extra}"


def pair_a_browser(tmp_path, *, then_sign_out: bool = False):
    paths = Paths.resolve(tmp_path / "home")
    paths.ensure()
    db = open_database(paths.db_file)
    auth = WebAuth(db)
    session = auth.redeem(auth.invite("phone").code)
    if then_sign_out:
        auth.revoke_all()
    db.close()
    return session


def test_the_chat_page_says_nothing_when_it_is_off(tmp_path):
    found = checks(tmp_path)
    assert not [name for name in found if name.startswith("chat page")]


def test_a_page_for_this_computer_only_is_fine_but_nobody_has_paired_yet(tmp_path):
    found = checks(tmp_path, web())
    assert found["chat page"].status == "ok" and "this computer only" in found["chat page"].detail
    browsers = found["chat page browsers"]
    assert browsers.status == "warn" and "no browser is paired" in browsers.detail
    assert "pa web pair" in browsers.fix


def test_paired_browsers_are_counted(tmp_path):
    toml = web()
    checks(tmp_path, toml)  # create the home
    pair_a_browser(tmp_path)
    browsers = checks(tmp_path, toml)["chat page browsers"]
    assert browsers.status == "ok" and "1 paired" in browsers.detail


def test_a_browser_that_was_signed_out_does_not_count(tmp_path):
    toml = web()
    checks(tmp_path, toml)
    pair_a_browser(tmp_path, then_sign_out=True)
    assert checks(tmp_path, toml)["chat page browsers"].status == "warn"


def test_listening_beyond_this_computer_without_https_is_flagged(tmp_path):
    check = checks(tmp_path, web('host = "0.0.0.0"\n'))["chat page"]
    assert check.status == "warn" and "other devices on the network" in check.detail
    assert "tailscale serve" in check.fix and "public_url" in check.fix


def test_https_in_front_makes_a_wider_listener_acceptable(tmp_path):
    toml = web('host = "0.0.0.0"\npublic_url = "https://mac.tailnet.ts.net"\n')
    check = checks(tmp_path, toml)["chat page"]
    assert check.status == "ok" and "https://mac.tailnet.ts.net" in check.detail


def test_a_public_address_without_https_is_flagged_unless_it_is_this_computer(tmp_path):
    flagged = checks(tmp_path, web('public_url = "http://mac.local:8765"\n'))["chat page"]
    assert flagged.status == "warn" and "not https" in flagged.detail
    local = checks(tmp_path / "local", web('public_url = "http://localhost:8765"\n'))["chat page"]
    assert local.status == "ok"


def test_a_port_that_is_taken_is_reported_while_nothing_runs_the_agent(tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        found = checks(tmp_path, f"[web]\nenabled = true\nport = {port}\n")
    check = found["web port"]
    assert check.status == "warn" and f"127.0.0.1:{port}" in check.detail
    assert "[web]" in check.fix


def test_a_taken_port_is_not_reported_while_the_agent_itself_runs(tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        paths = Paths.resolve(tmp_path / "home")
        paths.ensure()
        lock = EngineLock(paths.run_dir / LOCK_NAME)
        assert lock.acquire()
        try:
            found = checks(tmp_path, f"[web]\nenabled = true\nport = {port}\n")
        finally:
            lock.release()
    assert "web port" not in found


def test_an_ipv6_listen_address_is_checked_as_ipv6(tmp_path):
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
            port = probe.getsockname()[1]
    except OSError:
        pytest.skip("this machine has no IPv6 loopback")
    found = checks(tmp_path, f'[web]\nenabled = true\nhost = "::1"\nport = {port}\n')
    assert "web port" not in found  # it is free, and asking about it as IPv4 would have failed


# ---- webhooks ---------------------------------------------------------------------------------
STRONG = "wh-" + "k3y" * 8


def hooks(extra: str = "", routes: str | None = None) -> str:
    route = (
        '[webhooks.routes.github]\nscheme = "github"\nsecret_env = "GITHUB_WEBHOOK_SECRET"\n'
        if routes is None
        else routes
    )
    return f"[webhooks]\nenabled = true\nport = {free_port()}\n{extra}\n{route}"


def test_webhooks_say_nothing_when_they_are_off(tmp_path):
    found = checks(tmp_path)
    assert not [name for name in found if name.startswith("webhook")]


def test_switched_on_without_any_route_is_a_warning(tmp_path):
    check = checks(tmp_path, hooks(routes=""))["webhooks"]
    assert check.status == "warn" and "no route" in check.detail
    assert "[webhooks.routes.NAME]" in check.fix


def test_a_route_without_its_secret_fails_and_says_how_to_make_one(tmp_path):
    check = checks(tmp_path, hooks())["webhook github"]
    assert check.status == "fail" and "GITHUB_WEBHOOK_SECRET is not set" in check.detail
    assert "deliveries are refused" in check.detail
    assert "pa webhooks secret github" in check.fix and ".env" in check.fix


def test_a_secret_that_would_not_protect_anything_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "short")
    check = checks(tmp_path, hooks())["webhook github"]
    assert check.status == "fail" and "shorter than 16 characters" in check.detail
    assert "short" not in check.fix  # the value itself is never repeated


def test_a_standard_webhooks_secret_must_be_in_its_own_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("SVIX_SECRET", "not base64 at all!")
    route = '[webhooks.routes.svix]\nscheme = "standard"\nsecret_env = "SVIX_SECRET"\n'
    check = checks(tmp_path, hooks(routes=route))["webhook svix"]
    assert check.status == "fail" and "whsec_" in check.detail


def test_a_route_with_a_good_secret_is_ok_and_names_its_scheme_and_events(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", STRONG)
    route = (
        '[webhooks.routes.github]\nscheme = "github"\nsecret_env = "GITHUB_WEBHOOK_SECRET"\n'
        'events = ["push", "pull_request"]\n'
    )
    check = checks(tmp_path, hooks(routes=route))["webhook github"]
    assert check.status == "ok"
    assert "github" in check.detail and "push, pull_request" in check.detail


def test_no_check_ever_repeats_a_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", STRONG)
    found = checks(tmp_path, hooks())
    text = " ".join(f"{c.name} {c.detail} {c.fix}" for c in found.values())
    assert STRONG not in text


def test_a_listener_that_stays_on_this_computer_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", STRONG)
    check = checks(tmp_path, hooks())["webhook listener"]
    assert check.status == "ok" and "this computer only" in check.detail


def test_a_listener_open_to_the_network_without_https_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", STRONG)
    check = checks(tmp_path, hooks('host = "0.0.0.0"'))["webhook listener"]
    assert check.status == "warn" and "travels in the clear" in check.detail
    assert 'host = "127.0.0.1"' in check.fix


def test_a_tunnel_address_with_https_is_fine_and_without_it_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", STRONG)
    good = checks(tmp_path, hooks('public_url = "https://hooks.example.com"'))["webhook listener"]
    assert good.status == "ok" and "hooks.example.com" in good.detail
    bad = checks(tmp_path / "bad", hooks('public_url = "http://hooks.example.com"'))[
        "webhook listener"
    ]
    assert bad.status == "warn" and "not https" in bad.detail


def test_a_taken_webhook_port_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", STRONG)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        toml = (
            f"[webhooks]\nenabled = true\nport = {port}\n"
            '[webhooks.routes.github]\nscheme = "github"\nsecret_env = "GITHUB_WEBHOOK_SECRET"\n'
        )
        found = checks(tmp_path, toml)
    assert found["webhooks port"].status == "warn"
