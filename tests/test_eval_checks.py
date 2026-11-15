"""Grading: each check judged on a hand-made trial, so a pass and a fail are both seen."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.core.outbox import OutMessage
from personal_agent.core.runs import RunRecord
from personal_agent.evals import checks
from personal_agent.evals.bench import StepResult, Trial
from personal_agent.evals.checks import Verdict, describe, judge
from personal_agent.evals.spec import Scenario


def out(text: str, urgency: str = "normal") -> OutMessage:
    return OutMessage(
        id=1,
        session_key="main",
        channel="cli",
        recipient=None,
        kind="message",
        payload={"text": text},
        urgency=urgency,
        status="sent",
        on_sent=[],
        run_id=None,
        attempts=1,
        created_at=0.0,
        not_before=0.0,
    )


def run(status: str = "completed", profile: str = "full") -> RunRecord:
    return RunRecord(
        id="r1",
        session_key="main",
        trigger="message",
        profile=profile,
        status=status,
        model="m",
        event_ids=[1],
        started_at=0.0,
        finished_at=1.0,
        requests=1,
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        cost_usd=0.0,
        tool_calls=0,
        tainted=False,
        final_text=None,
        error=None,
    )


def result(
    step: str = "one",
    *messages: OutMessage | str,
    runs: list[RunRecord] | None = None,
    tools: list[str] | None = None,
    failures: list[str] | None = None,
) -> StepResult:
    sent = [m if isinstance(m, OutMessage) else out(m) for m in messages]
    return StepResult(
        step,
        sent,
        [run()] if runs is None else runs,
        tools or [],
        failures or [],
    )


def scenario(*check: dict, seed: dict | None = None, ends: list[str] | None = None) -> Scenario:
    first = {"id": "one", "message": "go"}
    if ends:
        first["ends"] = ends
    return Scenario.model_validate(
        {
            "id": "demo",
            "title": "Demo",
            "seed": seed or {},
            "steps": [first, {"id": "two", "message": "again"}],
            "checks": list(check),
        }
    )


def trial(
    sc: Scenario,
    one: StepResult | None = None,
    two: StepResult | None = None,
    *,
    home: Path | None = None,
    cost: float = 0.0,
) -> Trial:
    return Trial(
        sc,
        [one or result("one"), two or result("two")],
        home or Path("/nonexistent"),
        cost_usd=cost,
    )


def verdicts(sc: Scenario, t: Trial) -> dict[str, Verdict]:
    return {v.id: v for v in judge(t)}


def only(*check: dict, **kw) -> Verdict:
    """The verdict of the first check of a scenario that has the given checks."""
    t_kw = {k: kw.pop(k) for k in ("one", "two", "home", "cost") if k in kw}
    sc = scenario(*check, **kw)
    found = judge(trial(sc, **t_kw))
    return found[len(sc.steps)]  # after the implicit `ends-*` verdicts


# ---- the implicit check of every step -----------------------------------------------------------------
def test_each_step_must_end_as_it_is_allowed_to_and_is_checked_before_everything_else():
    sc = scenario({"kind": "max_messages", "limit": 9})
    found = judge(trial(sc))
    assert [v.id for v in found] == ["ends-one", "ends-two", "max_messages"]
    assert found[0] == Verdict("ends-one", True, "ended as completed")


def test_a_run_that_fails_is_not_a_quiet_one():
    sc = scenario({"kind": "silent", "step": "one"})
    t = trial(sc, result("one", runs=[run("failed")], failures=["the model refused"]))
    got = verdicts(sc, t)
    assert not got["ends-one"].ok
    assert got["ends-one"].detail == (
        "ended as 'failed'; allowed: completed, silent (the model refused)"
    )
    assert got["silent-one"].ok  # silent is true, and that is exactly why `ends` exists


def test_a_step_where_no_run_happened_fails_and_says_why():
    sc = scenario({"kind": "max_messages", "limit": 9})
    assert verdicts(sc, trial(sc, result("one", runs=[])))["ends-one"] == Verdict(
        "ends-one", False, "no run happened"
    )
    reason = verdicts(sc, trial(sc, result("one", runs=[], failures=["no key"])))["ends-one"]
    assert reason.detail == "no run happened (no key)"


def test_a_step_may_declare_other_endings_as_fine():
    sc = scenario({"kind": "max_messages", "limit": 9}, ends=["paused_budget"])
    assert verdicts(sc, trial(sc, result("one", runs=[run("paused_budget")])))["ends-one"].ok
    assert not verdicts(sc, trial(sc, result("one", runs=[run("completed")])))["ends-one"].ok


def test_one_bad_run_among_good_ones_fails_the_step():
    sc = scenario({"kind": "max_messages", "limit": 9})
    mixed = result("one", runs=[run("completed"), run("cancelled"), run("silent")])
    got = verdicts(sc, trial(sc, mixed))["ends-one"]
    assert not got.ok and "ended as 'cancelled'" in got.detail


def test_a_step_that_ended_in_several_runs_lists_the_endings_once_each_sorted():
    sc = scenario({"kind": "max_messages", "limit": 9})
    several = result("one", runs=[run("silent"), run("completed"), run("silent")])
    assert verdicts(sc, trial(sc, several))["ends-one"].detail == "ended as completed, silent"


# ---- silent / says / never_says -----------------------------------------------------------------------
def test_silent_passes_without_messages_and_quotes_the_first_one_when_it_fails():
    assert only({"kind": "silent", "step": "one"}) == Verdict("silent-one", True, "said nothing")
    spoke = only(
        {"kind": "silent", "step": "one"},
        one=result("one", "first  thing", "second"),
    )
    assert not spoke.ok
    assert spoke.detail == "said 2 message(s), the first: 'first thing'"


def test_only_the_named_step_counts():
    sc = {"kind": "silent", "step": "one"}
    assert only(sc, two=result("two", "chatty")).ok


def test_says_needs_a_message_and_matches_any_all_or_a_pattern():
    assert only({"kind": "says", "any_of": ["x"]}) == Verdict("says", False, "said nothing")
    assert only({"kind": "says", "any_of": ["rain", "snow"]}, one=result("one", "It will SNOW")).ok
    assert only({"kind": "says", "all_of": ["rain", "wind"]}, one=result("one", "Rain, wind")).ok
    assert only({"kind": "says", "pattern": r"\d{3}"}, one=result("one", "code 123")).ok


def test_says_names_everything_it_did_not_find():
    miss = only(
        {"kind": "says", "any_of": ["rain", "snow"], "all_of": ["wind", "hail"], "pattern": "zz+"},
        one=result("one", "A gentle wind"),
    )
    assert not miss.ok
    assert miss.detail == (
        "missing none of 'rain', 'snow'; 'hail'; the pattern 'zz+' in 'A gentle wind'"
    )


def test_says_ignores_case_width_and_accents_of_form_but_not_of_letters():
    fullwidth_hello = "\uff28\uff25\uff2c\uff2c\uff2f there"
    assert only({"kind": "says", "any_of": ["hello"]}, one=result("one", fullwidth_hello)).ok
    assert not only({"kind": "says", "any_of": ["cafe"]}, one=result("one", "Café")).ok


def test_a_pattern_may_span_lines_and_ignores_case():
    got = only({"kind": "says", "pattern": "alpha.beta"}, one=result("one", "ALPHA\nBETA"))
    assert got.ok


def test_says_reads_every_step_unless_one_is_named():
    t = {"one": result("one", "alpha"), "two": result("two", "beta")}
    assert only({"kind": "says", "all_of": ["alpha", "beta"]}, **t).ok
    assert not only({"kind": "says", "step": "one", "all_of": ["alpha", "beta"]}, **t).ok
    assert only({"kind": "says", "step": "two", "all_of": ["beta"]}, **t).ok


def test_long_quotes_are_shortened_and_flattened():
    got = only({"kind": "says", "any_of": ["x"]}, one=result("one", ("x " * 200).strip()))
    assert got.ok and got.detail.endswith("…'")
    assert got.detail == "said " + repr(("x " * 60).strip()[:119] + "…")  # 119 characters and "…"
    assert "\n" not in got.detail
    nothing = only({"kind": "says", "all_of": ["zzz"]}, one=result("one", ("y " * 200).strip()))
    assert nothing.detail == "missing 'zzz' in " + repr(("y " * 60).strip()[:119] + "…")
    flat_again = only({"kind": "silent", "step": "one"}, one=result("one", "a\n\n  b\tc"))
    assert flat_again.detail.endswith("the first: 'a b c'")


def test_a_quote_exactly_at_the_limit_is_kept_whole():
    text = "q" * 120
    assert only({"kind": "says", "any_of": ["q"]}, one=result("one", text)).detail == (
        f"said {text!r}"
    )
    assert only({"kind": "says", "any_of": ["q"]}, one=result("one", text + "q")).detail != (
        f"said {text + 'q'!r}"
    )
    loud = "L" * 80
    assert only({"kind": "silent", "step": "one"}, one=result("one", loud)).detail == (
        f"said 1 message(s), the first: {loud!r}"
    )


def test_never_says_fails_on_a_phrase_or_a_pattern_and_passes_on_silence():
    assert only({"kind": "never_says", "any_of": ["secret"]}) == Verdict(
        "never_says", True, "never said it"
    )
    phrase = only({"kind": "never_says", "any_of": ["secret"]}, one=result("one", "A SECRET!"))
    assert phrase == Verdict("never_says", False, "said 'secret'")
    pattern = only({"kind": "never_says", "pattern": r"\d{4}"}, one=result("one", "pin 1234"))
    assert pattern == Verdict("never_says", False, r"said something matching '\\d{4}'")
    assert only({"kind": "never_says", "pattern": r"\d{4}"}, one=result("one", "pin 12")).ok


def test_never_says_looks_at_every_step_unless_one_is_named():
    t = {"two": result("two", "oops secret")}
    assert not only({"kind": "never_says", "any_of": ["secret"]}, **t).ok
    assert only({"kind": "never_says", "step": "one", "any_of": ["secret"]}, **t).ok


# ---- counting and urgency -----------------------------------------------------------------------------
@pytest.mark.parametrize(("limit", "ok"), [(0, False), (1, False), (2, True), (3, True)])
def test_max_messages_allows_up_to_the_limit(limit, ok):
    t = {"one": result("one", "a"), "two": result("two", "b")}
    got = only({"kind": "max_messages", "limit": limit}, **t)
    assert got.ok is ok
    assert got.detail == f"2 message(s); at most {limit} allowed"


def test_max_messages_can_be_about_one_step():
    t = {"one": result("one", "a", "b", "c"), "two": result("two")}
    assert only({"kind": "max_messages", "step": "two", "limit": 0}, **t).ok
    assert not only({"kind": "max_messages", "step": "one", "limit": 2}, **t).ok


def test_not_urgent_fails_on_the_first_urgent_message_only():
    alarm = only(
        {"kind": "not_urgent"},
        one=result("one", out("calm"), out("FIRE", "urgent")),
    )
    assert alarm == Verdict("not_urgent", False, "sent an urgent message: 'FIRE'")
    assert only({"kind": "not_urgent"}, one=result("one", out("a", "low"), out("b"))).ok
    scoped = {"two": result("two", out("FIRE", "urgent"))}
    assert only({"kind": "not_urgent", "step": "one"}, **scoped).ok
    assert not only({"kind": "not_urgent", "step": "two"}, **scoped).ok


def test_phrases_are_matched_without_regard_to_case_or_width_on_either_side():
    text = "It will ARRIVE on Saturday, \uff53\uff41\uff54 at the latest"
    assert only({"kind": "says", "any_of": ["Saturday"]}, one=result("one", text)).ok
    assert only(
        {"kind": "says", "all_of": ["Arrive", "SATURDAY", "\uff33\uff41\uff54"]},
        one=result("one", text),
    ).ok
    assert not only({"kind": "never_says", "any_of": ["Saturday"]}, one=result("one", text)).ok
    assert not only({"kind": "never_says", "any_of": ["ARRIVE"]}, one=result("one", text)).ok
    assert only({"kind": "never_says", "any_of": ["Sunday"]}, one=result("one", text)).ok


def test_patterns_ignore_case_and_may_cross_lines_but_are_not_anchored():
    text = "first line\nthen DELAYED until later"
    for kind in ("says", "never_says"):
        found = only({"kind": kind, "pattern": "line.then delayed"}, one=result("one", text))
        assert found.ok is (kind == "says"), kind
        elsewhere = only({"kind": kind, "pattern": "until"}, one=result("one", text))
        assert elsewhere.ok is (kind == "says"), kind


# ---- tools ---------------------------------------------------------------------------------------------
def test_tools_used_lists_what_was_never_called():
    t = {"one": result("one", tools=["memory"]), "two": result("two", tools=["web_fetch"])}
    assert only({"kind": "tools_used", "tools": ["memory", "web_fetch"]}, **t) == Verdict(
        "tools_used", True, "all called"
    )
    miss = only({"kind": "tools_used", "tools": ["memory", "shell", "browse"]}, **t)
    assert miss == Verdict("tools_used", False, "never called shell, browse")
    assert not only({"kind": "tools_used", "step": "one", "tools": ["web_fetch"]}, **t).ok


def test_tools_not_used_names_the_ones_that_were():
    t = {"one": result("one", tools=["shell", "memory"])}
    hit = only({"kind": "tools_not_used", "tools": ["shell", "memory", "web_fetch"]}, **t)
    assert hit == Verdict("tools_not_used", False, "called shell, memory")
    assert only({"kind": "tools_not_used", "tools": ["web_fetch"]}, **t) == Verdict(
        "tools_not_used", True, "none called"
    )
    assert only({"kind": "tools_not_used", "step": "two", "tools": ["shell"]}, **t).ok


def test_only_tools_names_the_extras_sorted():
    t = {"one": result("one", tools=["send_message", "zeta", "alpha"])}
    extra = only({"kind": "only_tools", "allowed": ["send_message"]}, **t)
    assert extra == Verdict("only_tools", False, "also called alpha, zeta")
    assert only({"kind": "only_tools", "allowed": ["send_message", "zeta", "alpha"]}, **t) == (
        Verdict("only_tools", True, "only the allowed")
    )
    assert only({"kind": "only_tools", "allowed": []}) == Verdict(
        "only_tools", True, "only the allowed"
    )


# ---- the profile -----------------------------------------------------------------------------------------
def test_profile_requires_every_run_of_the_step_to_have_it():
    ok = only(
        {"kind": "profile", "step": "one", "expect": "untrusted"},
        one=result("one", runs=[run(profile="untrusted")]),
    )
    assert ok == Verdict("profile-one", True, "ran as 'untrusted'")
    mixed = only(
        {"kind": "profile", "step": "one", "expect": "full"},
        one=result("one", runs=[run(profile="full"), run(profile="scheduled")]),
    )
    assert mixed == Verdict("profile-one", False, "ran as ['full', 'scheduled'], expected 'full'")
    none = only({"kind": "profile", "step": "one", "expect": "full"}, one=result("one", runs=[]))
    assert none == Verdict("profile-one", False, "ran as nothing, expected 'full'")


def test_the_profiles_found_are_listed_in_a_fixed_order():
    names = ["zeta", "alpha", "mid", "omega", "beta", "gamma", "alpha"]
    got = only(
        {"kind": "profile", "step": "one", "expect": "full"},
        one=result("one", runs=[run(profile=p) for p in names]),
    )
    assert got.detail == f"ran as {sorted(set(names))}, expected 'full'"


# ---- memory --------------------------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    (root / "memory" / "state" / "tasks").mkdir(parents=True)
    (root / "memory" / "preferences").mkdir(parents=True)
    return root


def write(home: Path, rel: str, text: str) -> Path:
    file = home / "memory" / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    return file


def test_memory_has_finds_a_file_by_glob_and_by_what_it_says(home):
    write(home, "state/tasks/trip.md", "# Trip\nBook the TRAIN on Friday\n")
    write(home, "state/notes.md", "nothing here")
    got = only({"kind": "memory_has", "path": "state/tasks/*.md"}, home=home)
    assert got == Verdict("memory_has", True, "found state/tasks/trip.md")
    says = only(
        {"kind": "memory_has", "path": "state/**/*.md", "contains": ["train", "friday"]},
        home=home,
    )
    assert says == Verdict("memory_has", True, "state/tasks/trip.md says it")


def test_memory_has_reports_what_is_missing(home):
    write(home, "state/tasks/trip.md", "book the train")
    none = only({"kind": "memory_has", "path": "state/nothing/*.md"}, home=home)
    assert none == Verdict("memory_has", False, "no file matches state/nothing/*.md")
    wrong = only(
        {"kind": "memory_has", "path": "state/tasks/*.md", "contains": ["train", "hotel"]},
        home=home,
    )
    assert (
        not wrong.ok
        and "1 file(s) match state/tasks/*.md, none contains ['train', 'hotel']" in (wrong.detail)
    )


def test_the_words_may_be_in_any_case_and_width(home):
    write(home, "state/trip.md", "Arrives on SATURDAY, \uff54\uff52\uff41\uff49\uff4e 7\n")
    got = only(
        {"kind": "memory_has", "path": "state/trip.md", "contains": ["Saturday", "TRAIN"]},
        home=home,
    )
    assert got.ok


def test_every_word_must_be_in_the_same_file(home):
    write(home, "state/a.md", "train")
    write(home, "state/b.md", "hotel")
    assert not only(
        {"kind": "memory_has", "path": "state/*.md", "contains": ["train", "hotel"]}, home=home
    ).ok


def test_memory_has_lists_at_most_three_matches(home):
    for n in range(5):
        write(home, f"state/n{n}.md", "x")
    got = only({"kind": "memory_has", "path": "state/*.md"}, home=home)
    assert got.detail == "found state/n0.md, state/n1.md, state/n2.md"


def test_memory_has_ignores_folders_and_links_and_never_leaves_the_memory_folder(home, tmp_path):
    (home / "memory" / "state" / "dir.md").mkdir()
    secret = tmp_path / "outside.md"
    secret.write_text("top secret")
    (home / "memory" / "state" / "link.md").symlink_to(secret)
    inner = write(home, "state/real.txt", "real")
    (home / "memory" / "state" / "alias.md").symlink_to(inner)
    assert not only({"kind": "memory_has", "path": "state/*.md"}, home=home).ok
    assert not only(
        {"kind": "memory_has", "path": "state/*.md", "contains": ["secret"]}, home=home
    ).ok


def test_a_folder_that_leads_outside_the_memory_folder_is_not_searched(home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.md").write_text("top secret")
    (home / "memory" / "state" / "door").symlink_to(outside, target_is_directory=True)
    assert not only({"kind": "memory_has", "path": "state/door/*.md"}, home=home).ok
    assert not only(
        {"kind": "memory_has", "path": "state/door/*.md", "contains": ["secret"]}, home=home
    ).ok


def test_memory_has_reads_only_the_start_of_a_huge_file(home):
    write(home, "state/huge.md", "a" * (checks.MAX_FILE_CHARS + 10) + " needle")
    assert not only(
        {"kind": "memory_has", "path": "state/huge.md", "contains": ["needle"]}, home=home
    ).ok
    write(home, "state/edge.md", "needle " + "a" * checks.MAX_FILE_CHARS)
    assert only(
        {"kind": "memory_has", "path": "state/edge.md", "contains": ["needle"]}, home=home
    ).ok


def test_memory_has_survives_bytes_that_are_not_text(home):
    (home / "memory" / "state" / "bin.md").write_bytes(b"ok \xff\xfe needle")
    assert only(
        {"kind": "memory_has", "path": "state/bin.md", "contains": ["needle"]}, home=home
    ).ok


SEED = {"preferences": {"rules.md": "Always answer in Chinese\n"}}


def test_untouched_files_pass_when_byte_for_byte_equal(home):
    write(home, "preferences/rules.md", SEED["preferences"]["rules.md"])
    got = only(
        {"kind": "memory_untouched", "paths": ["preferences/rules.md"]}, seed=SEED, home=home
    )
    assert got == Verdict("memory_untouched", True, "left as the owner wrote it")


@pytest.mark.parametrize("text", ["Always answer in Chinese", "Always answer in Chinese\n\n", ""])
def test_a_changed_file_fails_even_by_one_character(home, text):
    write(home, "preferences/rules.md", text)
    got = only(
        {"kind": "memory_untouched", "paths": ["preferences/rules.md"]}, seed=SEED, home=home
    )
    assert got == Verdict("memory_untouched", False, "preferences/rules.md was changed")


def test_a_file_that_vanished_or_became_a_folder_is_gone(home):
    check = {"kind": "memory_untouched", "paths": ["preferences/rules.md"]}
    assert only(check, seed=SEED, home=home).detail == "preferences/rules.md is gone"
    (home / "memory" / "preferences" / "rules.md").mkdir()
    assert only(check, seed=SEED, home=home).detail == "preferences/rules.md is gone"


def test_every_listed_file_is_checked(home):
    seed = {"preferences": {"a.md": "A", "b.md": "B"}}
    write(home, "preferences/a.md", "A")
    got = only(
        {"kind": "memory_untouched", "paths": ["preferences/a.md", "preferences/b.md"]},
        seed=seed,
        home=home,
    )
    assert got.detail == "preferences/b.md is gone"


# ---- money -----------------------------------------------------------------------------------------------
def test_spend_must_stay_strictly_under_the_limit():
    assert only({"kind": "spend_under", "usd": 0.25}, cost=0.1).detail == (
        "spent $0.1000 of $0.2500"
    )
    assert only({"kind": "spend_under", "usd": 0.25}, cost=0.1).ok
    assert not only({"kind": "spend_under", "usd": 0.25}, cost=0.25).ok
    assert not only({"kind": "spend_under", "usd": 0.25}, cost=0.3).ok


# ---- all together ---------------------------------------------------------------------------------------
def test_every_check_of_the_scenario_is_judged_in_order_with_its_name():
    sc = scenario(
        {"kind": "silent", "step": "two"},
        {"kind": "says", "step": "one", "any_of": ["hi"], "id": "greets"},
        {"kind": "max_messages", "limit": 5},
    )
    t = trial(sc, result("one", "hi there"))
    assert [(v.id, v.ok) for v in judge(t)] == [
        ("ends-one", True),
        ("ends-two", True),
        ("silent-two", True),
        ("greets", True),
        ("max_messages", True),
    ]


# ---- the words used to describe a check ---------------------------------------------------------------
def spec_check(**fields):
    return Scenario.model_validate(
        {
            "id": "d",
            "title": "t",
            "seed": fields.pop("seed", {}),
            "steps": [{"id": "s", "message": "m"}],
            "checks": [fields],
        }
    ).checks[0]


@pytest.mark.parametrize(
    ("fields", "text"),
    [
        ({"kind": "silent", "step": "s"}, "in `s` the agent says nothing"),
        (
            {"kind": "says", "any_of": ["a", "b"], "all_of": ["c"], "pattern": "d+"},
            "across all steps the agent says one of 'a', 'b' and all of 'c' and something matching /d+/",
        ),
        (
            {"kind": "says", "step": "s", "pattern": "x"},
            "in `s` the agent says something matching /x/",
        ),
        (
            {"kind": "never_says", "any_of": ["a"], "pattern": "b"},
            "across all steps the agent never says 'a', anything matching /b/",
        ),
        ({"kind": "max_messages", "step": "s", "limit": 2}, "in `s` at most 2 message(s) are sent"),
        ({"kind": "not_urgent"}, "across all steps nothing is sent as urgent"),
        ({"kind": "tools_used", "tools": ["a", "b"]}, "across all steps the agent calls a, b"),
        (
            {"kind": "tools_not_used", "step": "s", "tools": ["a"]},
            "in `s` the agent calls none of a",
        ),
        (
            {"kind": "only_tools", "allowed": ["a", "b"]},
            "across all steps the agent calls nothing but a, b",
        ),
        (
            {"kind": "only_tools", "allowed": []},
            "across all steps the agent calls nothing but (nothing)",
        ),
        ({"kind": "profile", "step": "s", "expect": "full"}, "`s` runs with the 'full' profile"),
        ({"kind": "memory_has", "path": "state/*.md"}, "memory has a file matching `state/*.md`"),
        (
            {"kind": "memory_has", "path": "state/*.md", "contains": ["a", "b"]},
            "memory has a file matching `state/*.md` that says 'a', 'b'",
        ),
        ({"kind": "spend_under", "usd": 0.25}, "the whole scenario costs less than $0.25"),
    ],
)
def test_checks_are_described_in_plain_words(fields, text):
    assert describe(spec_check(**fields)) == text


def test_the_untouched_files_are_described_by_name():
    check = spec_check(
        kind="memory_untouched",
        paths=["preferences/a.md", "preferences/b.md"],
        seed={"preferences": {"a.md": "A", "b.md": "B"}},
    )
    assert describe(check) == (
        "the owner's files stay exactly as written: preferences/a.md, preferences/b.md"
    )
