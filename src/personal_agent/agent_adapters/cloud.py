"""Wake an existing cloud agent without confusing delivery with task completion.

Grok Bot routines use Bearer authentication and HTTP 200. Grok Automations' HMAC/202
interface is a different product. Muse has no assumed generic webhook: a hook is usable
only after the owner verifies that exact account's endpoint and acceptance contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from personal_agent.config import NetConfig
from personal_agent.security import ssrf
from personal_agent.security.redact import register_secret_envs

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


@dataclass(frozen=True)
class CloudWakeConfig:
    provider: str
    mode: str
    url_env: str = ""
    secret_env: str = ""
    hook_verified: bool = False
    accepted_status: int = 200


@dataclass(frozen=True)
class WakeReceipt:
    state: str
    code: str
    http_status: int | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        return {"state": self.state, "code": self.code, "http_status": self.http_status}


async def wake_cloud_agent(
    config: CloudWakeConfig,
    *,
    agent_id: str,
    task_id: str,
    wake_id: str,
    net: NetConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> WakeReceipt:
    """Send one bounded wake. Never retry; an unknown delivery must be reconciled first.

    The caller persists its dispatch attempt before this call. No credential, URL, response
    body, task prompt, or provider exception is included in the receipt.
    """
    if config.provider not in {"grok", "grok_bot", "muse"}:
        return WakeReceipt("blocked", "unsupported_provider")
    if config.mode == "manual":
        return WakeReceipt("manual_required", "manual_start")
    if config.mode == "native_schedule":
        return WakeReceipt("awaiting_schedule", "native_schedule")
    if config.mode == "grok_routine":
        if config.provider not in {"grok", "grok_bot"}:
            return WakeReceipt("blocked", "unsupported_wake_mode")
        accepted_status = 200
    elif config.mode == "verified_hook":
        if not config.hook_verified:
            return WakeReceipt("blocked", "hook_not_verified")
        if type(config.accepted_status) is not int or not 200 <= config.accepted_status < 300:
            return WakeReceipt("blocked", "invalid_acceptance_contract")
        accepted_status = config.accepted_status
    else:
        return WakeReceipt("blocked", "unsupported_wake_mode")
    if any(not IDENTIFIER.fullmatch(value) for value in (agent_id, task_id, wake_id)):
        return WakeReceipt("blocked", "invalid_identity")
    if not ENV_NAME.fullmatch(config.url_env) or not ENV_NAME.fullmatch(config.secret_env):
        return WakeReceipt("blocked", "invalid_secret_reference")
    register_secret_envs([config.url_env, config.secret_env])
    environment = os.environ if environ is None else environ
    url, key = environment.get(config.url_env, ""), environment.get(config.secret_env, "")
    if not url or not key:
        return WakeReceipt("blocked", "missing_secret")
    if (
        len(key) > 8192
        or not key.isascii()
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in key)
    ):
        return WakeReceipt("blocked", "invalid_secret")
    try:
        parsed = ssrf.check_url(url)
        if parsed.scheme != "https" or parsed.fragment:
            return WakeReceipt("blocked", "https_required")
    except ssrf.Blocked:
        return WakeReceipt("blocked", "invalid_endpoint")
    configured = net or NetConfig()
    bounded = NetConfig(
        allow_private=configured.allow_private,
        timeout_s=min(configured.timeout_s, 10.0),
        deadline_s=min(configured.deadline_s, 15.0),
        max_response_bytes=16_384,
        max_redirects=0,
        user_agent="personal-agent/cloud-wake-v1",
    )
    payload = json.dumps(
        {
            "schema": "pa.cloud-wake.v1",
            "agent_id": agent_id,
            "task_id": task_id,
            "wake_id": wake_id,
        },
        separators=(",", ":"),
    ).encode()
    try:
        async with asyncio.timeout(bounded.deadline_s):
            async with (
                httpx.AsyncClient(
                    transport=ssrf.GuardedTransport(ssrf.NetPolicy.from_config(bounded)),
                    timeout=httpx.Timeout(bounded.timeout_s),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    parsed,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "Accept-Encoding": "identity",
                        "User-Agent": bounded.user_agent,
                    },
                    content=payload,
                ) as response,
            ):
                # The acknowledgement is its status. Do not read an untrusted body at all.
                status = response.status_code
    except ssrf.Blocked:
        return WakeReceipt("blocked", "network_policy_or_redirect")
    except (httpx.HTTPError, OSError, TimeoutError):
        return WakeReceipt("wake_unknown", "delivery_unconfirmed")
    if status == accepted_status:
        return WakeReceipt("wake_accepted", "accepted_not_completed", status)
    # A reverse proxy may return an error after the receiver has already started work.
    # Only the documented success contract is conclusive; do not infer non-execution.
    return WakeReceipt("wake_unknown", "unexpected_status", status)
