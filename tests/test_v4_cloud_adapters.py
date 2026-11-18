from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from personal_agent.agent_adapters.cloud import CloudWakeConfig, wake_cloud_agent
from personal_agent.config import NetConfig
from personal_agent.security import ssrf

ENV = {"BOT_URL": "https://hooks.example.test/private-route", "BOT_KEY": "test-bearer-secret"}


async def test_grok_ack_is_only_wake_acceptance_and_does_not_read_body(monkeypatch):
    seen = []

    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("A wake acknowledgement must not read a provider response body")
            yield b""  # pragma: no cover

    def handler(request):
        seen.append(request)
        return httpx.Response(200, stream=UnreadBody())

    monkeypatch.setattr(ssrf, "GuardedTransport", lambda _: httpx.MockTransport(handler))
    receipt = await wake_cloud_agent(
        CloudWakeConfig("grok_bot", "grok_routine", "BOT_URL", "BOT_KEY"),
        agent_id="agent_a",
        task_id="task_a",
        wake_id="wake_a",
        environ=ENV,
    )
    assert receipt.state == "wake_accepted"
    assert receipt.code == "accepted_not_completed"
    assert seen[0].headers["authorization"] == "Bearer test-bearer-secret"
    assert json.loads(seen[0].content) == {
        "schema": "pa.cloud-wake.v1",
        "agent_id": "agent_a",
        "task_id": "task_a",
        "wake_id": "wake_a",
    }
    assert all(value not in repr(receipt) for value in ENV.values())


@pytest.mark.parametrize("status", [202, 301, 302, 307, 308, 401, 429, 500])
async def test_grok_does_not_follow_redirects_or_accept_automations_202(monkeypatch, status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            status, headers={"location": "https://other.example.test"}, text=ENV["BOT_KEY"]
        )

    monkeypatch.setattr(ssrf, "GuardedTransport", lambda _: httpx.MockTransport(handler))
    receipt = await wake_cloud_agent(
        CloudWakeConfig("grok", "grok_routine", "BOT_URL", "BOT_KEY", accepted_status=202),
        agent_id="agent_a",
        task_id="task_a",
        wake_id="wake_a",
        environ=ENV,
    )
    assert receipt.state == "wake_unknown"
    assert receipt.http_status == status
    assert len(seen) == 1
    assert ENV["BOT_KEY"] not in repr(receipt)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("manual", "manual_required"),
        ("native_schedule", "awaiting_schedule"),
        ("verified_hook", "blocked"),
        ("grok_routine", "blocked"),
    ],
)
async def test_muse_never_invents_a_hook(monkeypatch, mode, expected):
    def forbidden(_):
        raise AssertionError("This Muse mode must not make an HTTP request")

    monkeypatch.setattr(ssrf, "GuardedTransport", forbidden)
    receipt = await wake_cloud_agent(
        CloudWakeConfig("muse", mode), agent_id="agent_a", task_id="task_a", wake_id="wake_a"
    )
    assert receipt.state == expected


async def test_muse_hook_requires_an_explicitly_verified_contract(monkeypatch):
    monkeypatch.setattr(
        ssrf, "GuardedTransport", lambda _: httpx.MockTransport(lambda _: httpx.Response(202))
    )
    receipt = await wake_cloud_agent(
        CloudWakeConfig(
            "muse", "verified_hook", "BOT_URL", "BOT_KEY", hook_verified=True, accepted_status=202
        ),
        agent_id="agent_a",
        task_id="task_a",
        wake_id="wake_a",
        environ=ENV,
    )
    assert receipt.state == "wake_accepted"


async def test_total_wake_deadline_is_unknown_and_not_retried(monkeypatch):
    calls = 0

    async def slow(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(10)
        return httpx.Response(200)

    monkeypatch.setattr(ssrf, "GuardedTransport", lambda _: httpx.MockTransport(slow))
    receipt = await wake_cloud_agent(
        CloudWakeConfig("grok", "grok_routine", "BOT_URL", "BOT_KEY"),
        agent_id="agent_a",
        task_id="task_a",
        wake_id="wake_a",
        environ=ENV,
        net=NetConfig(timeout_s=0.03, deadline_s=0.03),
    )
    assert receipt.state == "wake_unknown"
    assert calls == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/hook",
        "https://user:secret@example.test/hook",
        "https://example.test/hook#secret",
        "https://169.254.169.254/hook",
        "https://127.0.0.1/hook",
    ],
)
async def test_webhooks_require_https_and_the_existing_ssrf_gate(url):
    receipt = await wake_cloud_agent(
        CloudWakeConfig("grok", "grok_routine", "BOT_URL", "BOT_KEY"),
        agent_id="agent_a",
        task_id="task_a",
        wake_id="wake_a",
        environ={**ENV, "BOT_URL": url},
    )
    assert receipt.state == "blocked"
    assert url not in repr(receipt)
