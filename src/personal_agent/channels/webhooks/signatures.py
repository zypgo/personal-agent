"""How a webhook sender proves who it is.

A route is pinned to one scheme in config.toml. The scheme is never worked out from the request:
whoever can choose how a delivery is checked will choose the weakest way.

          what the sender signs             a replayed delivery is stopped by
github    the body                          its delivery id (the inbox keeps what it has seen)
gitlab    nothing: a shared token that      its delivery id; use `standard` where GitLab offers a
          travels as plain text             signing token, it is the stronger of the two
standard  id + timestamp + body             the timestamp window and the id (Standard Webhooks,
                                            the scheme of Svix and of recent GitLab)
generic   timestamp + body                  the timestamp window and the signature itself (for
                                            scripts: `pa webhooks send`, or curl and openssl)

Every comparison takes the same time whatever the input. The body is read exactly as it arrived:
parsing JSON and writing it again changes bytes, and a signature is over bytes.

Nothing in a `Rejected` message comes from the request, so it is safe to log.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

SCHEMES = ("github", "gitlab", "standard", "generic")

MIN_SECRET_BYTES = 16
"""A shorter secret can be guessed offline from one captured delivery."""

DEFAULT_TOLERANCE_S = 300

_WHSEC = "whsec_"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}")
_SECONDS = re.compile(r"[0-9]{1,12}")
_NOT_NAME = re.compile(r"[^a-z0-9.]+")


class Rejected(Exception):
    """The delivery is not proven to come from the sender. The text is for the owner's log; the
    sender is told nothing."""


class SecretProblem(ValueError):
    """A route's secret cannot be used. The text says what is wrong and what to do."""


@dataclass(frozen=True)
class Verified:
    event: str = ""
    """What happened, when a header says so ("" otherwise), as `norm_event` writes it."""
    delivery: str = ""
    """The sender's id for this delivery, the same on every retry ("" if it sends none)."""


# ---- secrets -------------------------------------------------------------------------------
def secret_key(scheme: str, secret: str) -> bytes:
    """The bytes a route's secret stands for under its scheme, or `SecretProblem`."""
    if scheme not in SCHEMES:
        raise SecretProblem(f"unknown scheme {scheme!r}")
    if not secret.strip():
        raise SecretProblem("the secret is empty: anyone could then sign a delivery")
    if scheme == "standard":
        text = secret.strip().removeprefix(_WHSEC)
        try:
            key = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise SecretProblem(
                f"a Standard Webhooks secret is '{_WHSEC}' followed by base64 text"
            ) from None
        if len(key) < MIN_SECRET_BYTES:
            raise SecretProblem(f"the secret must hold at least {MIN_SECRET_BYTES} bytes")
        return key
    key = secret.encode("utf-8")
    if len(key) < MIN_SECRET_BYTES:
        raise SecretProblem(f"the secret is shorter than {MIN_SECRET_BYTES} characters")
    return key


def new_secret(scheme: str) -> str:
    """A fresh secret in the shape `scheme` expects."""
    raw = secrets.token_bytes(32)
    if scheme == "standard":
        return _WHSEC + base64.b64encode(raw).decode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ---- checking ------------------------------------------------------------------------------
def verify(
    scheme: str,
    key: bytes,
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: float,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
) -> Verified:
    """Proof that `body` came from whoever holds the secret, or `Rejected`.

    `headers` may be spelled any way; a header sent twice must already be joined with ", " (that
    can never verify, which is the point)."""
    h = {name.lower(): value for name, value in headers.items()}
    if scheme == "github":
        return _github(key, h, body)
    if scheme == "gitlab":
        return _gitlab(key, h)
    if scheme == "standard":
        return _standard(key, h, body, now, tolerance_s)
    if scheme == "generic":
        return _generic(key, h, body, now, tolerance_s)
    raise Rejected("the route's scheme is not one that is known")


def _github(key: bytes, h: dict[str, str], body: bytes) -> Verified:
    given = h.get("x-hub-signature-256", "")
    if not given.startswith("sha256="):  # the sha1 header of old is not accepted
        raise Rejected("no X-Hub-Signature-256 header")
    if not _same(given[7:].lower(), _hmac_hex(key, body)):
        raise Rejected("the signature does not match")
    return Verified(
        event=norm_event(h.get("x-github-event")), delivery=_id(h.get("x-github-delivery"))
    )


def _gitlab(key: bytes, h: dict[str, str]) -> Verified:
    given = h.get("x-gitlab-token", "")
    if not given or not _same(given, key):
        raise Rejected("the X-Gitlab-Token is missing or wrong")
    return Verified(
        event=norm_event(h.get("x-gitlab-event")),
        delivery=_id(h.get("idempotency-key") or h.get("webhook-id")),
    )


def _standard(key: bytes, h: dict[str, str], body: bytes, now: float, tolerance_s: int) -> Verified:
    prefix = "webhook" if "webhook-signature" in h else "svix"  # Svix's own headers, same maths
    message_id = h.get(f"{prefix}-id", "")
    stamp = h.get(f"{prefix}-timestamp", "")
    signatures = h.get(f"{prefix}-signature", "")
    if not (message_id and stamp and signatures):
        raise Rejected("webhook-id, webhook-timestamp or webhook-signature is missing")
    _fresh(stamp, now, tolerance_s)
    signed = f"{message_id}.{stamp}.".encode("latin-1", "replace") + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
    found = False
    for candidate in signatures.split():  # several while the sender rotates its secret
        version, _, value = candidate.partition(",")
        if version == "v1" and _same(value, expected):  # `v1a` is asymmetric: not supported
            found = True  # no early exit: how many were tried is not a signal
    if not found:
        raise Rejected("the signature does not match")
    return Verified(delivery=_id(message_id))


def _generic(key: bytes, h: dict[str, str], body: bytes, now: float, tolerance_s: int) -> Verified:
    stamp = h.get("x-webhook-timestamp", "")
    given = h.get("x-webhook-signature", "")
    if not stamp or not given.startswith("sha256="):
        raise Rejected("X-Webhook-Timestamp or X-Webhook-Signature is missing")
    _fresh(stamp, now, tolerance_s)
    signature = given[7:].lower()
    if not _same(signature, _hmac_hex(key, stamp.encode("latin-1", "replace") + b"." + body)):
        raise Rejected("the signature does not match")
    # Nothing else in this scheme is signed, so the signature is the delivery's identity: a
    # captured request replayed with another header is still the same delivery.
    return Verified(delivery=signature[:40])


# ---- helpers -------------------------------------------------------------------------------
def _hmac_hex(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _same(given: str | bytes, expected: str | bytes) -> bool:
    a = given.encode("latin-1", "replace") if isinstance(given, str) else given
    b = expected.encode("latin-1", "replace") if isinstance(expected, str) else expected
    return hmac.compare_digest(a, b)


def _fresh(stamp: str, now: float, tolerance_s: int) -> None:
    if not _SECONDS.fullmatch(stamp):
        raise Rejected("the timestamp is not a number of seconds")
    if abs(now - int(stamp)) > tolerance_s:
        raise Rejected(
            "the timestamp is outside the allowed window (a replay, or the clocks differ)"
        )


def norm_event(value: object) -> str:
    """An event's name in one spelling. GitHub's `pull_request`, GitLab's "Merge Request Hook" and
    what the owner wrote in `events` all come out alike. Only lower-case letters, digits, `_` and
    `.` survive, and at most 48 of them: a name is short and has no spaces or punctuation, which
    is all that can be said for a word from the sender that is shown outside the data fence."""
    if not isinstance(value, str):
        return ""
    text = _NOT_NAME.sub("_", value.lower()).strip("_.")
    return text.removesuffix("_hook")[:48].strip("_.")


def _id(value: str | None) -> str:
    return value if value and _ID.fullmatch(value) else ""


def fallback_id(body: bytes, now: float) -> str:
    """An id for a delivery whose sender sent none: the same body on the same (UTC) day counts as
    the same delivery, so a sender that retries is not heard twice."""
    day = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d")
    return f"sha256:{hashlib.sha256(body).hexdigest()[:32]}:{day}"


# ---- the sending side ----------------------------------------------------------------------
def sign(
    scheme: str,
    secret: str,
    body: bytes,
    *,
    now: float,
    event: str = "",
    delivery: str = "",
) -> dict[str, str]:
    """The headers a sender of `scheme` would add to `body`: for `pa webhooks send` (a way to try a
    route) and for the tests, which check every scheme against what its own documentation says."""
    key = secret_key(scheme, secret)
    stamp = str(int(now))
    delivery = delivery or uuid.uuid4().hex
    if scheme == "github":
        return {
            "X-Hub-Signature-256": "sha256=" + _hmac_hex(key, body),
            "X-GitHub-Event": event or "ping",
            "X-GitHub-Delivery": delivery,
        }
    if scheme == "gitlab":
        return {
            "X-Gitlab-Token": secret,
            "X-Gitlab-Event": event or "Push Hook",
            "Idempotency-Key": delivery,
        }
    if scheme == "standard":
        message_id = f"msg_{delivery}"
        signed = f"{message_id}.{stamp}.".encode() + body
        digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
        return {
            "webhook-id": message_id,
            "webhook-timestamp": stamp,
            "webhook-signature": f"v1,{digest}",
        }
    return {
        "X-Webhook-Timestamp": stamp,
        "X-Webhook-Signature": "sha256=" + _hmac_hex(key, stamp.encode() + b"." + body),
    }
