"""scripts/pr_watch.py: the local watcher that wakes the agent when a pull request is opened.

What has to hold: a pull request is announced once (and again only for a reason), nothing that
was already there is announced on the first run, a restart neither loses nor repeats work, a line
that wakes an agent carries only fields the script checked itself, and an unreadable state file
stops the watcher instead of starting over. GitHub and the clock are stood in for; nothing here
talks to the network or sleeps.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "PR_WATCH_EVENT "
REPO = "zypgo/personal-agent"
NOW = 1_000_000.0
HOUR = 3600.0


def _load():
    spec = importlib.util.spec_from_file_location("pr_watch", ROOT / "scripts" / "pr_watch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass looks its own module up while it is built
    spec.loader.exec_module(module)
    return module


pw = _load()


def sha(n: int) -> str:
    return f"{n:040x}"


def pull(number: int = 5, **changes):
    fields = {
        "number": number,
        "open": True,
        "draft": False,
        "author": "zypgo",
        "head": sha(number),
        "head_repo": REPO,
        "base_repo": REPO,
        "base_ref": "main",
    }
    return pw.Pull(**{**fields, **changes})


def cfg(**changes):
    return pw.Settings(**{"repo": REPO, "trusted": frozenset({"zypgo"}), **changes})


def baselined(*pulls):
    """A state after a first run that found `pulls` already there."""
    state = pw.State(repo=REPO)
    assert pw.plan(state, list(pulls), NOW - 10, cfg()) == []
    return state


def announced(events):
    return [(e["event"], e["number"]) for e in events]


# ---- what to announce ------------------------------------------------------------------------
def test_the_first_run_only_notes_what_is_already_there():
    state = pw.State(repo=REPO)
    events = pw.plan(state, [pull(1), pull(2, draft=True), pull(3, open=False)], NOW, cfg())

    assert events == []
    assert state.initialized
    assert {n: t.status for n, t in state.prs.items()} == {
        1: "baseline",
        2: "baseline",
        3: "baseline",
    }


def test_a_pull_request_opened_later_is_announced_exactly_once():
    state = baselined(pull(1))

    assert announced(pw.plan(state, [pull(1), pull(2)], NOW, cfg())) == [("new_pr", 2)]
    assert pw.plan(state, [pull(1), pull(2)], NOW + 30, cfg()) == []
    assert state.prs[2].status == "pending" and state.prs[1].status == "baseline"


def test_several_new_pull_requests_are_announced_oldest_first():
    state = baselined()

    events = pw.plan(state, [pull(9), pull(4), pull(7)], NOW, cfg())

    assert announced(events) == [("new_pr", 4), ("new_pr", 7), ("new_pr", 9)]


def test_include_open_announces_what_is_open_on_the_first_run_but_no_draft_and_no_closed_one():
    state = pw.State(repo=REPO)

    events = pw.plan(
        state, [pull(1), pull(2, draft=True), pull(3, open=False)], NOW, cfg(include_open=True)
    )

    assert announced(events) == [("new_pr", 1)]
    assert (state.prs[2].status, state.prs[3].status) == ("draft", "closed")


def test_a_draft_is_announced_when_it_is_marked_ready():
    state = baselined()

    assert pw.plan(state, [pull(5, draft=True)], NOW, cfg()) == []
    assert state.prs[5].status == "draft"
    assert announced(pw.plan(state, [pull(5)], NOW + 30, cfg())) == [("new_pr", 5)]


def test_a_pull_request_opened_and_closed_between_two_polls_is_never_announced():
    state = baselined()

    assert pw.plan(state, [pull(5, open=False)], NOW, cfg()) == []
    assert state.prs[5].status == "closed"


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        ({}, []),
        ({"author": "ZYPGO"}, []),  # a login is not case sensitive
        ({"author": "mallory"}, ["author_not_trusted"]),
        ({"author": "dependabot[bot]"}, ["author_not_trusted"]),
        ({"author": "?"}, ["author_not_trusted"]),
        ({"head_repo": "mallory/personal-agent"}, ["from_fork"]),
        ({"head_repo": None}, ["from_fork"]),  # the fork it came from was deleted
        ({"base_ref": "dev"}, ["other_base_branch"]),
        (
            {"author": "mallory", "head_repo": "mallory/x", "base_ref": "dev"},
            ["author_not_trusted", "from_fork", "other_base_branch"],
        ),
    ],
)
def test_why_a_pull_request_may_not_be_merged_unasked(changes, reasons):
    assert pw.assess(pull(**changes), cfg()) == reasons


def test_the_announcement_says_whether_it_may_be_merged_unasked_and_why_not():
    state = baselined()

    [good] = pw.plan(state, [pull(5)], NOW, cfg())
    [bad] = pw.plan(state, [pull(6, author="mallory")], NOW + 30, cfg())

    assert (good["trusted"], good["reasons"]) == (True, [])
    assert (bad["trusted"], bad["reasons"]) == (False, ["author_not_trusted"])


def test_nobody_answering_brings_reminders_for_a_while_and_then_stops():
    c = cfg(remind_after_s=HOUR, max_reminders=2)
    state = baselined()
    pw.plan(state, [pull(5)], NOW, c)

    assert pw.plan(state, [pull(5)], NOW + HOUR - 1, c) == []
    [first] = pw.plan(state, [pull(5)], NOW + HOUR, c)
    assert (first["event"], first["attempt"]) == ("reminder", 2)
    assert pw.plan(state, [pull(5)], NOW + HOUR + 1, c) == []
    [second] = pw.plan(state, [pull(5)], NOW + 2 * HOUR, c)
    assert second["attempt"] == 3
    assert pw.plan(state, [pull(5)], NOW + 9 * HOUR, c) == []
    assert state.prs[5].status == "pending"  # given up reminding, not given up on


def test_a_claimed_pull_request_is_left_alone_until_the_claim_runs_out():
    c = cfg(remind_after_s=HOUR)
    state = baselined()
    pw.plan(state, [pull(5)], NOW, c)
    assert pw.claim(state, 5, NOW + 60, 2 * HOUR) == "ok"

    assert pw.plan(state, [pull(5)], NOW + HOUR + 60, c) == []
    [event] = pw.plan(state, [pull(5)], NOW + 2 * HOUR + 120, c)
    assert event["event"] == "reminder"


def test_who_may_claim_what():
    state = baselined(pull(1))
    pw.plan(state, [pull(1), pull(5)], NOW, cfg())

    assert pw.claim(state, 5, NOW, 600) == "ok"
    assert pw.claim(state, 5, NOW + 1, 600) == "claimed"
    assert pw.claim(state, 5, NOW + 1, 600, renew=True) == "ok"
    assert pw.claim(state, 5, NOW + 700, 600) == "ok"  # the last claim ran out
    assert pw.claim(state, 1, NOW, 600) == "not_pending"  # it was there before the first run
    assert pw.claim(state, 99, NOW, 600) == "not_pending"


def test_final_answers_end_the_matter_and_the_others_set_it_aside_until_a_new_commit():
    c = cfg()
    state = baselined()
    pw.plan(state, [pull(5), pull(6)], NOW, c)

    assert pw.ack(state, 5, "integrated", "landed as abc", NOW + 1) == "ok"
    assert pw.ack(state, 5, "integrated", "", NOW + 2) == "not_pending"  # answered already
    assert pw.ack(state, 6, "needs_changes", "tests are missing", NOW + 1) == "ok"
    assert (state.prs[5].status, state.prs[6].status) == ("done", "parked")
    assert pw.plan(state, [pull(5), pull(6)], NOW + 10, c) == []  # the same commits: silence

    events = pw.plan(state, [pull(5, head=sha(500)), pull(6, head=sha(600))], NOW + 20, c)

    assert announced(events) == [("pr_updated", 6)]
    assert (state.prs[5].status, state.prs[6].status) == ("done", "pending")


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ("integrated", "done"),
        ("declined", "done"),
        ("needs_user", "parked"),
        ("needs_changes", "parked"),
    ],
)
def test_what_an_answer_does_to_the_pull_request(result, status):
    state = baselined()
    pw.plan(state, [pull(5)], NOW, cfg())
    pw.claim(state, 5, NOW + 1, 600)

    assert pw.ack(state, 5, result, "", NOW + 2) == "ok"

    tracked = state.prs[5]
    assert (tracked.status, tracked.result, tracked.claimed_until) == (status, result, None)


def test_a_pull_request_that_is_set_aside_and_then_closed_is_closed():
    state = baselined()
    pw.plan(state, [pull(5)], NOW, cfg())
    pw.ack(state, 5, "needs_changes", "", NOW + 1)

    assert pw.plan(state, [pull(5, open=False)], NOW + 30, cfg()) == []
    assert state.prs[5].status == "closed"


def test_a_pull_request_set_aside_can_still_be_answered_once_the_user_has_decided():
    state = baselined()
    pw.plan(state, [pull(5)], NOW, cfg())
    pw.ack(state, 5, "needs_user", "fork, ask first", NOW + 1)

    assert pw.ack(state, 5, "integrated", "user said go", NOW + 2) == "ok"
    assert state.prs[5].status == "done"


def test_an_answer_naming_a_commit_is_refused_when_the_pull_request_has_moved_on():
    state = baselined()
    pw.plan(state, [pull(5)], NOW, cfg())
    pw.plan(state, [pull(5, head=sha(55))], NOW + 5, cfg())  # a push while it was being looked at

    assert pw.ack(state, 5, "integrated", "", NOW + 6, head=sha(5)) == "head_changed"
    assert state.prs[5].status == "pending"
    assert pw.ack(state, 5, "integrated", "", NOW + 7, head=sha(55)) == "ok"


def test_a_note_is_kept_as_one_short_line():
    state = baselined()
    pw.plan(state, [pull(5), pull(6)], NOW, cfg())

    pw.ack(state, 5, "declined", "one\n\ntwo\t" + "x" * 500, NOW)
    pw.ack(state, 6, "declined", "  \n ", NOW)

    assert state.prs[5].note.startswith("one two xxx") and len(state.prs[5].note) == 300
    assert state.prs[6].note is None


def test_closing_a_waiting_pull_request_ends_the_matter_and_reopening_it_brings_it_back():
    state = baselined()
    pw.plan(state, [pull(5)], NOW, cfg())
    pw.claim(state, 5, NOW + 1, 600)

    assert pw.plan(state, [pull(5, open=False)], NOW + 10, cfg()) == []
    assert (state.prs[5].status, state.prs[5].claimed_until) == ("closed", None)
    assert pw.claim(state, 5, NOW + 11, 600) == "not_pending"
    assert announced(pw.plan(state, [pull(5)], NOW + 40, cfg())) == [("pr_updated", 5)]


def test_a_draft_that_is_closed_ends_the_matter_too():
    state = baselined()
    pw.plan(state, [pull(5, draft=True)], NOW, cfg())

    assert pw.plan(state, [pull(5, draft=True, open=False)], NOW + 30, cfg()) == []
    assert state.prs[5].status == "closed"


def test_a_pull_request_that_was_answered_for_good_is_never_announced_again():
    state = baselined()
    pw.plan(state, [pull(5)], NOW, cfg())
    pw.ack(state, 5, "integrated", "", NOW + 1)

    for later in (pull(5, head=sha(8)), pull(5, open=False), pull(5)):
        assert pw.plan(state, [later], NOW + 99 * HOUR, cfg()) == []
    assert state.prs[5].status == "done"


def test_after_a_restart_what_still_waits_is_announced_again_unless_somebody_is_on_it():
    state = baselined()
    pw.plan(state, [pull(5), pull(6), pull(7), pull(8)], NOW, cfg())
    pw.claim(state, 6, NOW + 1, 600)
    pw.ack(state, 7, "declined", "", NOW + 2)
    pw.ack(state, 8, "needs_user", "", NOW + 2)

    events = pw.resume(state, NOW + 100, cfg())

    assert announced(events) == [("resume", 5)]
    assert state.prs[5].notified_at == NOW + 100  # so no reminder follows at once


def test_an_announcement_is_one_ascii_line_made_only_of_fields_that_were_checked():
    state = baselined()
    [event] = pw.plan(state, [pull(5)], NOW, cfg())

    line = pw.format_event(event)

    assert line.startswith(SENTINEL + "{") and "\n" not in line and line.isascii()
    payload = json.loads(line.removeprefix(SENTINEL))
    assert set(payload) == {
        "event", "repo", "number", "head", "author", "trusted", "reasons", "attempt", "url",
        "playbook",
    }  # fmt: skip
    assert payload["url"] == f"https://github.com/{REPO}/pull/5"
    assert payload["playbook"] == "docs/PR_PLAYBOOK.md"


def test_whatever_is_put_in_an_event_it_stays_one_ascii_line():
    line = pw.format_event({"event": "x", "text": "two\nlines \u2028 and \u00e9 and \x1b[31m"})

    assert "\n" not in line and "\r" not in line and line.isascii()
    assert json.loads(line.removeprefix(SENTINEL))["text"].startswith("two\nlines")


# ---- reading what GitHub says ----------------------------------------------------------------
GOOD = {
    "number": 7,
    "state": "open",
    "draft": False,
    "author": "zypgo",
    "head_sha": sha(7),
    "head_repo": REPO,
    "base_repo": REPO,
    "base_ref": "main",
}


def test_a_good_entry_is_read():
    assert pw.parse_pull(GOOD) == pull(7)
    assert pw.parse_pull({**GOOD, "state": "closed", "draft": True}) == pull(
        7, open=False, draft=True
    )
    assert pw.parse_pull({**GOOD, "head_repo": None}) == pull(7, head_repo=None)


@pytest.mark.parametrize(
    "changes",
    [
        {"number": 0},
        {"number": True},
        {"number": "7"},
        {"head_sha": "abc"},
        {"head_sha": "A" * 40},  # GitHub writes ids in lower case
        {"head_sha": None},
        {"base_repo": "no-slash"},
        {"base_repo": None},
        {"base_ref": ""},
        {"base_ref": None},
    ],
)
def test_an_entry_that_does_not_look_like_a_pull_request_is_refused(changes):
    assert pw.parse_pull({**GOOD, **changes}) is None


@pytest.mark.parametrize("entry", [None, 7, "text", [GOOD]])
def test_something_that_is_not_an_object_is_refused(entry):
    assert pw.parse_pull(entry) is None


@pytest.mark.parametrize("login", ["ev il", "x" * 50, "a\nb", "", None, 5, "-leading", "a;b"])
def test_an_author_that_is_not_a_login_becomes_a_question_mark_which_nobody_can_trust(login):
    parsed = pw.parse_pull({**GOOD, "author": login})

    assert parsed.author == "?"  # and `--trusted` only takes real logins: see below
    assert "author_not_trusted" in pw.assess(parsed, cfg())


def test_a_bot_login_is_kept_as_it_is():
    assert pw.parse_pull({**GOOD, "author": "dependabot[bot]"}).author == "dependabot[bot]"


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("gh: API rate limit exceeded for user ID 1. (HTTP 403)", "rate_limit"),
        ("You have exceeded a secondary rate limit", "rate_limit"),
        ("gh: Bad credentials (HTTP 401)", "auth"),
        ("To get started with GitHub CLI, please run:  gh auth login", "auth"),
        ("gh: Not Found (HTTP 404)", "not_found"),
        ("error connecting to api.github.com", "network"),
        ("could not resolve host: api.github.com", "network"),
        ("dial tcp: lookup api.github.com: no such host", "network"),
        ("something nobody has seen before", "error"),
        ("", "error"),
    ],
)
def test_what_went_wrong_is_reduced_to_a_fixed_word(stderr, kind):
    assert pw.classify(stderr) == kind


def completed(stdout: str = "", stderr: str = "", code: int = 0):
    return subprocess.CompletedProcess(["gh"], code, stdout=stdout, stderr=stderr)


def as_json(p) -> dict:
    return {
        "number": p.number,
        "state": "open" if p.open else "closed",
        "draft": p.draft,
        "author": p.author,
        "head_sha": p.head,
        "head_repo": p.head_repo,
        "base_repo": p.base_repo,
        "base_ref": p.base_ref,
    }


def listing(*pulls):
    return completed(json.dumps([as_json(p) for p in pulls]))


class FakeGh:
    """Answers one call per entry of `answers` (an exception is raised, anything else returned)."""

    def __init__(self, *answers, on_last=None):
        self.answers, self.calls, self.on_last = list(answers), [], on_last

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        answer = self.answers.pop(0)
        if not self.answers and self.on_last:
            self.on_last()
        if isinstance(answer, BaseException):
            raise answer
        return answer


def test_one_page_of_pull_requests_is_asked_for_with_a_fixed_command():
    gh = FakeGh(listing(pull(7)))

    assert pw.fetch_pulls(REPO, gh) == [pull(7)]

    [(command, kwargs)] = gh.calls
    assert command[:5] == ["gh", "api", "--method", "GET", f"repos/{REPO}/pulls"]
    assert {"per_page=100", "state=all", "sort=created"} <= set(command)
    assert kwargs["check"] is False and kwargs["timeout"] > 0
    assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"


def test_an_empty_answer_is_a_good_answer_and_an_odd_entry_is_skipped():
    assert pw.fetch_pulls(REPO, FakeGh(completed("[]"))) == []
    mixed = completed(json.dumps([as_json(pull(7)), {"number": "x"}]))
    assert pw.fetch_pulls(REPO, FakeGh(mixed)) == [pull(7)]


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (completed(stderr="gh: Not Found (HTTP 404)", code=1), "not_found"),
        (completed(stderr="API rate limit exceeded", code=1), "rate_limit"),
        (completed(stdout="<html>"), "bad_response"),
        (completed(stdout='{"message": "x"}'), "bad_response"),
        (completed(stdout=json.dumps([{"number": "x"}, 3])), "bad_response"),  # all unreadable
        (subprocess.TimeoutExpired("gh", 45), "network"),
        (FileNotFoundError("gh"), "gh_missing"),
    ],
)
def test_a_failed_poll_is_named_by_a_fixed_word(answer, kind):
    with pytest.raises(pw.FetchError) as caught:
        pw.fetch_pulls(REPO, FakeGh(answer))

    assert caught.value.kind == kind


# ---- what is remembered ----------------------------------------------------------------------
def test_the_state_survives_a_restart_and_is_private(tmp_path):
    store = pw.Store(tmp_path / "state", REPO)
    with store.transaction() as state:
        pw.plan(state, [pull(1)], NOW, cfg())
        state.last_ok = NOW

    again = pw.Store(tmp_path / "state", REPO).load()

    assert again.initialized and again.prs[1].status == "baseline" and again.last_ok == NOW
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in store.path.parent.iterdir()) == [
        "zypgo__personal-agent.json",
        "zypgo__personal-agent.lock",
    ]  # no half-written file is left behind


def test_a_change_that_fails_halfway_is_not_written(tmp_path):
    store = pw.Store(tmp_path, REPO)
    with store.transaction() as state:
        state.last_error = "kept"

    with pytest.raises(RuntimeError), store.transaction() as state:
        state.last_error = "lost"
        raise RuntimeError

    assert store.load().last_error == "kept"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[1]",
        json.dumps({"repo": "someone/else", "prs": {}}),
        json.dumps({"repo": REPO, "prs": {"x": {}}}),
        json.dumps({"repo": REPO, "prs": {"5": {"status": "pending"}}}),  # fields missing
        json.dumps({"repo": REPO, "prs": [1]}),
    ],
)
def test_a_state_file_that_cannot_be_read_stops_everything_instead_of_starting_over(
    tmp_path, content
):
    store = pw.Store(tmp_path, REPO)
    store.path.write_text(content)

    with pytest.raises(pw.StateError):
        store.load()
    with pytest.raises(pw.StateError), store.transaction():
        pytest.fail("the caller must not get a made-up state")
    assert store.path.read_text() == content  # and what was there is left as it was


def test_a_field_this_version_does_not_know_is_ignored(tmp_path):
    entry = {"status": "pending", "head": sha(5), "author": "zypgo", "seen_at": NOW, "future": 1}
    store = pw.Store(tmp_path, REPO)
    store.path.write_text(json.dumps({"repo": REPO, "initialized": True, "prs": {"5": entry}}))

    assert store.load().prs[5].status == "pending"


def test_only_one_watcher_runs_per_repository(tmp_path):
    store = pw.Store(tmp_path, REPO)
    assert store.watcher_pid() is None

    with store.single_watcher():
        assert store.watcher_pid() == os.getpid()
        with (
            pytest.raises(pw.StateError, match="already running"),
            pw.Store(tmp_path, REPO).single_watcher(),
        ):
            pytest.fail("a second watcher must not start")

    assert store.watcher_pid() is None
    with store.single_watcher():  # and the lock is free again
        pass


# ---- the loop ---------------------------------------------------------------------------------
class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def drive(tmp_path, answers, c=None, *, once=False):
    """Run the loop over `answers` (one per poll) on a clock that moves only when it waits."""
    store = pw.Store(tmp_path / "state", REPO)
    stop, clock, delays = threading.Event(), Clock(), []
    gh = FakeGh(*answers, on_last=stop.set)

    def wait(delay):
        delays.append(delay)
        clock.now += delay

    out, err = io.StringIO(), io.StringIO()
    code = pw.watch(
        c or cfg(), store, run=gh, out=out, err=err, clock=clock, stop=stop, wait=wait, once=once
    )
    events = [json.loads(line.removeprefix(SENTINEL)) for line in out.getvalue().splitlines()]
    return SimpleNamespace(code=code, events=events, delays=delays, err=err.getvalue(), store=store)


def test_the_loop_notes_what_is_there_and_announces_what_comes_once(tmp_path):
    answers = [listing(pull(1)), listing(pull(1), pull(2)), listing(pull(1), pull(2))]

    run = drive(tmp_path, answers)

    assert run.code == 0
    assert announced(run.events) == [("new_pr", 2)]
    assert run.delays == [30.0, 30.0, 30.0]
    # everything else it says goes to standard error and starts with a word no monitor waits for
    assert run.err.splitlines() and all(
        line.startswith("pr-watch: ") for line in run.err.splitlines()
    )
    state = run.store.load()
    assert (state.prs[1].status, state.prs[2].status) == ("baseline", "pending")
    assert state.last_error is None and state.last_ok is not None


def test_a_restart_repeats_what_is_still_waiting_and_announces_what_came_meanwhile(tmp_path):
    first = drive(tmp_path, [listing(), listing(pull(2))])
    assert announced(first.events) == [("new_pr", 2)]

    again = drive(tmp_path, [listing(pull(2)), listing(pull(2), pull(3))])

    assert announced(again.events) == [("resume", 2), ("new_pr", 3)]


def test_what_still_waits_is_repeated_only_after_the_first_good_poll(tmp_path):
    drive(tmp_path, [listing(), listing(pull(2))])
    down = completed(stderr="error connecting to api.github.com", code=1)

    again = drive(tmp_path, [down, listing(pull(2))])

    assert announced(again.events) == [("resume", 2)]


def test_failed_polls_back_off_and_the_listener_is_told_once_that_it_goes_on(tmp_path):
    down = completed(stderr="error connecting to api.github.com", code=1)

    run = drive(tmp_path, [down] * 7 + [listing(pull(1))])

    assert run.delays == [30.0, 60.0, 120.0, 240.0, 480.0, 900.0, 900.0, 30.0]
    [unhealthy] = run.events
    assert (unhealthy["event"], unhealthy["failures"], unhealthy["kind"]) == (
        "watcher_unhealthy", 5, "network",
    )  # fmt: skip
    assert "polling works again" in run.err
    assert run.store.load().last_error is None


def test_a_rate_limit_is_waited_out_for_at_least_five_minutes(tmp_path):
    limited = completed(stderr="API rate limit exceeded", code=1)

    run = drive(tmp_path, [limited])

    assert run.delays == [300.0]
    assert run.store.load().last_error == "rate_limit"


def test_polling_once_says_by_its_exit_code_whether_it_worked(tmp_path):
    good = drive(tmp_path / "good", [listing(pull(1))], once=True)
    bad = drive(tmp_path / "bad", [completed(stderr="boom", code=1)], once=True)

    assert (good.code, good.delays) == (0, [])
    assert (bad.code, bad.delays) == (1, [])


# ---- the command line ------------------------------------------------------------------------
def cli(tmp_path, *argv, answers=(), clock=None, repo=REPO):
    out, err = io.StringIO(), io.StringIO()
    gh = FakeGh(*answers)
    args = ["--repo", repo, "--state-dir", str(tmp_path / "state"), *argv]
    code = pw.main(args, run=gh, out=out, err=err, clock=clock or Clock())
    return code, out.getvalue(), err.getvalue()


def lines(output: str):
    return [json.loads(line.removeprefix(SENTINEL)) for line in output.splitlines()]


def test_from_a_new_pull_request_to_its_answer(tmp_path):
    clock = Clock()
    assert cli(tmp_path, "watch", "--once", answers=[listing(pull(1))], clock=clock)[:2] == (0, "")
    clock.now += 60
    code, out, _ = cli(
        tmp_path, "watch", "--once", answers=[listing(pull(1), pull(2))], clock=clock
    )
    [event] = lines(out)
    assert code == 0 and (event["event"], event["number"], event["head"]) == ("new_pr", 2, sha(2))

    code, status, _ = cli(tmp_path, "status", clock=clock)
    header, _columns, *rows = status.splitlines()
    assert code == 0 and "watcher not running" in header
    assert [r.split()[:2] for r in rows] == [["1", "baseline"], ["2", "pending"]]

    code, out, _ = cli(tmp_path, "claim", "2", clock=clock)
    assert (code, out.strip()) == (0, f"claimed #2 at {sha(2)} for 120 minutes")
    assert cli(tmp_path, "claim", "2", clock=clock)[0] == 4  # somebody is on it already
    assert cli(tmp_path, "claim", "1", clock=clock)[0] == 5  # nothing was asked about it

    code, _, err = cli(
        tmp_path, "ack", "2", "--result", "integrated", "--head", sha(99), clock=clock
    )
    assert code == 6 and sha(2) in err  # the commit that was looked at is not the newest
    assert (
        cli(tmp_path, "ack", "2", "--result", "integrated", "--head", sha(2), clock=clock)[0] == 0
    )
    assert cli(tmp_path, "ack", "2", "--result", "integrated", clock=clock)[0] == 5  # only once

    shown = json.loads(cli(tmp_path, "status", "--json", clock=clock)[1])
    assert shown["prs"]["2"]["status"] == "done" and shown["prs"]["2"]["result"] == "integrated"


def test_the_owner_is_trusted_by_default_and_who_else_is_trusted_can_be_said(tmp_path):
    answers = [listing(pull(1, author="alice"), pull(2, author="zypgo"))]

    _, out, _ = cli(tmp_path / "a", "watch", "--once", "--include-open", answers=answers)
    by_number = {e["number"]: e["trusted"] for e in lines(out)}
    assert by_number == {1: False, 2: True}

    answers = [listing(pull(1, author="alice"), pull(2, author="zypgo"))]
    argv = ("watch", "--once", "--include-open", "--trusted", "Alice")
    _, out, _ = cli(tmp_path / "b", *argv, answers=answers)
    by_number = {e["number"]: e["trusted"] for e in lines(out)}
    assert by_number == {1: True, 2: False}


@pytest.mark.parametrize("login", ["?", "a b", "x;y", ""])
def test_who_may_be_trusted_must_be_a_github_login(tmp_path, login):
    code, out, err = cli(tmp_path, "watch", "--once", "--trusted", login)

    assert (code, out) == (2, "") and "--trusted" in err


def test_a_commit_that_is_not_one_is_refused_before_anything_is_touched(tmp_path):
    with pytest.raises(SystemExit) as caught:
        cli(tmp_path, "ack", "2", "--result", "declined", "--head", "main")

    assert caught.value.code == 2


def test_a_self_test_prints_one_event_that_wakes_the_listener(tmp_path):
    code, out, _ = cli(tmp_path, "selftest")

    [event] = lines(out)
    assert code == 0 and event["event"] == "selftest" and out.startswith(SENTINEL)


@pytest.mark.parametrize(
    ("remote", "repo"),
    [
        ("https://github.com/zypgo/personal-agent.git\n", "zypgo/personal-agent"),
        ("https://github.com/zypgo/personal-agent\n", "zypgo/personal-agent"),
        ("https://github.com/zypgo/personal-agent/\n", "zypgo/personal-agent"),
        ("git@github.com:zypgo/personal-agent.git\n", "zypgo/personal-agent"),
        ("https://example.com/zypgo/personal-agent.git\n", None),
    ],
)
def test_the_repository_comes_from_the_github_remote(remote, repo):
    assert pw.default_repo(FakeGh(completed(remote))) == repo


def test_without_a_repository_it_is_a_usage_error(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    no_remote = FakeGh(completed(code=1), completed(code=1))

    argv = ["--state-dir", str(tmp_path), "status"]
    assert pw.main(argv, run=no_remote, out=out, err=err) == 2
    assert pw.main(["--repo", "not a repo", *argv], run=no_remote, out=out, err=err) == 2
    assert "--repo" in err.getvalue()


def test_a_damaged_state_file_ends_in_an_error_not_in_a_fresh_start(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "zypgo__personal-agent.json").write_text("{broken")

    for argv in (("watch", "--once"), ("status",), ("claim", "2")):
        code, out, err = cli(tmp_path, *argv, answers=[listing(pull(1))])
        assert code == 1 and out == "" and "damaged" in err


def test_a_second_watcher_for_the_same_repository_is_refused(tmp_path):
    with pw.Store(tmp_path / "state", REPO).single_watcher():
        code, out, err = cli(tmp_path, "watch", "--once", answers=[listing()])

    assert (code, out) == (1, "") and "already running" in err


def test_a_termination_signal_ends_the_watcher_cleanly_after_the_poll_in_progress(tmp_path):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers belong to the main thread")
    seen = []
    before = signal.signal(signal.SIGTERM, lambda *_: seen.append("not ours"))  # a safety net
    try:
        out, err = io.StringIO(), io.StringIO()
        gh = FakeGh(listing(pull(1)), on_last=lambda: os.kill(os.getpid(), signal.SIGTERM))

        code = pw.main(
            ["--repo", REPO, "--state-dir", str(tmp_path), "watch"],
            run=gh, out=out, err=err, clock=Clock(),
        )  # fmt: skip

        assert (code, seen) == (0, [])  # our handler took it, then ours was put back
        assert "stopped" in err.getvalue()
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        assert pw.Store(tmp_path, REPO).load().initialized  # the poll in progress was finished
    finally:
        signal.signal(signal.SIGTERM, before)


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [(-5, "0s"), (5, "5s"), (89, "89s"), (90, "1m"), (5399, "89m"), (5400, "1h"), (7 * 3600, "7h")],
)
def test_ages_are_shown_in_the_largest_unit_that_keeps_them_short(seconds, shown):
    assert pw.ago(seconds) == shown
