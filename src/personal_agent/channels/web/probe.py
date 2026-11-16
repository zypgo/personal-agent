"""Prove a listening HTTP service belongs to this agent home before exposing it to a phone.

The public probe signs a fresh random challenge; it reveals neither a credential nor a stable
home identifier. The signing key is only for this protocol, never a browser authentication key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

import httpx

from personal_agent.channels.web.connect import ConnectionError, port_available
from personal_agent.db import Database

PROBE_KEY = "web:connection-probe-key"


def probe_signature(db: Database, challenge: str) -> str:
    # The health route needs no sign-in, so the common case is a plain read; only the first call
    # (and only once, even if two race) takes the write lock to create the key.
    key = db.kv_get(PROBE_KEY)
    if key is None:
        with db.tx():
            key = db.kv_get(PROBE_KEY)
            if key is None:
                key = secrets.token_hex(32)
                db.kv_set(PROBE_KEY, key, now=0)
    return hmac.new(
        bytes.fromhex(str(key)),
        b"personal-agent-phone-probe\0" + challenge.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_backend(db: Database, port: int, *, host: str = "127.0.0.1") -> None:
    if port_available(port, host=host):
        return
    verify_endpoint(db, f"http://{host}:{port}")


def verify_endpoint(db: Database, url: str) -> None:
    """Require a live response signed by this home; an unrelated HTTP 200 is not success."""
    challenge = secrets.token_hex(16)
    expected = probe_signature(db, challenge)
    deadline = time.monotonic() + 8
    try:
        with (
            httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client,
            client.stream(
                "GET",
                url + "/api/health",
                params={"challenge": challenge},
                headers={"Accept-Encoding": "identity"},
            ) as response,
        ):
            if (
                response.status_code != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise ValueError("not the agent's probe")
            body = bytearray()
            for chunk in response.iter_raw():
                if time.monotonic() >= deadline:
                    raise ValueError("probe deadline exceeded")
                if len(body) + len(chunk) > 4096:
                    raise ValueError("oversized probe")
                body.extend(chunk)
            data = json.loads(body)
        proof = data.get("proof") if isinstance(data, dict) else None
        if not isinstance(proof, str) or not hmac.compare_digest(expected, proof):
            raise ValueError("different home")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise ConnectionError(
            "The listening service cannot be verified as this agent. "
            "Restart this agent on the new version, stop the other service, or choose another --port."
        ) from exc
