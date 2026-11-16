"""The network gate: what outbound requests can and cannot reach, tested against real sockets."""

from __future__ import annotations

import gzip
import ipaddress
import json
import socket
import time

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


class _Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.read = False

    async def __aiter__(self):
        self.read = True
        yield self.data


@pytest.mark.parametrize("encoding", ["gzip", "GZip", "deflate", "br", "zstd", "identity, gzip"])
async def test_compressed_responses_are_refused_before_decoding_or_reading(encoding):
    # The compressed input is tiny; expanding it before applying the 1 KiB result cap used to
    # allocate over 20 MiB. Never consuming the stream proves no decoder can do that allocation.
    body = _Body(gzip.compress(b"x" * (8 * 1024 * 1024)))
    response = httpx.Response(200, headers={"content-encoding": encoding}, stream=body)
    try:
        with pytest.raises(Blocked, match="compressed response"):
            await ssrf._read_capped(response, 1024)
        assert not body.read
    finally:
        await response.aclose()


@pytest.mark.parametrize("encoding", [None, "identity", " Identity "])
async def test_uncompressed_responses_are_still_capped(encoding):
    body = _Body(b"x" * 2048)
    headers = {} if encoding is None else {"content-encoding": encoding}
    response = httpx.Response(200, headers=headers, stream=body)
    try:
        text, truncated = await ssrf._read_capped(response, 1024)
        assert text == b"x" * 1024 and truncated and body.read
    finally:
        await response.aclose()


@pytest.mark.parametrize(("method", "status"), [("HEAD", 200), ("GET", 204), ("GET", 304)])
async def test_bodyless_responses_keep_encoding_metadata_without_decoding_it(
    method, status, monkeypatch
):
    body = _Body(b"")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, headers={"content-encoding": "gzip"}, stream=body)
    )
    monkeypatch.setattr(ssrf, "GuardedTransport", lambda policy: transport)
    response = await fetch(NetConfig(), method, "https://example.test/resource")
    assert response.body == b"" and not response.truncated and not body.read
    assert response.headers["content-encoding"] == "gzip"


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
    "Private-Token": "glpat-1",  # credentials need not have a famous name
    "X-Auth-Token": "t",
    "X-Notes": "the owner's private notes",
    "Accept-Language": "de",
}


async def test_only_harmless_headers_follow_a_redirect_to_another_site(site, other_site):
    url = f"{site.base}/redirect?to={other_site.base}/echo"
    result = await fetch(LOCAL, "GET", url, headers=AUTH)
    arrived = json.loads(result.body)["headers"]
    assert arrived["accept-language"] == "de"  # what is known to be harmless is kept
    named = {name.lower() for name in AUTH} - {"accept-language"}
    assert not named & arrived.keys(), "a header written for one site reached another"
    assert site.seen[0].headers["x-notes"] == AUTH["X-Notes"]  # the first site did get them


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


async def test_a_server_that_dribbles_out_its_answer_is_cut_off_at_the_deadline(site):
    """No single wait is long (a byte comes every 50 ms), so only a limit on the whole can end it."""
    config = NetConfig(allow_private=["127.0.0.1"], timeout_s=5, deadline_s=0.5)
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException, match=r"not complete within 0\.5 seconds"):
        await fetch(config, "GET", f"{site.base}/trickle")
    assert time.monotonic() - started < 3


async def test_a_request_asks_for_the_answer_uncompressed(site):
    result = await fetch(LOCAL, "GET", f"{site.base}/echo")
    assert json.loads(result.body)["headers"]["accept-encoding"] == "identity"


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


def test_what_a_redirect_keeps_is_a_short_list_of_harmless_headers():
    assert {"accept", "accept-language"} <= ssrf.KEPT_ACROSS_SITES
    assert (
        not {"authorization", "cookie", "x-api-key", "proxy-authorization"} & ssrf.KEPT_ACROSS_SITES
    )


# ---- what a redirect asks for (the decision alone, with no network) ---------------------------------------
def request(url: str, verb: str = "GET", content: bytes | None = None, **headers: str):
    return ssrf._Request(httpx.URL(url), verb, content, headers)


def test_a_redirect_within_a_site_changes_only_the_address():
    asked = request("https://a.example/start", Authorization="Bearer x", Accept="text/html")
    follows = asked.after(302, "/elsewhere?x=1")
    assert str(follows.url) == "https://a.example/elsewhere?x=1"
    assert (follows.verb, follows.headers) == ("GET", asked.headers)


def test_a_redirect_to_another_site_leaves_behind_every_header_that_is_not_known_to_be_harmless():
    asked = request(
        "https://a.example/start",
        Authorization="Bearer x",
        **{
            "PRIVATE-TOKEN": "t",
            "X-Auth-Token": "u",
            "Accept": "text/html",
            "Accept-Language": "zh",
        },
    )
    follows = asked.after(302, "https://b.example/landing")
    assert follows.headers == {"Accept": "text/html", "Accept-Language": "zh"}
    assert asked.headers["Authorization"] == "Bearer x"  # the request that was asked is untouched


@pytest.mark.parametrize("status", [307, 308])
def test_data_is_not_replayed_to_another_site_but_is_within_one(status):
    posting = request(
        "https://a.example/form", "POST", b"secret=1", **{"Content-Type": "text/plain"}
    )
    with pytest.raises(Blocked, match="carries data to another address"):
        posting.after(status, "https://b.example/")
    same_site = posting.after(status, "/again")
    assert (same_site.verb, same_site.content) == ("POST", b"secret=1")
    assert same_site.headers == {"Content-Type": "text/plain"}


@pytest.mark.parametrize("status", [301, 302, 303])
def test_these_redirects_turn_a_post_into_a_get_without_the_body_or_what_describes_it(status):
    posting = request(
        "https://a.example/form",
        "POST",
        b"secret=1",
        **{"Content-Type": "text/plain", "Content-Length": "8", "Accept": "text/html"},
    )
    follows = posting.after(status, "/done")
    assert (follows.verb, follows.content) == ("GET", None)
    assert follows.headers == {"Accept": "text/html"}


@pytest.mark.parametrize("status", [301, 302, 303])
def test_a_post_sent_on_to_another_site_becomes_a_get_there_and_takes_nothing_with_it(status):
    """Only 307 and 308 ask for the data to follow; the others ask for a page, and get one."""
    posting = request(
        "https://a.example/form",
        "POST",
        b"secret=1",
        **{"Content-Type": "text/plain", "Authorization": "Bearer x", "Accept": "text/html"},
    )
    follows = posting.after(status, "https://b.example/done")
    assert (str(follows.url), follows.verb, follows.content) == (
        "https://b.example/done",
        "GET",
        None,
    )
    assert follows.headers == {"Accept": "text/html"}


def test_a_head_request_stays_a_head_request():
    assert request("https://a.example/", "HEAD").after(301, "/x").verb == "HEAD"


@pytest.mark.parametrize("location", ["http://user:pw@b.example/", "ftp://b.example/"])
def test_a_redirect_to_an_address_the_gate_would_not_accept_is_not_followed(location):
    with pytest.raises(Blocked):
        request("https://a.example/").after(302, location)


@pytest.mark.parametrize(
    "location", ["http://b.example:abc/", "http://[::1/", "http://b.example:99999/"]
)
async def test_a_redirect_to_something_that_is_not_an_address_ends_in_an_error_the_tools_word(
    site, location
):
    """httpx reads the Location itself before the gate sees it. That is what keeps an unparseable
    address from escaping as `httpx.InvalidURL`, which is not an `HTTPError` and so would not be
    caught: if a new httpx stops doing it, this fails and says where the gate must."""
    with pytest.raises((Blocked, httpx.HTTPError)):
        await fetch(LOCAL, "GET", f"{site.base}/redirect?to={location}")
    assert len(site.seen) == 1


# ---- what a request says about itself ---------------------------------------------------------------------
async def test_a_request_says_who_it_is_what_it_reads_and_that_it_wants_the_plain_text(site):
    await fetch(LOCAL, "GET", f"{site.base}/text")
    sent = dict(site.seen[0].lines)
    assert sent["user-agent"] == LOCAL.user_agent
    assert "text/html" in sent["accept"] and sent["accept-encoding"] == "identity"


@pytest.mark.parametrize("spelling", ["Accept", "accept", "ACCEPT"])
async def test_a_header_the_caller_writes_replaces_ours_in_any_capitalization(site, spelling):
    """A server joins two lines of one header into one list, so `Accept: application/json` after our
    own `accept: text/html,...` asked for both. It did, until this was written."""
    await fetch(LOCAL, "GET", f"{site.base}/text", headers={spelling: "application/json"})
    names = [name for name, _ in site.seen[0].lines]
    assert [v for n, v in site.seen[0].lines if n == "accept"] == ["application/json"]
    assert names.count("user-agent") == 1 and names.count("accept-encoding") == 1


async def test_what_a_request_says_about_itself_is_said_again_after_a_redirect(site):
    await fetch(
        LOCAL, "GET", f"{site.base}/redirect?to=/text", headers={"Accept": "application/json"}
    )
    assert len(site.seen) == 2
    for seen in site.seen:
        names = [name for name, _ in seen.lines]
        assert [v for n, v in seen.lines if n == "accept"] == ["application/json"]
        assert names.count("user-agent") == 1 and names.count("accept-encoding") == 1
        assert dict(seen.lines)["user-agent"] == LOCAL.user_agent
        assert dict(seen.lines)["accept-encoding"] == "identity"


# ---- the capped read --------------------------------------------------------------------------------------
class Chunks(httpx.AsyncByteStream):
    """A body that arrives in the given pieces."""

    def __init__(self, *parts: bytes) -> None:
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part


@pytest.mark.parametrize(
    ("parts", "limit", "expected"),
    [
        ((b"abcdef",), 7, (b"abcdef", False)),
        ((b"abcdef",), 6, (b"abcdef", False)),  # fits exactly: nothing was left out
        ((b"abcdef",), 5, (b"abcde", True)),
        ((b"abc", b"def"), 6, (b"abcdef", False)),  # fits exactly across two pieces
        ((b"abc", b"def"), 5, (b"abcde", True)),
        ((b"abc", b"def"), 3, (b"abc", True)),  # the first piece fills it and more follows
        ((b"abc", b""), 3, (b"abc", False)),
        ((), 3, (b"", False)),
    ],
)
async def test_a_body_is_cut_at_the_limit_and_only_said_to_be_cut_when_something_was_left(
    parts, limit, expected
):
    response = httpx.Response(200, stream=Chunks(*parts))
    assert await ssrf._read_capped(response, limit) == expected
