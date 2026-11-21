from __future__ import annotations

import json
import stat

import httpx
import pytest

from helpers import invoke
from personal_agent.channels.web import connect as phone
from personal_agent.commands.web import _reachable_from_a_phone
from personal_agent.config import load_config
from personal_agent.config_edit import ConfigEditError, set_section

NAME = "mac.example.ts.net"
TARGET = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    from personal_agent.channels.web import probe

    monkeypatch.setattr(phone, "local_address", lambda host: None)
    monkeypatch.setattr(phone, "port_available", lambda port: True)
    monkeypatch.setattr(probe, "port_available", lambda port, **kw: True)


def serve_config(proxy=TARGET, *, funnel=False, port=443):
    return {
        "TCP": {str(port): {"HTTPS": True}},
        "Web": {f"{NAME}:{port}": {"Handlers": {"/": {"Proxy": proxy}}}},
        "AllowFunnel": {f"{NAME}:{port}": funnel},
    }


class Tailscale:
    def __init__(self, before=None, *, after=None, connected=True):
        self.config = before or {}
        self.after = after
        self.connected = connected
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[1:] == ["status", "--json"]:
            return json.dumps(
                {
                    "BackendState": "Running" if self.connected else "Stopped",
                    "Self": {"DNSName": NAME + "."},
                }
            )
        if args[1:] == ["serve", "status", "--json"]:
            return json.dumps(self.config)
        assert args[1:3] == ["serve", "--bg"]
        assert "funnel" not in args and "reset" not in args
        port = int(args[3].split("=")[1])
        self.config = self.after if self.after is not None else serve_config(port=port)
        return "Available within your tailnet"


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "0.0.0.0",
        "8.8.8.8",
        "100.100.10.1",
        "169.254.1.2",
        "example.com",
        "192.168.1.2;echo hi",
    ],
)
def test_lan_setup_never_selects_a_public_or_wildcard_listener(host):
    with pytest.raises(phone.ConnectionError, match="private IPv4"):
        phone.lan_connection(host, 8765)


def test_lan_setup_uses_only_the_chosen_interface():
    result = phone.lan_connection("192.168.1.23", 9000)
    assert result.host == "192.168.1.23"
    assert result.url == "http://192.168.1.23:9000"


def test_ambiguous_lan_interfaces_need_an_explicit_choice(monkeypatch):
    monkeypatch.setattr(phone, "lan_addresses", lambda: ["192.168.1.2", "10.0.0.2"])
    with pytest.raises(phone.ConnectionError, match="--host"):
        phone.lan_connection(None, 8765)


def test_one_lan_interface_is_selected_without_an_extra_question(monkeypatch):
    monkeypatch.setattr(phone, "lan_addresses", lambda: ["192.168.1.2"])
    assert phone.lan_connection(None, 8765).host == "192.168.1.2"


def test_tailscale_setup_verifies_the_private_https_proxy():
    fake = Tailscale()
    result = phone.tailscale_connection(8765, run=fake, binary="tailscale")
    assert result.url == f"https://{NAME}" and result.host == "127.0.0.1"
    assert ["tailscale", "serve", "--bg", "--https=443", TARGET] in fake.calls
    assert fake.calls[-1] == ["tailscale", "serve", "status", "--json"]


def test_an_occupied_local_port_is_not_exposed_as_if_it_were_the_agent(monkeypatch):
    fake = Tailscale()
    monkeypatch.setattr(phone, "port_available", lambda port: False)
    with pytest.raises(phone.ConnectionError, match="occupied"):
        phone.tailscale_connection(8765, run=fake, binary="tailscale")
    assert fake.calls == []


def test_lan_pairing_refuses_an_address_not_on_this_computer(wired, monkeypatch):
    app = wired()

    def not_here(host):
        raise phone.ConnectionError("not an available address")

    monkeypatch.setattr(phone, "local_address", not_here)
    result = invoke("web", "connect", "--via", "lan", "--host", "10.255.255.254", "--no-qr")
    assert result.exit_code == 1
    assert not app.web_auth.pending_invites()
    assert not app.paths.config_file.exists()


def test_tailscale_setup_is_idempotent_and_supports_a_different_port():
    fake = Tailscale(serve_config(port=8443))
    result = phone.tailscale_connection(8765, 8443, run=fake, binary="tailscale")
    assert result.url == f"https://{NAME}:8443"
    assert all("--bg" not in command for command in fake.calls)


@pytest.mark.parametrize(
    "before",
    [
        serve_config("http://127.0.0.1:3000"),
        serve_config(funnel=True),
        {"TCP": {"443": {"TCPForward": "127.0.0.1:22"}}},
        {"Foreground": {"other": serve_config("http://127.0.0.1:3000")}},
    ],
)
def test_tailscale_refuses_conflicts_and_public_funnel_without_mutation(before):
    fake = Tailscale(before)
    with pytest.raises(phone.ConnectionError):
        phone.tailscale_connection(8765, run=fake, binary="tailscale")
    assert all("--bg" not in command for command in fake.calls)


def test_tailscale_setup_does_not_claim_success_before_https_is_enabled():
    with pytest.raises(phone.ConnectionError, match="not enabled"):
        phone.tailscale_connection(8765, run=Tailscale(after={}), binary="tailscale")


def test_a_disconnected_tailnet_does_not_change_serve():
    fake = Tailscale(connected=False)
    with pytest.raises(phone.ConnectionError, match="not connected"):
        phone.tailscale_connection(8765, run=fake, binary="tailscale")
    assert len(fake.calls) == 1


def test_connect_command_preserves_other_settings_and_prints_a_usable_invite(wired):
    app = wired()
    app.paths.config_file.write_text('# keep my notes\n[agent]\nname = "Sam"\n[web]\nport = 9010\n')
    app.paths.config_file.chmod(0o600)
    result = invoke(
        "web", "connect", "--via", "lan", "--host", "192.168.1.23", "--port", "9010", "--no-qr"
    )
    assert result.exit_code == 0, result.output
    assert "http://192.168.1.23:9010/#pair=" in result.output
    assert "pa service restart" in result.output
    assert "scan the code" not in result.output  # --no-qr prints no code to scan
    cfg = load_config(app.paths)
    assert cfg.web.enabled and cfg.web.host == "192.168.1.23" and cfg.agent.name == "Sam"
    assert "# keep my notes" in app.paths.config_file.read_text()
    assert stat.S_IMODE(app.paths.config_file.stat().st_mode) == 0o600
    assert len(app.web_auth.pending_invites()) == 1


def test_failed_setup_does_not_edit_the_config_or_issue_a_link(wired, monkeypatch):
    app = wired()
    app.paths.config_file.write_text("[web]\nenabled = false\n")
    before = app.paths.config_file.read_bytes()

    def refuse(*args, **kwargs):
        raise phone.ConnectionError("Another service uses this port")

    monkeypatch.setattr(phone, "tailscale_connection", refuse)
    result = invoke("web", "connect", "--no-qr")
    assert result.exit_code == 1 and "Another service" in result.output
    assert app.paths.config_file.read_bytes() == before
    assert app.web_auth.pending_invites() == []


def test_config_edit_validates_before_replacing_the_file(paths):
    paths.config_file.write_text("# keep\n[web]\nport=8765\n")
    before = paths.config_file.read_bytes()
    with pytest.raises(ConfigEditError):
        set_section(paths.config_file, "web", {"port": -1})
    assert paths.config_file.read_bytes() == before


@pytest.mark.parametrize("url", ["https://localhost", "https://127.0.0.1", "http://[::1]:8765"])
def test_loopback_links_are_never_presented_as_phone_addresses(url):
    assert not _reachable_from_a_phone(url)


def test_connection_check_reports_listener_and_phone_address(wired, monkeypatch):
    from personal_agent.channels.web.probe import probe_signature
    from personal_agent.config import Config

    app = wired(config=Config(web={"enabled": True, "public_url": f"https://{NAME}"}))
    urls = []

    def respond(request):
        urls.append(str(request.url).split("?")[0])
        body = json.dumps(
            {"proof": probe_signature(app.db, request.url.params["challenge"])}
        ).encode()
        return httpx.Response(200, stream=httpx.ByteStream(body))

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    result = invoke("web", "check")
    assert result.exit_code == 0, result.output
    assert urls == [TARGET + "/api/health", f"https://{NAME}/api/health"]
    assert "Local listener" in result.output and "Phone address" in result.output


def test_http_200_from_an_unrelated_service_is_not_a_healthy_agent(wired, monkeypatch):
    from personal_agent.config import Config

    wired(config=Config(web={"enabled": True}))
    original = httpx.Client

    def respond(request):
        return httpx.Response(200, stream=httpx.ByteStream(b'{"proof":"wrong-home"}'))

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    result = invoke("web", "check")
    assert result.exit_code == 1 and "cannot be verified" in result.output


def test_an_anonymous_probe_does_not_take_the_write_lock_once_its_key_exists(make_app, monkeypatch):
    """`/api/health` needs no sign-in, so every call from the network must be a plain read: a
    write transaction per request would queue anyone on the Wi-Fi in front of the agent's writers."""
    from personal_agent.channels.web.probe import probe_signature

    app = make_app()
    first = probe_signature(app.db, "a" * 32)  # the first call creates the key: that one may write
    opened = []
    original = app.db.tx

    def counting():
        opened.append(True)
        return original()

    monkeypatch.setattr(app.db, "tx", counting)
    assert [probe_signature(app.db, "a" * 32) for _ in range(3)] == [first] * 3
    assert opened == []


def test_probe_returns_only_a_challenge_signature_and_never_authenticates_a_browser(make_app):
    from personal_agent.channels.web.probe import PROBE_KEY, probe_signature
    from web_fake import Web

    site = Web(make_app())
    challenge = "a" * 32
    response = site.client.get("/api/health", params={"challenge": challenge})
    assert response.status_code == 200
    assert response.json() == {"proof": probe_signature(site.app.db, challenge)}
    assert site.app.db.kv_get(PROBE_KEY) not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert site.client.get("/api/session").status_code == 401
    assert site.client.get("/api/health").status_code == 400
