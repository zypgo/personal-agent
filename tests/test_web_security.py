"""Every request to the web page passes the same checks first. These are the ones that decide
whether a hostile site the owner happens to have open can drive an agent that runs commands."""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from personal_agent.channels.web.security import (
    CSP,
    SECURITY_HEADERS,
    allowed_hosts,
    allowed_origins,
    hostname,
)
from web_fake import ORIGIN, PORT, Web


@pytest.fixture
def web(make_app) -> Web:
    site = Web(make_app())
    site.pair()
    return site


def headers_of(response) -> dict[str, str]:
    return {k.lower(): v for k, v in response.headers.items()}


# ---- the sets of names and origins ------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:8765", "127.0.0.1"),
        ("LocalHost", "localhost"),
        ("[::1]:8765", "[::1]"),
        ("[::1]", "[::1]"),
        ("mac.tail1234.ts.net", "mac.tail1234.ts.net"),
        ("", ""),
        (None, ""),
    ],
)
def test_the_host_is_read_without_its_port(value, expected):
    assert hostname(value) == expected


def test_this_machine_is_always_a_valid_host_and_so_is_the_public_name():
    names = allowed_hosts("127.0.0.1", "https://Mac.Tail1234.ts.net", ["Phone.local"])
    assert names == {"localhost", "127.0.0.1", "[::1]", "mac.tail1234.ts.net", "phone.local"}


def test_a_listener_on_every_address_does_not_make_every_name_valid():
    assert allowed_hosts("0.0.0.0", None) == {"localhost", "127.0.0.1", "[::1]"}
    assert allowed_hosts("::", None) == {"localhost", "127.0.0.1", "[::1]"}


def test_a_listener_on_one_address_answers_to_that_address():
    assert "192.168.1.5" in allowed_hosts("192.168.1.5", None)


def test_origins_carry_the_port():
    origins = allowed_origins("127.0.0.1", 8765, None)
    assert origins == {"http://localhost:8765", "http://127.0.0.1:8765", "http://[::1]:8765"}
    assert "http://localhost:3000" not in origins and "http://127.0.0.1" not in origins


def test_the_public_origin_is_added_as_it_is():
    assert "https://mac.ts.net" in allowed_origins("127.0.0.1", 8765, "https://mac.ts.net/")


# ---- host -------------------------------------------------------------------------------------
def test_a_name_that_is_not_ours_is_refused_even_with_a_valid_session(web):
    # DNS rebinding: a hostile site's name starts to point at this machine; it still says its own.
    for path in ("/", "/api/session", "/api/history"):
        response = web.client.get(path, headers={"Host": "attacker.example"})
        assert response.status_code == 421, path


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_our_own_names_are_served(web, host):
    assert web.client.get("/", headers={"Host": host}).status_code == 200


def test_a_name_that_merely_contains_ours_is_refused(web):
    for host in ("127.0.0.1.attacker.example", "localhost.evil.test", "evil.test:127.0.0.1"):
        assert web.client.get("/", headers={"Host": host}).status_code == 421


# ---- origin -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3000",  # another program on this machine: same site, other port
        "http://127.0.0.1:8766",
        "http://127.0.0.1",
        "https://127.0.0.1:8765",
        "http://evil.example",
        "null",
        "http://127.0.0.1:8765.evil.example",
    ],
)
def test_a_request_from_another_origin_is_refused_even_for_reading(web, origin):
    response = web.client.get("/api/session", headers={"Origin": origin})
    assert response.status_code == 403
    assert "agent" not in response.text


def test_our_own_origin_is_accepted_for_reading(web):
    assert web.client.get("/api/session", headers={"Origin": ORIGIN}).status_code == 200
    local = f"http://localhost:{PORT}"
    assert web.client.get("/api/session", headers={"Origin": local}).status_code == 200


def test_origin_is_compared_case_insensitively_but_otherwise_exactly(web):
    assert web.client.get("/api/session", headers={"Origin": ORIGIN.upper()}).status_code == 200
    assert web.client.get("/api/session", headers={"Origin": ORIGIN + "/"}).status_code == 403


# ---- requests that change something ------------------------------------------------------------
def post(web, headers):
    return web.client.post("/api/logout", headers=headers)


def test_a_change_must_say_where_it_came_from(web):
    assert post(web, {"X-PA": "1"}).status_code == 403
    assert web.client.get("/api/session").status_code == 200  # still signed in


def test_a_change_from_another_origin_is_refused(web):
    assert post(web, {"Origin": "http://localhost:3000", "X-PA": "1"}).status_code == 403
    assert web.client.get("/api/session").status_code == 200


def test_a_change_without_the_page_header_is_refused(web):
    # A form on another site can send a cookie and an Origin but cannot add this header.
    assert post(web, {"Origin": ORIGIN}).status_code == 403
    assert post(web, {"Origin": ORIGIN, "X-PA": "0"}).status_code == 403
    assert web.client.get("/api/session").status_code == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_change_the_browser_says_came_from_another_site_is_refused(web, site):
    response = post(web, {**web.write, "Sec-Fetch-Site": site})
    assert response.status_code == 403
    assert web.client.get("/api/session").status_code == 200


def test_a_change_from_the_page_itself_goes_through(web):
    assert post(web, {**web.write, "Sec-Fetch-Site": "same-origin"}).status_code == 200


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_methods_need_the_same_proof_and_are_not_routes_anyway(web, method):
    response = web.client.request(method, "/api/session", headers={"X-PA": "1"})
    assert response.status_code == 403
    response = web.client.request(method, "/api/session", headers=web.write)
    assert response.status_code == 405


# ---- fetch metadata ----------------------------------------------------------------------------
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_the_api_refuses_reads_the_browser_says_came_from_another_site(web, site):
    # A page on another port of this machine can embed things (an image, a script) that carry our
    # cookie but send no Origin. The browser still says where they came from.
    assert web.client.get("/api/session", headers={"Sec-Fetch-Site": site}).status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_the_api_serves_the_page_and_the_owner_typing_an_address(web, site):
    assert web.client.get("/api/session", headers={"Sec-Fetch-Site": site}).status_code == 200


def test_the_page_itself_can_be_opened_from_a_link_on_another_site(web):
    # Following a pairing link out of an email or a chat is a cross-site navigation.
    assert web.client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


# ---- never any CORS ----------------------------------------------------------------------------
def test_a_preflight_from_another_origin_is_refused_and_nothing_grants_access(web):
    response = web.client.options(
        "/api/messages",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-pa",
        },
    )
    assert response.status_code == 403
    assert not any(k.startswith("access-control-") for k in headers_of(response))


def test_no_response_ever_carries_a_cors_header(web):
    for path in ("/", "/sw.js", "/api/session", "/api/history", "/api/approvals", "/nope"):
        response = web.client.get(path, headers={"Origin": ORIGIN})
        assert not any(k.startswith("access-control-") for k in headers_of(response)), path


def test_a_websocket_is_refused(web):
    with pytest.raises(WebSocketDisconnect), web.client.websocket_connect("/api/stream"):
        pass


# ---- size --------------------------------------------------------------------------------------
def test_a_declared_oversize_body_is_refused_before_anything_reads_it(web):
    response = web.client.post(
        "/api/logout",
        content=b"x" * (65 * 1024),
        headers={**web.write, "Content-Type": "text/plain"},
    )
    assert response.status_code == 413
    assert web.client.get("/api/session").status_code == 200  # the body was never looked at


def test_a_streamed_oversize_body_is_refused_too(web):
    def chunks():
        for _ in range(100):  # no Content-Length: chunked
            yield b"x" * 1024

    response = web.client.post(
        "/api/pair", content=chunks(), headers={**web.write, "Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_a_message_may_be_big_enough_for_its_pictures_but_not_more(make_app):
    from personal_agent.channels.web.api import body_limit_for
    from personal_agent.config import Config, MediaConfig

    app = make_app(
        config=Config(media=MediaConfig(max_image_bytes=100_000, max_voice_bytes=50_000))
    )
    limit = body_limit_for(app)
    assert limit("POST", "/api/messages") == 4 * 100_000 + 50_000 + 256 * 1024
    assert limit("POST", "/api/pair") == 64 * 1024
    assert limit("GET", "/api/messages") == 64 * 1024


# ---- headers on everything ---------------------------------------------------------------------
def expected_headers():
    return {k: v for k, v in SECURITY_HEADERS.items()}


@pytest.mark.parametrize("path", ["/", "/sw.js", "/manifest.webmanifest", "/api/session", "/nope"])
def test_every_response_carries_the_security_headers(web, path):
    found = headers_of(web.client.get(path))
    for key, value in expected_headers().items():
        assert found.get(key) == value, (path, key)


def test_refusals_carry_them_too(web):
    refused = web.client.get("/", headers={"Host": "attacker.example"})
    assert refused.status_code == 421
    found = headers_of(refused)
    assert all(found.get(k) == v for k, v in expected_headers().items())


def test_the_policy_allows_nothing_from_elsewhere_and_no_inline_code():
    directives = dict(part.strip().split(" ", 1) for part in CSP.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert directives["connect-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'none'" and directives["form-action"] == "'none'"
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP
    # A picture the agent was talked into printing cannot carry the owner's data out in its address.
    assert "http:" not in directives["img-src"] and "https:" not in directives["img-src"]
    assert directives["img-src"].split() == ["'self'", "blob:", "data:"]


def test_the_page_cannot_be_framed_or_leak_its_address(web):
    found = headers_of(web.client.get("/"))
    assert found["x-frame-options"] == "DENY" and found["referrer-policy"] == "no-referrer"
    assert found["x-content-type-options"] == "nosniff"


def test_only_the_microphone_is_requested_from_the_browser(web):
    policy = headers_of(web.client.get("/"))["permissions-policy"]
    assert "microphone=(self)" in policy and "camera=()" in policy and "geolocation=()" in policy


# ---- the public address ------------------------------------------------------------------------
def test_behind_a_tailnet_name_the_public_origin_and_host_are_ours(make_app):
    site = Web(make_app(), public_url="https://mac.tail1234.ts.net")
    site.pair()
    ok = site.client.get("/api/session", headers={"Origin": "https://mac.tail1234.ts.net"})
    assert ok.status_code == 200
    wrong_scheme = site.client.get("/api/session", headers={"Origin": "http://mac.tail1234.ts.net"})
    assert wrong_scheme.status_code == 403
    other_name = site.client.get("/api/session", headers={"Origin": "https://other.ts.net"})
    assert other_name.status_code == 403
    assert site.client.get("/", headers={"Host": "other.ts.net"}).status_code == 421


def test_the_owner_can_name_more_hosts(make_app):
    site = Web(make_app(), extra_hosts=["phone.local"])
    assert site.client.get("/", headers={"Host": "phone.local:8765"}).status_code == 200
    assert site.client.get("/", headers={"Host": "other.local"}).status_code == 421
