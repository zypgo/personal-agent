"""Properties of how a webhook sender proves who it is. The example-based tests check each scheme
against its own documentation; these check what has to hold for *every* body, secret and header:

- what `sign` writes, `verify` accepts, and nothing else of the sender's choosing changes the answer;
- any change to what is signed, the key, or the clock is refused;
- junk can only be `Rejected`: never a crash, and never accepted;
- the words that survive from the sender (`norm_event`) are short, plain and safe to print.
"""

from __future__ import annotations

import base64
import re

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from personal_agent.channels.webhooks.signatures import (
    DEFAULT_TOLERANCE_S,
    MIN_SECRET_BYTES,
    SCHEMES,
    Rejected,
    SecretProblem,
    fallback_id,
    norm_event,
    secret_key,
    sign,
    verify,
)

NOW = 1_800_000_000.0
SIGNED_SCHEMES = ("github", "standard", "generic")  # gitlab sends a shared token, not a signature

TEXT = st.characters(blacklist_categories=("Cs",))
PLAIN_SECRET = st.text(TEXT, min_size=MIN_SECRET_BYTES, max_size=48).filter(
    lambda s: s.strip() and len(s.encode()) >= MIN_SECRET_BYTES
)
STANDARD_SECRET = st.binary(min_size=MIN_SECRET_BYTES, max_size=48).map(
    lambda raw: "whsec_" + base64.b64encode(raw).decode("ascii")
)
DELIVERY = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,40}", fullmatch=True)
BODY = st.binary(max_size=400)
EVENTS = st.one_of(st.none(), st.text(TEXT, max_size=60))


def secrets_for(scheme: str) -> st.SearchStrategy[str]:
    return STANDARD_SECRET if scheme == "standard" else PLAIN_SECRET


@st.composite
def senders(draw, schemes=SCHEMES):
    scheme = draw(st.sampled_from(schemes))
    return scheme, draw(secrets_for(scheme))


def prove(scheme, secret, body, **kw):
    """The headers a sender writes, as the service sees them: a sender puts UTF-8 bytes on the wire
    and the server reads each byte as one character (latin-1), which matters for a token that is
    not ASCII."""
    headers = sign(scheme, secret, body, now=NOW, **kw)
    return {name: value.encode("utf-8").decode("latin-1") for name, value in headers.items()}


# ---- a signed delivery is accepted, whatever it holds -------------------------------------
@given(senders(), BODY, DELIVERY, EVENTS)
def test_what_sign_writes_verify_accepts(sender, body, delivery, event):
    scheme, secret = sender
    headers = prove(scheme, secret, body, delivery=delivery, event=event or "")
    proven = verify(scheme, secret_key(scheme, secret), headers, body, now=NOW)
    assert proven.delivery in ("", delivery, f"msg_{delivery}") or scheme == "generic"
    if scheme == "generic":
        assert re.fullmatch(r"[0-9a-f]{40}", proven.delivery)
    assert re.fullmatch(r"[a-z0-9_.]{0,48}", proven.event)


@given(senders(), BODY, DELIVERY)
def test_header_names_may_be_written_in_any_case(sender, body, delivery):
    scheme, secret = sender
    headers = prove(scheme, secret, body, delivery=delivery)
    key = secret_key(scheme, secret)
    shouted = {name.upper(): value for name, value in headers.items()}
    quiet = {name.lower(): value for name, value in headers.items()}
    assert verify(scheme, key, shouted, body, now=NOW) == verify(scheme, key, quiet, body, now=NOW)


@given(senders(), BODY, DELIVERY)
def test_a_delivery_is_the_same_delivery_on_every_retry(sender, body, delivery):
    scheme, secret = sender
    key = secret_key(scheme, secret)
    headers = prove(scheme, secret, body, delivery=delivery)
    first = verify(scheme, key, headers, body, now=NOW)
    again = verify(scheme, key, headers, body, now=NOW + 5)
    assert first == again


# ---- and nothing else is ----------------------------------------------------------------------
@given(senders(SIGNED_SCHEMES), BODY, DELIVERY, st.data())
def test_any_change_to_the_body_is_refused(sender, body, delivery, data):
    scheme, secret = sender
    headers = prove(scheme, secret, body, delivery=delivery)
    if body and data.draw(st.booleans()):
        at = data.draw(st.integers(0, len(body) - 1))
        changed = body[:at] + bytes([body[at] ^ data.draw(st.integers(1, 255))]) + body[at + 1 :]
    else:
        changed = body + data.draw(st.binary(min_size=1, max_size=8))
    with pytest.raises(Rejected):
        verify(scheme, secret_key(scheme, secret), headers, changed, now=NOW)


@given(senders(), senders(), BODY, DELIVERY)
def test_another_secret_is_refused(sender, other, body, delivery):
    scheme, secret = sender
    _, wrong = other
    assume(secret_key(scheme, secret) != _key_or_none(scheme, wrong))
    headers = prove(scheme, secret, body, delivery=delivery)
    key = _key_or_none(scheme, wrong)
    assume(key is not None)
    with pytest.raises(Rejected):
        verify(scheme, key, headers, body, now=NOW)


def _key_or_none(scheme: str, secret: str) -> bytes | None:
    try:
        return secret_key(scheme, secret)
    except SecretProblem:
        return None


@given(
    senders(("standard", "generic")),
    BODY,
    DELIVERY,
    st.integers(DEFAULT_TOLERANCE_S + 1, 10**9),
    st.sampled_from([-1, 1]),
)
def test_a_signature_outside_the_time_window_is_refused(sender, body, delivery, drift, sign_of):
    scheme, secret = sender
    headers = prove(scheme, secret, body, delivery=delivery)
    with pytest.raises(Rejected):
        verify(scheme, secret_key(scheme, secret), headers, body, now=NOW + sign_of * drift)


@given(
    senders(("standard", "generic")),
    BODY,
    DELIVERY,
    st.integers(-DEFAULT_TOLERANCE_S, DEFAULT_TOLERANCE_S),
)
def test_a_signature_inside_the_time_window_is_accepted(sender, body, delivery, drift):
    scheme, secret = sender
    headers = prove(scheme, secret, body, delivery=delivery)
    verify(scheme, secret_key(scheme, secret), headers, body, now=NOW + drift)


@given(senders(SIGNED_SCHEMES), BODY, DELIVERY, st.text(TEXT, min_size=1, max_size=12))
def test_a_header_the_scheme_does_not_sign_cannot_change_the_answer_into_yes(
    sender, body, delivery, name
):
    scheme, secret = sender
    headers = prove(scheme, secret, body, delivery=delivery)
    key = secret_key(scheme, secret)
    other = verify(scheme, key, {**headers, f"x-extra-{name}": "1"}, body, now=NOW)
    assert other == verify(scheme, key, headers, body, now=NOW)


# ---- junk is only ever refused ---------------------------------------------------------------
KNOWN_HEADERS = [
    "x-hub-signature-256",
    "x-github-event",
    "x-github-delivery",
    "x-gitlab-token",
    "x-gitlab-event",
    "idempotency-key",
    "webhook-id",
    "webhook-timestamp",
    "webhook-signature",
    "svix-id",
    "svix-timestamp",
    "svix-signature",
    "x-webhook-timestamp",
    "x-webhook-signature",
]
JUNK_VALUE = st.one_of(
    st.text(TEXT, max_size=120),
    st.sampled_from(
        ["sha256=", "sha256=00", "v1,", "v1,AAAA", "v1a,xx v1,yy", "0", "-1", "1e9", " "]
    ),
    st.from_regex(r"[0-9]{1,14}", fullmatch=True),
    st.builds(lambda n: "9" * n, st.integers(1, 40)),
)
JUNK_HEADERS = st.one_of(
    st.dictionaries(st.sampled_from(KNOWN_HEADERS), JUNK_VALUE, max_size=6),
    st.dictionaries(st.text(TEXT, max_size=20), JUNK_VALUE, max_size=6),
)


@settings(max_examples=400)
@given(
    st.sampled_from(SCHEMES), JUNK_HEADERS, BODY, st.floats(0, 4e9), STANDARD_SECRET, PLAIN_SECRET
)
def test_junk_is_refused_and_never_crashes(scheme, headers, body, now, standard, plain):
    # GitLab sends the secret itself, so "junk" that happens to be the secret is the right answer
    assume(plain not in headers.values())
    key = secret_key(scheme, standard if scheme == "standard" else plain)
    with pytest.raises(Rejected):
        verify(scheme, key, headers, body, now=now)


@given(st.text(TEXT, max_size=40))
def test_a_scheme_that_is_not_known_is_refused(scheme):
    assume(scheme not in SCHEMES)
    with pytest.raises(Rejected):
        verify(scheme, b"k" * 32, {}, b"", now=NOW)
    with pytest.raises(SecretProblem):
        secret_key(scheme, "s" * 32)


# ---- secrets ----------------------------------------------------------------------------------
@given(st.sampled_from(SCHEMES), st.text(max_size=100))
def test_a_secret_is_either_usable_or_a_problem_that_says_so(scheme, secret):
    try:
        key = secret_key(scheme, secret)
    except SecretProblem as problem:
        assert str(problem)
    else:
        assert len(key) >= MIN_SECRET_BYTES


@given(st.sampled_from(SCHEMES), st.text(TEXT, max_size=12))
def test_a_short_secret_is_never_accepted(scheme, secret):
    assume(len(secret.encode()) < MIN_SECRET_BYTES)
    with pytest.raises(SecretProblem):
        secret_key(scheme, secret)


# ---- words from the sender ----------------------------------------------------------------------
@given(st.one_of(st.text(max_size=200), st.none(), st.integers(), st.binary(max_size=20)))
def test_an_event_name_is_short_plain_and_safe_to_print(value):
    name = norm_event(value)
    assert re.fullmatch(r"[a-z0-9_.]{0,48}", name)
    assert name == name.strip("_.")


@given(BODY, st.floats(0, 4e9), st.floats(0, 4e9))
def test_the_same_body_on_the_same_day_is_the_same_delivery(body, one, other):
    same_day = int(one // 86400) == int(other // 86400)
    assert (fallback_id(body, one) == fallback_id(body, other)) == same_day
