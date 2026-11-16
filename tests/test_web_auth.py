"""Pairing links and browser sessions: the door to a page that can run the agent."""

from __future__ import annotations

import sqlite3

import pytest

from personal_agent.channels.web.auth import (
    KEEP_DEAD_S,
    MAX_LIVE_INVITES,
    TOUCH_EVERY_S,
    TooManyInvites,
    WebAuth,
    clean_agent,
    digest,
)

DAY = 86400.0


@pytest.fixture
def auth(db, clock) -> WebAuth:
    return WebAuth(db, clock, session_days=30, invite_minutes=10)


def paired(auth: WebAuth, label: str = "phone", agent: str = ""):
    found = auth.redeem(auth.invite(label).code, agent=agent)
    assert found is not None
    return found


# ---- links -------------------------------------------------------------------------------------
def test_a_link_trades_for_a_session_once(auth):
    invite = auth.invite("my phone")
    first = auth.redeem(invite.code)
    assert first is not None and first.session.label == "my phone"
    assert auth.redeem(invite.code) is None  # used up


def test_a_wrong_code_gets_nothing(auth):
    auth.invite()
    assert auth.redeem("not-the-code") is None
    assert auth.redeem("") is None
    assert auth.redeem("   ") is None
    assert auth.redeem("x" * 5000) is None


def test_a_link_expires(auth, clock):
    invite = auth.invite()
    clock.advance(10 * 60 + 1)
    assert auth.redeem(invite.code) is None


def test_a_link_works_right_up_to_its_expiry(auth, clock):
    invite = auth.invite()
    clock.advance(10 * 60 - 1)
    assert auth.redeem(invite.code) is not None


def test_codes_are_long_random_and_unique(auth):
    codes = {auth.invite().code for _ in range(MAX_LIVE_INVITES)}
    assert len(codes) == MAX_LIVE_INVITES
    assert all(len(code) >= 32 for code in codes)  # 192 bits


def test_only_a_hash_of_the_code_is_stored(auth, db):
    invite = auth.invite("p")
    found = auth.redeem(invite.code)
    assert found is not None
    stored = " ".join(
        str(value)
        for table in ("web_invites", "web_sessions")
        for row in db.query(f"SELECT * FROM {table}")
        for value in tuple(row)
    )
    assert invite.code not in stored and found.token not in stored
    assert digest(invite.code) in stored and digest(found.token) in stored


def test_too_many_waiting_links_are_refused_until_one_is_used_or_expires(auth, clock):
    codes = [auth.invite().code for _ in range(MAX_LIVE_INVITES)]
    with pytest.raises(TooManyInvites):
        auth.invite()
    assert auth.redeem(codes[0]) is not None
    auth.invite()  # room again
    clock.advance(10 * 60 + 1)
    for _ in range(MAX_LIVE_INVITES):
        auth.invite()  # the old ones lapsed


def test_pending_links_are_listed_without_their_codes(auth):
    invite = auth.invite("tablet")
    [pending] = auth.pending_invites()
    assert pending.label == "tablet" and not hasattr(pending, "code")
    auth.redeem(invite.code)
    assert auth.pending_invites() == []


def test_cancelling_links_makes_them_useless(auth):
    first, second = auth.invite().code, auth.invite().code
    assert auth.cancel_invites() == 2
    assert auth.redeem(first) is None and auth.redeem(second) is None


def test_a_label_chosen_at_the_prompt_cannot_carry_control_characters(auth):
    invite = auth.invite("ph\x1b[31mone\u202e  \n x" + "y" * 200)
    assert "\x1b" not in invite.label and "\u202e" not in invite.label and len(invite.label) <= 60


# ---- sessions ----------------------------------------------------------------------------------
def test_a_session_is_found_by_its_token(auth):
    found = paired(auth)
    session = auth.lookup(found.token)
    assert session is not None and session.id == found.session.id


@pytest.mark.parametrize("token", ["", "nope", "x" * 5000])
def test_an_unknown_token_is_nobody(auth, token):
    paired(auth)
    assert auth.lookup(token) is None


def test_the_invite_code_is_not_a_session_token(auth):
    invite = auth.invite()
    auth.redeem(invite.code)
    assert auth.lookup(invite.code) is None


def test_a_session_expires_after_it_was_last_used(auth, clock):
    found = paired(auth)
    clock.advance(29 * DAY)
    assert auth.lookup(found.token) is not None  # using it pushes the expiry out
    clock.advance(29 * DAY)
    assert auth.lookup(found.token) is not None
    clock.advance(31 * DAY)
    assert auth.lookup(found.token) is None


def test_using_a_session_extends_it_but_not_on_every_request(auth, clock, db):
    found = paired(auth)
    first = auth.lookup(found.token)
    clock.advance(TOUCH_EVERY_S / 2)
    second = auth.lookup(found.token)
    assert second.expires_at == first.expires_at  # no write yet
    clock.advance(TOUCH_EVERY_S)
    third = auth.lookup(found.token)
    assert third.expires_at > first.expires_at


def test_revoking_ends_a_session_at_once(auth):
    keep, drop = paired(auth, "keep"), paired(auth, "drop")
    assert auth.revoke(drop.session.id)
    assert auth.lookup(drop.token) is None
    assert auth.lookup(keep.token) is not None
    assert not auth.revoke(drop.session.id)  # already ended
    assert not auth.revoke(9999)


def test_signing_out_revokes_only_that_browser(auth):
    one, two = paired(auth), paired(auth)
    assert auth.revoke_token(one.token)
    assert auth.lookup(one.token) is None and auth.lookup(two.token) is not None
    assert not auth.revoke_token("nope")


def test_revoke_all(auth):
    tokens = [paired(auth).token for _ in range(3)]
    assert auth.revoke_all() == 3
    assert all(auth.lookup(t) is None for t in tokens)
    assert auth.revoke_all() == 0


def test_sessions_are_listed_with_what_the_browser_called_itself(auth):
    paired(auth, "iPhone", agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) AppleWebKit Safari/605")
    [session] = auth.sessions()
    assert session.label == "iPhone" and session.agent == "Safari on iPhone"


def test_ended_sessions_are_listed_only_when_asked_for(auth):
    ended = paired(auth)
    auth.revoke(ended.session.id)
    paired(auth)
    assert len(auth.sessions()) == 1
    assert len(auth.sessions(include_ended=True)) == 2


def test_the_state_of_a_session_is_readable(auth, clock):
    found = paired(auth)
    now = clock.now()
    assert found.session.state(now) == "active"
    assert found.session.state(now + 40 * DAY) == "expired"
    auth.revoke(found.session.id)
    [session] = auth.sessions(include_ended=True)
    assert session.state(now) == "revoked"


def test_a_database_that_cannot_be_read_lets_nobody_in(auth, monkeypatch):
    found = paired(auth)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(auth.db, "one", broken)
    assert auth.lookup(found.token) is None


def test_old_dead_rows_are_forgotten(auth, clock, db):
    found = paired(auth)
    auth.revoke(found.session.id)
    clock.advance(KEEP_DEAD_S + 1)
    auth.invite()  # making a link tidies up what has been dead for a while
    assert db.scalar("SELECT COUNT(*) FROM web_sessions") == 0
    assert db.scalar("SELECT COUNT(*) FROM web_invites") == 1  # only the new one


# ---- what a browser says about itself -----------------------------------------------------------
@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit Version/18 Safari/605",
            "Safari on iPhone",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/130.0 Safari/537",
            "Chrome on Mac",
        ),
        ("Mozilla/5.0 (Macintosh) Gecko Firefox/131.0", "Firefox on Mac"),
        ("Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile Safari/537", "Chrome on Android"),
        ("Mozilla/5.0 (iPhone) CriOS/130 Safari/605", "Chrome on iPhone"),
        ("curl/8.7.1", "curl/8.7.1"),
        ("", "unknown browser"),
        ("evil\x1b[2J\u202e" + "a" * 100, ("evil[2J" + "a" * 100)[:30]),
    ],
)
def test_a_browser_is_described_briefly_and_safely(agent, expected):
    described = clean_agent(agent)
    assert described == expected
    assert "\x1b" not in described and "\u202e" not in described and len(described) <= 40
