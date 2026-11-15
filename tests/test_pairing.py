from __future__ import annotations

import sqlite3

from personal_agent.channels.pairing import (
    ALPHABET,
    CODE_LENGTH,
    MAX_PENDING,
    PAIRING_TTL_S,
    PairingStore,
    new_code,
    normalize_code,
)


def store(db, clock, **kw) -> PairingStore:
    return PairingStore(db, clock, **kw)


# ---- codes -----------------------------------------------------------------------------------
def test_codes_are_eight_unambiguous_characters_in_two_groups():
    for _ in range(200):
        code = new_code()
        head, _, tail = code.partition("-")
        assert len(head) == len(tail) == CODE_LENGTH // 2
        assert set(head + tail) <= set(ALPHABET)
    assert not set("0O1IL") & set(ALPHABET)  # read off a phone screen, typed on a computer


def test_codes_are_not_predictable():
    assert len({new_code() for _ in range(200)}) == 200


def test_any_way_of_typing_a_code_is_the_same_code():
    assert normalize_code("abcd-efgh") == normalize_code("ABCD EFGH") == "ABCD-EFGH"
    assert normalize_code("abcdefgh") == "ABCD-EFGH"
    assert normalize_code("abc") == "ABC"  # too short stays recognisably wrong


# ---- a stranger asks -------------------------------------------------------------------------
def test_a_stranger_gets_a_code_and_the_same_one_every_time(db, clock):
    pairing = store(db, clock)
    first = pairing.request("telegram", "42", "Eve")
    again = pairing.request("telegram", "42", "Eve")
    assert first is not None and again is not None
    assert first.code == again.code
    assert first.expires_at == clock.now() + PAIRING_TTL_S
    assert not pairing.is_approved("telegram", "42")


def test_the_code_is_only_marked_told_when_the_caller_says_so(db, clock):
    pairing = store(db, clock)
    request = pairing.request("telegram", "42")
    assert request.notified_at is None
    pairing.mark_notified(request.code)
    assert pairing.request("telegram", "42").notified_at == clock.now()


def test_only_a_few_requests_wait_per_channel_and_more_are_ignored(db, clock):
    pairing = store(db, clock)
    issued = [pairing.request("telegram", str(n)) for n in range(MAX_PENDING)]
    assert all(issued)
    assert pairing.request("telegram", "999") is None  # ignored, not given a code
    assert pairing.request("telegram", "0").code == issued[0].code  # existing ones still answer
    assert pairing.request("other", "999") is not None  # the cap is per channel
    assert len(pairing.pending("telegram")) == MAX_PENDING


def test_a_slot_frees_up_when_a_code_expires(db, clock):
    pairing = store(db, clock)
    for n in range(MAX_PENDING):
        pairing.request("telegram", str(n))
    clock.advance(PAIRING_TTL_S + 1)
    assert pairing.pending("telegram") == []
    assert pairing.request("telegram", "999") is not None


def test_an_expired_code_cannot_be_approved(db, clock):
    pairing = store(db, clock)
    request = pairing.request("telegram", "42")
    clock.advance(PAIRING_TTL_S + 1)
    assert pairing.approve(request.code) is None
    assert pairing.peek(request.code) is None
    assert not pairing.is_approved("telegram", "42")


def test_a_stranger_cannot_choose_what_the_owner_reads_in_the_terminal(db, clock):
    hostile = "Mallory\x1b[2J\u202e\u2066 \x00admin\n\n" + "x" * 500
    request = store(db, clock).request("telegram", "42", hostile)
    assert "\x1b" not in request.label and "\u202e" not in request.label
    assert "\n" not in request.label and "\x00" not in request.label
    assert len(request.label) <= 60


# ---- the owner decides -----------------------------------------------------------------------
def test_approving_a_code_lets_that_sender_in_and_uses_the_code_up(db, clock):
    pairing = store(db, clock)
    request = pairing.request("telegram", "42", "Eve")
    sender = pairing.approve(request.code.lower())  # typed in lower case
    assert (sender.channel, sender.sender, sender.label) == ("telegram", "42", "Eve")
    assert pairing.is_approved("telegram", "42")
    assert not pairing.is_approved("telegram", "43")
    assert not pairing.is_approved("web", "42")  # per channel
    assert pairing.approve(request.code) is None  # one use
    assert pairing.pending() == []


def test_approving_an_unknown_code_changes_nothing(db, clock):
    pairing = store(db, clock)
    pairing.request("telegram", "42")
    assert pairing.approve("ZZZZ-ZZZZ") is None
    assert pairing.senders() == []


def test_denying_a_code_throws_it_away_and_the_sender_stays_out(db, clock):
    pairing = store(db, clock)
    request = pairing.request("telegram", "42")
    assert pairing.deny(request.code).sender == "42"
    assert pairing.deny(request.code) is None
    assert pairing.approve(request.code) is None
    assert not pairing.is_approved("telegram", "42")


def test_allow_approves_by_id_and_clears_a_waiting_request(db, clock):
    pairing = store(db, clock)
    pairing.request("telegram", "42")
    pairing.allow("telegram", 42, "me")
    assert pairing.is_approved("telegram", "42")
    assert pairing.pending() == []
    assert [s.sender for s in pairing.senders("telegram")] == ["42"]


def test_revoking_removes_access_and_any_waiting_request(db, clock):
    pairing = store(db, clock)
    pairing.allow("telegram", "42")
    assert pairing.revoke("telegram", "42") is True
    assert not pairing.is_approved("telegram", "42")
    assert pairing.revoke("telegram", "42") is False


def test_the_most_recently_heard_sender_is_where_unprompted_messages_go(db, clock):
    pairing = store(db, clock)
    pairing.allow("telegram", "1")
    clock.advance(10)
    pairing.allow("telegram", "2")
    assert pairing.recent_sender("telegram").sender == "2"
    clock.advance(10)
    pairing.touch("telegram", "1")
    assert pairing.recent_sender("telegram").sender == "1"
    assert pairing.recent_sender("web") is None


def test_the_store_fails_closed_when_it_cannot_be_read(db, clock):
    pairing = store(db, clock)
    pairing.allow("telegram", "42")

    class Broken:
        def one(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    pairing.db = Broken()
    assert pairing.is_approved("telegram", "42") is False
