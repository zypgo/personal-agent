from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from personal_agent.channels.dingtalk.api import (
    MESSAGE_TOPIC,
    OPEN_PATH,
    SEND_PATH,
    TOKEN_HEADER,
    TOKEN_PATH,
    DeliveryUncertain,
    DingTalkApi,
    DingTalkApiError,
    stream_url,
)


class FakeDingTalk:
    """Exercise the real HTTP adapter without connecting to DingTalk or holding credentials."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses = defaultdict(deque)

    def script(self, path, *results):
        self.responses[path].extend(results)

    async def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "api.dingtalk.com"
        assert request.method == "POST" and not request.url.query
        path = request.url.path
        if self.responses[path]:
            result = self.responses[path].popleft()
            if isinstance(result, Exception):
                raise result
            return (
                result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)
            )
        if path == TOKEN_PATH:
            return httpx.Response(200, json={"accessToken": "offline-access", "expireIn": 7200})
        if path == OPEN_PATH:
            return httpx.Response(
                200,
                json={
                    "endpoint": "wss://wss-open-connection.dingtalk.com:443/connect",
                    "ticket": "offline-ticket",
                },
            )
        assert path == SEND_PATH
        return httpx.Response(200, json={"processQueryKey": "accepted-offline"})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def calls(self, path):
        return [r for r in self.requests if r.url.path == path]

    def sent(self):
        return [json.loads(r.content) for r in self.calls(SEND_PATH)]


@pytest.fixture
async def rig():
    fake = FakeDingTalk()
    async with fake.client() as client:
        yield fake, DingTalkApi("offline-app", "offline-secret", client=client)


async def test_open_connection_uses_official_subscription_and_encoded_one_use_ticket(rig):
    fake, api = rig
    fake.script(OPEN_PATH, {"endpoint": "wss://gateway.dingtalk.com/connect", "ticket": "a+b&c?"})
    result = await api.open_connection()
    assert parse_qs(urlsplit(result).query) == {"ticket": ["a+b&c?"]}
    [request] = fake.calls(OPEN_PATH)
    assert json.loads(request.content) == {
        "clientId": "offline-app",
        "clientSecret": "offline-secret",
        "subscriptions": [{"type": "CALLBACK", "topic": MESSAGE_TOPIC}],
        "ua": "personal-agent-python/1",
    }
    assert TOKEN_HEADER not in request.headers


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://gateway.dingtalk.com/connect",
        "wss://dingtalk.com.evil.invalid/connect",
        "wss://notdingtalk.com/connect",
        "wss://gateway.dingtalk.com:8080/connect",
        "wss://user@gateway.dingtalk.com/connect",
        "wss://gateway.dingtalk.com/connect?leak=1",
        "wss://gateway.dingtalk.com/connect#fragment",
        "wss://127.0.0.1/connect",
        "wss://gateway.dingtalk.com:bad/connect",
        "https://gateway.dingtalk.com/connect",
    ],
)
def test_stream_tickets_never_go_to_an_untrusted_endpoint(endpoint):
    with pytest.raises(DingTalkApiError):
        stream_url(endpoint, "offline-ticket")


async def test_send_uses_staff_id_json_string_params_and_token_header(rig):
    fake, api = rig
    assert await api.send_message("robot", "owner", "你好 **world**") == "accepted-offline"
    assert fake.sent() == [
        {
            "robotCode": "robot",
            "userIds": ["owner"],
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": "栖序", "text": "你好 **world**"}, ensure_ascii=False),
        }
    ]
    assert fake.calls(SEND_PATH)[0].headers[TOKEN_HEADER] == "offline-access"
    assert json.loads(fake.calls(TOKEN_PATH)[0].content) == {
        "appKey": "offline-app",
        "appSecret": "offline-secret",
    }
    await api.send_message("robot", "owner", "pairing", markdown=False)
    assert fake.sent()[1]["msgKey"] == "sampleText"
    assert json.loads(fake.sent()[1]["msgParam"]) == {"content": "pairing"}
    assert len(fake.calls(TOKEN_PATH)) == 1


async def test_token_refreshes_before_expiry_and_parallel_sends_share_refresh():
    fake = FakeDingTalk()
    now = [100.0]
    async with fake.client() as client:
        api = DingTalkApi("app", "secret", client=client, monotonic=lambda: now[0])
        await asyncio.gather(*(api.send_message("robot", f"owner{i}", "hi") for i in range(8)))
        assert len(fake.calls(TOKEN_PATH)) == 1
        now[0] += 7141
        await api.send_message("robot", "owner", "after refresh")
        assert len(fake.calls(TOKEN_PATH)) == 2


async def test_explicit_expired_token_refetches_once_and_retries_with_new_token(rig):
    fake, api = rig
    fake.script(
        TOKEN_PATH,
        {"accessToken": "first", "expireIn": 7200},
        {"accessToken": "second", "expireIn": 7200},
    )
    fake.script(SEND_PATH, httpx.Response(401, json={"code": "InvalidAuthentication"}))
    await api.send_message("robot", "owner", "hi")
    assert [r.headers[TOKEN_HEADER] for r in fake.calls(SEND_PATH)] == ["first", "second"]
    assert len(fake.calls(TOKEN_PATH)) == 2


async def test_persistent_authentication_failure_is_bounded_and_sanitized(rig):
    fake, api = rig
    fake.script(SEND_PATH, *[httpx.Response(401, json={"message": "offline-secret"})] * 2)
    with pytest.raises(DingTalkApiError) as caught:
        await api.send_message("robot", "owner", "hi")
    assert caught.value.permanent and "offline-secret" not in str(caught.value)
    assert len(fake.calls(SEND_PATH)) == len(fake.calls(TOKEN_PATH)) == 2


@pytest.mark.parametrize(
    "key,permanent",
    [
        ("invalidStaffIdList", True),
        ("filteredStaffIdList", True),
        ("flowControlledStaffIdList", False),
    ],
)
async def test_batch_receipt_does_not_hide_rejected_or_rate_limited_recipient(rig, key, permanent):
    fake, api = rig
    fake.script(SEND_PATH, {"processQueryKey": "accepted-for-somebody", key: ["owner"]})
    with pytest.raises(DingTalkApiError) as caught:
        await api.send_message("robot", "owner", "hi")
    assert caught.value.permanent is permanent
    assert caught.value.retry_after == (None if permanent else 60)
    assert len(fake.calls(SEND_PATH)) == 1


async def test_http_rate_limit_uses_retry_after_without_a_hidden_resend(rig):
    fake, api = rig
    fake.script(SEND_PATH, httpx.Response(429, headers={"Retry-After": "23"}, json={}))
    with pytest.raises(DingTalkApiError) as caught:
        await api.send_message("robot", "owner", "hi")
    assert caught.value.retry_after == 23 and not caught.value.permanent
    assert len(fake.calls(SEND_PATH)) == 1


async def test_optional_empty_recipient_results_are_accepted(rig):
    fake, api = rig
    fake.script(
        SEND_PATH,
        {
            "processQueryKey": "accepted",
            "invalidStaffIdList": None,
            "filteredStaffIdList": [],
            "flowControlledStaffIdList": [],
        },
    )
    assert await api.send_message("robot", "owner", "hi") == "accepted"


@pytest.mark.parametrize(
    "result",
    [
        httpx.ReadTimeout("sensitive request URL"),
        httpx.WriteError("sensitive request body"),
        httpx.Response(503, json={"message": "sensitive response"}),
        httpx.Response(202, json={}),
        httpx.Response(204),
        httpx.Response(302, headers={"Location": "https://evil.invalid/send"}),
        httpx.Response(408),
        {},
        {"processQueryKey": "ok", "filteredStaffIdList": "owner"},
        {"processQueryKey": "ok", "invalidStaffIdList": [123]},
        httpx.Response(200, content=b"<not-json>"),
    ],
)
async def test_unknown_delivery_is_not_automatically_retried(rig, result):
    fake, api = rig
    fake.script(SEND_PATH, result)
    with pytest.raises(DeliveryUncertain) as caught:
        await api.send_message("robot", "owner", "hi")
    assert "sensitive" not in str(caught.value)
    assert len(fake.calls(SEND_PATH)) == 1


async def test_failure_before_connecting_is_known_non_delivery(rig):
    fake, api = rig
    fake.script(SEND_PATH, httpx.ConnectError("sensitive request URL"))
    with pytest.raises(DingTalkApiError) as caught:
        await api.send_message("robot", "owner", "hi")
    assert not caught.value.permanent and "sensitive" not in str(caught.value)


async def test_http_redirects_never_forward_credentials_or_send_to_another_host(rig):
    fake, api = rig
    fake.script(TOKEN_PATH, httpx.Response(307, headers={"Location": "https://evil.invalid/token"}))
    with pytest.raises(DingTalkApiError):
        await api.send_message("robot", "owner", "hi")
    assert len(fake.requests) == 1 and not fake.calls(SEND_PATH)


async def test_token_failure_cannot_be_mistaken_for_an_uncertain_message_send(rig):
    fake, api = rig
    fake.script(TOKEN_PATH, httpx.ReadTimeout("secret"))
    with pytest.raises(DingTalkApiError):
        await api.send_message("robot", "owner", "hi")
    assert not fake.calls(SEND_PATH)


# ---- Gaps found by breaking the code on purpose (scripts/mutation/lists/v02_channels_files.py) ----


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://gateway.dingtalk.com/connect",
        "wss://notdingtalk.com/connect",
        "wss://gateway.dingtalk.com:8080/connect",
        "wss://user@gateway.dingtalk.com/connect",
        "wss://:secret@gateway.dingtalk.com/connect",
        "wss://gateway.dingtalk.com/connect?leak=1",
        "wss://gateway.dingtalk.com/connect#fragment",
        "wss://gateway.dingtalk.com",
        "https://gateway.dingtalk.com/connect",
    ],
)
def test_an_untrusted_endpoint_is_refused_for_good_not_retried(endpoint):
    with pytest.raises(DingTalkApiError) as caught:
        stream_url(endpoint, "offline-ticket")
    assert caught.value.permanent


@pytest.mark.parametrize(
    "code, kept",
    [
        ("InvalidParameter.Staff", "InvalidParameter.Staff"),
        ("has a space", ""),
        ("offline-secret-echoed-back " * 4, ""),
        ("x" * 120, "x" * 120),
        ("x" * 121, ""),
    ],
)
async def test_only_an_identifier_shaped_error_code_is_kept(rig, code, kept):
    fake, api = rig
    fake.script(SEND_PATH, httpx.Response(400, json={"code": code}))
    with pytest.raises(DingTalkApiError) as caught:
        await api.send_message("robot", "owner", "hi")
    assert caught.value.code == kept
    assert "offline-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "header, seconds",
    [
        ("23", 23.0),
        ("0", 1.0),
        ("-5", 1.0),
        ("99999", 3600.0),
        ("nan", 60.0),
        ("inf", 60.0),
        ("soon", 60.0),
    ],
)
async def test_retry_after_is_kept_within_what_a_person_can_wait_for(rig, header, seconds):
    fake, api = rig
    fake.script(SEND_PATH, httpx.Response(429, headers={"Retry-After": header}, json={}))
    with pytest.raises(DingTalkApiError) as caught:
        await api.send_message("robot", "owner", "hi")
    assert caught.value.retry_after == seconds


@pytest.mark.parametrize("lifetime", ["true", "0", "-1", "Infinity", "NaN", '"7200"', "null"])
async def test_an_implausible_token_lifetime_is_refused_not_cached(rig, lifetime):
    fake, api = rig
    body = f'{{"accessToken":"offline-access","expireIn":{lifetime}}}'.encode()
    fake.script(
        TOKEN_PATH,
        httpx.Response(200, content=body, headers={"content-type": "application/json"}),
    )
    with pytest.raises(DingTalkApiError, match="invalid access token"):
        await api.access_token()
