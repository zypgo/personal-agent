"""The network gate: what outbound requests can and cannot reach, tested against real sockets."""

from __future__ import annotations

import ipaddress
import json
import socket

import httpx
import pytest

from personal_agent.config import NetConfig
from personal_agent.security import ssrf
from personal_agent.security.ssrf import (
    Blocked,
    GuardedBackend,
    GuardedTransport,
    NetPolicy,
    check_url,
    fetch,
    resolve_checked,
)

LOCAL = NetConfig(allow_private=["127.0.0.1"])
"""The test servers listen on loopback, which is blocked unless listed."""


def ip(text: str):
    return ipaddress.ip_address(text)


# ---- which addresses are reachable --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.5.4", "172.31.255.255", "192.168.1.1",
        "100.64.0.1",  # carrier-grade NAT: not public, not "private" either
        "0.0.0.0", "169.254.169.254", "169.254.1.1", "224.0.0.1", "255.255.255.255",
        "::1", "::", "fc00::1", "fd12:3456::1", "fe80::1", "ff02::1", "fd00:ec2::254",
    ],
)  # fmt: skip
def test_non_public_addresses_are_refused_by_default(address):
    assert NetPolicy().check("host", ip(address)) is not None


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1", "11.0.0.1", "100.128.0.1",
        "2606:4700:4700::1111", "2001:4860:4860::8888",
    ],
)  # fmt: skip
def test_public_addresses_are_reachable(address):
    assert NetPolicy().check("host", ip(address)) is None


@pytest.mark.parametrize(
    "wrapped",
    [
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.1.2.3",
        "::ffff:169.254.169.254",
        "::ffff:7f00:1",  # the same loopback, written in hex
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 wrapping 169.254.169.254
        "::ffff:0:7f00:1",  # IPv4-translated
        "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
        "2002:a9fe:a9fe::1",  # 6to4 wrapping the metadata address
    ],
)
def test_an_ipv4_address_in_an_ipv6_wrapper_is_judged_as_the_ipv4_address(wrapped):
    assert NetPolicy().check("host", ip(wrapped)) is not None


def test_a_wrapped_public_address_stays_reachable():
    assert NetPolicy().check("host", ip("::ffff:8.8.8.8")) is None
    assert NetPolicy().check("host", ip("64:ff9b::808:808")) is None


def test_metadata_and_link_local_stay_blocked_even_when_the_owner_allows_private_networks():
    everything = NetPolicy.from_config(
        NetConfig(
            allow_private=["0.0.0.0/0", "::/0", "169.254.169.254", "metadata.google.internal"]
        )
    )
    for address in (
        "169.254.169.254",
        "169.254.0.5",
        "::ffff:169.254.169.254",
        "fe80::1",
        "0.0.0.0",
    ):
        assert everything.check("metadata.google.internal", ip(address)) is not None, address
    # ...while the rest of the owner's allowance still works
    assert everything.check("home", ip("192.168.1.10")) is None


def test_metadata_host_names_are_refused_without_asking_dns(monkeypatch):
    def no_dns(*a, **k):
        raise AssertionError("must not resolve a metadata name")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    for name in ("metadata.google.internal", "METADATA.GOOGLE.INTERNAL.", "metadata.goog"):
        with pytest.raises(Blocked, match="metadata"):
            resolve_checked(name, 80, NetPolicy())


def test_allowing_a_network_allows_that_network_only():
    policy = NetPolicy.from_config(NetConfig(allow_private=["192.168.1.0/24"]))
    assert policy.check("nas", ip("192.168.1.20")) is None
    assert policy.check("nas", ip("192.168.2.20")) is not None
    assert policy.check("nas", ip("10.0.0.1")) is not None


def test_allowing_a_host_name_allows_what_that_name_resolves_to():
    policy = NetPolicy.from_config(NetConfig(allow_private=["homeassistant.local"]))
    assert policy.check("homeassistant.local", ip("192.168.1.20")) is None
    assert policy.check("other.local", ip("192.168.1.20")) is not None
    assert policy.is_listed("HomeAssistant.local.") and not policy.is_listed("other.local")


def test_is_listed_understands_ip_literals_and_networks():
    policy = NetPolicy.from_config(NetConfig(allow_private=["192.168.1.0/24", "::1"]))
    assert policy.is_listed("192.168.1.9") and policy.is_listed("[::1]")
    assert not policy.is_listed("192.168.2.9") and not policy.is_listed("example.com")


def test_a_proxy_tools_fake_ip_answer_gets_its_own_explanation():
    reason = NetPolicy().check("example.com", ip("198.18.1.59"))
    assert (
        reason and "fake-IP" in reason and "198.18.0.0/15" in reason and "allow_private" in reason
    )
    allowed = NetPolicy.from_config(NetConfig(allow_private=["198.18.0.0/15"]))
    assert allowed.check("example.com", ip("198.18.1.59")) is None
    assert (
        allowed.check("example.com", ip("10.0.0.1")) is not None
    )  # allowing it opens nothing else


def test_the_refusal_tells_the_owner_how_to_allow_it():
    reason = NetPolicy().check("nas.local", ip("192.168.1.20"))
    assert reason and "allow_private" in reason and "nas.local" in reason


# ---- resolving ----------------------------------------------------------------------------------------------
def fake_dns(monkeypatch, *addresses):
    def getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)) for addr in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_a_name_that_points_partly_at_a_private_address_is_not_trusted_at_all(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34", "10.0.0.5")  # a rebinding answer set
    with pytest.raises(Blocked, match=r"10\.0\.0\.5"):
        resolve_checked("sneaky.example", 443, NetPolicy())


def test_only_vetted_public_addresses_come_back(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34", "93.184.216.35", "93.184.216.34")
    assert resolve_checked("fine.example", 443, NetPolicy()) == ["93.184.216.34", "93.184.216.35"]


def test_ipv4_addresses_are_tried_before_ipv6_ones_and_the_list_is_short(monkeypatch):
    def getaddrinfo(host, port, *a, **k):
        answers = [("2606:4700::1111", socket.AF_INET6), ("93.184.216.34", socket.AF_INET)]
        answers += [(f"93.184.216.{n}", socket.AF_INET) for n in range(40, 48)]
        return [(family, socket.SOCK_STREAM, 6, "", (addr, port)) for addr, family in answers]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    result = resolve_checked("dual.example", 443, NetPolicy())
    assert result[0] == "93.184.216.34" and len(result) == ssrf.MAX_ADDRESSES
    assert all(":" not in address for address in result)  # the v6 address fell off the short list

    def v6_only(host, port, *a, **k):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", port, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", v6_only)
    assert resolve_checked("v6.example", 443, NetPolicy()) == ["2606:4700::1111"]


def test_a_name_that_does_not_resolve_is_reported_plainly(monkeypatch):
    def failing(*a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", failing)
    with pytest.raises(Blocked, match="cannot resolve"):
        resolve_checked("nonexistent.example", 443, NetPolicy())


# ---- URLs ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("ftp://example.com/file", "only http"),
        ("file:///etc/passwd", "only http"),
        ("javascript:alert(1)", "only http"),
        ("gopher://example.com/", "only http"),
        ("//example.com/path", "only http"),
        ("example.com/path", "only http"),
        ("http://", "no host"),
        ("https:///path", "no host"),
        ("http://user:pw@example.com/", "user name"),
        ("http://example.com@evil.com/", "user name"),
        ("http://example.com\\@evil.com/", "backslash"),
        ("http://evil.com\\.example.com/", "backslash"),
        ("http://exa%6dple.com/", "ambiguous"),
        ("http://example.com/\nHost: evil.com", "control characters"),
        ("http://example.com/\x00", "control characters"),
        ("https://example.com:99999/", "not valid"),
        ("https://example.com:0/", "port"),
        ("http://[::1/", "not valid"),
    ],
)
def test_urls_that_parsers_read_differently_are_refused(url, why):
    with pytest.raises(Blocked, match=why):
        check_url(url)


def test_ordinary_urls_pass_and_are_normalized():
    assert (
        str(check_url("  https://Example.COM/a b?x=1#frag  "))
        == "https://example.com/a%20b?x=1#frag"
    )
    assert check_url("http://127.0.0.1:8080/x").port == 8080
    assert check_url("http://[::1]:9/").host == "::1"


# ---- real requests --------------------------------------------------------------------------------------------
async def test_loopback_is_refused_by_default_and_the_server_never_hears_about_it(site):
    with pytest.raises(Blocked, match="allow_private"):
        await fetch(NetConfig(), "GET", f"{site.base}/text")
    assert site.seen == []


async def test_a_name_that_resolves_to_loopback_is_refused_too(site):
    with pytest.raises(Blocked, match="resolves to"):
        await fetch(NetConfig(), "GET", f"http://localhost:{site.port}/text")
    assert site.seen == []


async def test_a_listed_address_can_be_reached(site):
    result = await fetch(LOCAL, "GET", f"{site.base}/text")
    assert (result.status, result.body, result.content_type) == (200, b"plain words", "text/plain")
    assert not result.truncated and result.redirects == []


async def test_a_listed_host_name_can_be_reached_and_keeps_its_name_in_the_request(
    site, monkeypatch
):
    real = socket.getaddrinfo

    def getaddrinfo(host, port, *a, **k):
        if host == "nas.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    cfg = NetConfig(allow_private=["nas.test"])
    result = await fetch(cfg, "GET", f"http://nas.test:{site.port}/echo")
    assert json.loads(result.body)["headers"]["host"] == f"nas.test:{site.port}"
    # and the same name is refused when it is not listed
    with pytest.raises(Blocked):
        await fetch(NetConfig(), "GET", f"http://nas.test:{site.port}/echo")


async def test_a_redirect_to_a_forbidden_address_is_stopped_where_it_happens(site):
    target = "http://169.254.169.254/latest/meta-data/"
    with pytest.raises(Blocked, match="link-local"):
        await fetch(LOCAL, "GET", f"{site.base}/redirect?to={target}")
    assert site.paths() == [f"/redirect?to={target}"]  # nothing was requested after the redirect


async def test_a_redirect_to_a_private_address_is_stopped(site):
    with pytest.raises(Blocked, match="allow_private"):
        await fetch(LOCAL, "GET", f"{site.base}/redirect?to=http://10.0.0.5:8080/admin")


async def test_a_redirect_to_a_forbidden_scheme_is_stopped(site):
    with pytest.raises(Blocked, match="only http"):
        await fetch(LOCAL, "GET", f"{site.base}/redirect?to=file:///etc/passwd")


async def test_redirects_are_followed_and_listed(site, other_site):
    url = f"{site.base}/redirect?to={other_site.base}/text&code=301"
    result = await fetch(LOCAL, "GET", url)
    assert result.body == b"plain words" and result.url == f"{other_site.base}/text"
    assert result.redirects == [f"{other_site.base}/text"]


async def test_a_relative_redirect_stays_on_the_same_site(site):
    result = await fetch(LOCAL, "GET", f"{site.base}/redirect?to=/text")
    assert result.url == f"{site.base}/text" and result.body == b"plain words"


async def test_redirect_loops_end(site):
    with pytest.raises(Blocked, match="more than 3 redirects"):
        await fetch(
            NetConfig(allow_private=["127.0.0.1"], max_redirects=3), "GET", f"{site.base}/loop"
        )
    assert len(site.seen) == 4  # the first request plus three redirects


AUTH = {
    "Authorization": "Bearer abc",
    "Cookie": "session=1",
    "X-Api-Key": "k",
    "Proxy-Authorization": "Basic x",
    "X-Custom": "kept",
}


async def test_credentials_do_not_follow_a_redirect_to_another_site(site, other_site):
    url = f"{site.base}/redirect?to={other_site.base}/echo"
    result = await fetch(LOCAL, "GET", url, headers=AUTH)
    arrived = json.loads(result.body)["headers"]
    assert not {"authorization", "cookie", "x-api-key", "proxy-authorization"} & arrived.keys()
    assert arrived["x-custom"] == "kept"
    assert site.seen[0].headers["authorization"] == "Bearer abc"  # the first site did get them


async def test_credentials_stay_on_a_redirect_within_the_same_site(site):
    result = await fetch(LOCAL, "GET", f"{site.base}/redirect?to=/echo", headers=AUTH)
    assert json.loads(result.body)["headers"]["authorization"] == "Bearer abc"


async def test_a_redirect_that_comes_back_to_the_first_site_does_not_get_credentials_again(
    site, other_site
):
    hop = f"{other_site.base}/redirect?to={site.base}/echo"
    result = await fetch(LOCAL, "GET", f"{site.base}/redirect?to={hop}", headers=AUTH)
    assert "authorization" not in json.loads(result.body)["headers"]


async def test_a_post_that_is_redirected_becomes_a_get_without_its_body(site):
    result = await fetch(
        LOCAL,
        "POST",
        f"{site.base}/redirect?to=/echo&code=303",
        headers={"Content-Type": "application/json"},
        content=b'{"secret": 1}',
    )
    echoed = json.loads(result.body)
    assert echoed["method"] == "GET" and echoed["body"] == ""
    assert "content-type" not in echoed["headers"]


async def test_a_307_keeps_the_method_and_body(site):
    result = await fetch(
        LOCAL, "POST", f"{site.base}/redirect?to=/echo&code=307", content=b"payload"
    )
    echoed = json.loads(result.body)
    assert (echoed["method"], echoed["body"]) == ("POST", "payload")


async def test_the_hop_callback_sees_every_request_and_can_stop_one(site, other_site):
    seen: list[tuple[str, str | None]] = []

    def on_hop(url, previous):
        seen.append((str(url), str(previous) if previous else None))

    await fetch(LOCAL, "GET", f"{site.base}/redirect?to={other_site.base}/text", on_hop=on_hop)
    assert seen == [
        (f"{site.base}/redirect?to={other_site.base}/text", None),
        (f"{other_site.base}/text", f"{site.base}/redirect?to={other_site.base}/text"),
    ]

    def refuse(url, previous):
        if previous is not None:
            raise Blocked("no hops")

    with pytest.raises(Blocked, match="no hops"):
        await fetch(LOCAL, "GET", f"{site.base}/redirect?to=/text", on_hop=refuse)
    assert site.paths()[-1].startswith("/redirect")  # the second request was never made


async def test_the_body_is_read_only_up_to_the_limit(site):
    result = await fetch(LOCAL, "GET", f"{site.base}/big", max_bytes=1000)
    assert result.truncated and len(result.body) == 1000
    whole = await fetch(LOCAL, "GET", f"{site.base}/text", max_bytes=1000)
    assert not whole.truncated


async def test_slow_servers_time_out(site):
    with pytest.raises(httpx.TimeoutException):
        await fetch(
            NetConfig(allow_private=["127.0.0.1"], timeout_s=0.3), "GET", f"{site.base}/slow"
        )


async def test_an_unreachable_port_is_a_network_error_not_a_crash(site):
    dead = site.port
    site.stop()
    with pytest.raises(httpx.ConnectError):
        await fetch(LOCAL, "GET", f"http://127.0.0.1:{dead}/")


async def test_environment_proxies_are_ignored(site, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    result = await fetch(LOCAL, "GET", f"{site.base}/text")
    assert result.status == 200


async def test_response_headers_are_lowercased_and_the_type_and_charset_are_parsed(site):
    result = await fetch(LOCAL, "GET", f"{site.base}/latin1")
    assert result.content_type == "text/html" and result.charset == "iso-8859-1"
    assert result.headers["content-type"].startswith("text/html")


# ---- the gate really is on the transport ------------------------------------------------------------------
def test_the_transport_is_built_on_the_guarded_backend():
    """If httpx or httpcore ever change how a pool is built, this fails instead of the gate
    silently turning off."""
    transport = GuardedTransport(NetPolicy())
    assert isinstance(transport._pool._network_backend, GuardedBackend)


async def test_a_client_on_the_guarded_transport_cannot_reach_loopback(site):
    async with httpx.AsyncClient(transport=GuardedTransport(NetPolicy())) as client:
        with pytest.raises(Blocked):
            await client.get(f"{site.base}/text")
    assert site.seen == []


async def test_the_backend_refuses_unix_sockets():
    with pytest.raises(Blocked, match="unix"):
        await GuardedBackend(NetPolicy()).connect_unix_socket("/var/run/docker.sock")


async def test_a_plain_client_would_have_reached_it(site):
    """The control for the test above: the server is reachable, so the refusal came from the gate."""
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"{site.base}/text")).status_code == 200


def test_the_module_exposes_what_the_rest_of_the_code_relies_on():
    assert {"authorization", "cookie", "x-api-key"} <= ssrf.CREDENTIAL_HEADERS
