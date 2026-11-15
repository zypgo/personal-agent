"""Reports: what a run leaves behind for the owner, with nothing secret in it."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from personal_agent.evals import report
from personal_agent.evals.checks import Verdict
from personal_agent.evals.report import (
    checked_dict,
    checked_markdown,
    live_dict,
    live_markdown,
    noise_of,
    reports_dir,
    save,
)
from personal_agent.evals.runner import (
    BadResult,
    CheckedScenario,
    LiveRun,
    ScenarioResult,
    StepSummary,
    TrialResult,
)
from personal_agent.paths import Paths

WHEN = datetime(2026, 11, 16, 9, 30, 5, tzinfo=timezone(timedelta(hours=8)))


def fake_openai_key() -> str:
    return "sk-" + "proj-" + "Zx9Y8w7V" * 4  # built here so no real-looking key sits in the source


def trial(
    index: int = 0,
    *,
    ok: bool = True,
    detail: str = "fine",
    said: str = "hello",
    cost: float = 0.0123456789,
    quiet: int = 0,
    noise: int = 0,
    error: str | None = None,
) -> TrialResult:
    return TrialResult(
        scenario_id="demo",
        index=index,
        verdicts=[
            Verdict("ends-tick", True, "ended as completed"),
            Verdict("silent-tick", ok, detail),
        ],
        steps=[StepSummary("tick", [("normal", said)], ["send_message"], ["completed"])],
        cost_usd=cost,
        requests=2,
        quiet_steps=quiet,
        noise=noise,
        error=error,
    )


def live_run(*scenarios: ScenarioResult, cost: float = 0.5) -> LiveRun:
    return LiveRun(
        list(scenarios), cost_usd=cost, cap_usd=2.0, trials=2, model="gpt-x", effort="medium"
    )


@pytest.fixture
def paths(tmp_path):
    value = Paths(home=tmp_path / "home")
    value.ensure()
    return value


# ---- noise ------------------------------------------------------------------------------------------------
def test_noise_is_messages_per_step_that_asked_for_silence():
    assert noise_of([]) == {"quiet_steps": 0, "messages": 0, "per_quiet_step": None}
    got = noise_of([trial(quiet=1, noise=0), trial(quiet=2, noise=1), trial(quiet=0, noise=5)])
    assert got == {"quiet_steps": 3, "messages": 6, "per_quiet_step": 2.0}
    assert noise_of([trial(quiet=3, noise=1)])["per_quiet_step"] == 0.333  # rounded to 3 places


# ---- live, as data -----------------------------------------------------------------------------------------
def test_a_live_run_as_data():
    run = live_run(
        ScenarioResult(
            "demo", "A demo", [trial(0), trial(1, ok=False, detail="said 1 message(s)")]
        ),
        ScenarioResult("other", "Other", [], skipped=2),
    )
    data = live_dict(run, when=WHEN)
    assert {k: data[k] for k in ("mode", "when", "model", "effort", "trials_per_scenario")} == {
        "mode": "live",
        "when": "2026-11-16T09:30:05+08:00",
        "model": "gpt-x",
        "effort": "medium",
        "trials_per_scenario": 2,
    }
    assert data["ok"] is False and data["cost_usd"] == 0.5 and data["cap_usd"] == 2.0
    demo, other = data["scenarios"]
    assert (demo["id"], demo["title"], demo["passed"], demo["ran"], demo["skipped"]) == (
        "demo",
        "A demo",
        1,
        2,
        0,
    )
    assert demo["all_passed"] is False and other["all_passed"] is False and other["skipped"] == 2
    first, second = demo["trials"]
    assert first["ok"] is True and second["ok"] is False
    assert first["cost_usd"] == 0.012346 and first["requests"] == 2  # rounded to the microdollar
    assert first["checks"][1] == {"id": "silent-tick", "ok": True, "detail": "fine"}
    assert second["checks"][1] == {"id": "silent-tick", "ok": False, "detail": "said 1 message(s)"}
    assert first["steps"] == [
        {
            "id": "tick",
            "statuses": ["completed"],
            "tools": ["send_message"],
            "messages": [{"urgency": "normal", "text": "hello"}],
        }
    ]
    json.dumps(data)  # it must be plain JSON


def test_a_live_run_where_everything_held_says_so():
    run = live_run(ScenarioResult("demo", "A demo", [trial(0), trial(1)]))
    data = live_dict(run, when=WHEN)
    assert data["ok"] is True and data["scenarios"][0]["all_passed"] is True


def test_what_the_agent_said_is_scrubbed_of_secrets_and_cut_short(monkeypatch):
    monkeypatch.setenv("SERVICE_API_TOKEN", "plainvalue-" + "Q" * 20)
    leaked = fake_openai_key()
    bad = trial(
        said=f"key {leaked} and plainvalue-{'Q' * 20} " + "x" * 1000,
        detail=f"said {leaked}",
        error=f"boom {leaked}",
        ok=False,
    )
    data = live_dict(live_run(ScenarioResult("demo", "Demo", [bad])), when=WHEN)
    text = json.dumps(data)
    assert leaked not in text and "plainvalue-" + "Q" * 20 not in text
    saved = data["scenarios"][0]["trials"][0]
    shown = saved["steps"][0]["messages"][0]["text"]
    assert len(shown) == report.SHOWN_CHARS and shown.endswith("…")
    assert leaked not in saved["error"] and leaked not in saved["checks"][1]["detail"]


def test_text_exactly_at_the_limit_is_not_cut():
    exact = "y" * report.SHOWN_CHARS
    data = live_dict(live_run(ScenarioResult("demo", "Demo", [trial(said=exact)])), when=WHEN)
    assert data["scenarios"][0]["trials"][0]["steps"][0]["messages"][0]["text"] == exact


# ---- live, as text ----------------------------------------------------------------------------------------
def test_the_live_report_has_a_table_and_only_the_trials_that_failed():
    run = live_run(
        ScenarioResult("fine", "All good", [trial(0), trial(1)]),
        ScenarioResult(
            "shaky",
            "Not always",
            [
                trial(0),
                trial(1, ok=False, detail="said 1 message(s), the first: 'hi'", said="hi there"),
            ],
        ),
        ScenarioResult("cut", "Out of money", [trial(0)], skipped=1),
        cost=0.3333,
    )
    text = live_markdown(live_dict(run, when=WHEN))
    assert text.startswith("# Live evaluation, 2026-11-16T09:30:05+08:00\n")
    assert "- Model: `gpt-x` (reasoning effort `medium`)" in text
    assert "- Trials per scenario: 2" in text and "- Spent: $0.3333 of the $2.00 ceiling" in text
    assert "- Result: **something did not hold**" in text
    assert "| `fine` | 2/2 | yes |" in text
    assert "| `shaky` | 1/2 | no |" in text
    assert "| `cut` | 1/1, 1 not run (ceiling reached) | no |" in text
    assert "## shaky: Not always" in text and "## fine" not in text
    assert "**Trial 2**" in text and "**Trial 1**" not in text
    assert "- `silent-tick` failed: said 1 message(s), the first: 'hi'" in text
    assert "`ends-tick` failed" not in text  # a check that held is not listed among the failures
    assert "  - step `tick` ['completed'], tools ['send_message']: [normal] hi there" in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_a_run_where_everything_held_has_no_failure_sections():
    text = live_markdown(
        live_dict(live_run(ScenarioResult("fine", "All good", [trial(0)])), when=WHEN)
    )
    assert "- Result: **all scenarios held every time**" in text
    assert "\n## " not in text


def test_a_trial_that_could_not_be_played_says_so():
    broken = TrialResult("demo", 0, error="step 'ask': the message was not taken as a message")
    text = live_markdown(live_dict(live_run(ScenarioResult("demo", "Demo", [broken])), when=WHEN))
    assert (
        "**Trial 1** could not be played: step 'ask': the message was not taken as a message"
        in text
    )


def test_a_step_where_the_agent_said_nothing_is_shown_as_nothing():
    silent = trial(ok=False)
    silent.steps[0].messages.clear()
    text = live_markdown(live_dict(live_run(ScenarioResult("demo", "Demo", [silent])), when=WHEN))
    assert "tools ['send_message']: (nothing)" in text


def test_the_noise_line_says_what_was_measured():
    quiet = live_run(ScenarioResult("demo", "Demo", [trial(quiet=2, noise=1)]))
    text = live_markdown(live_dict(quiet, when=WHEN))
    assert "- Noise: 1 message(s) in 2 step(s) that asked for silence (0.5 per quiet step)" in text
    none = live_run(ScenarioResult("demo", "Demo", [trial(quiet=0)]))
    assert "- Noise: no scenario here asks for silence" in live_markdown(live_dict(none, when=WHEN))


# ---- scripted ---------------------------------------------------------------------------------------------
def sound() -> CheckedScenario:
    return CheckedScenario(
        "sound",
        "Sound",
        good=TrialResult("sound", 0, [Verdict("a", True, "")]),
        bad=[BadResult("chatty", ["a"], ["a"])],
    )


def unsound() -> CheckedScenario:
    leaked = fake_openai_key()
    return CheckedScenario(
        "weak",
        "Weak",
        good=TrialResult(
            "weak", 0, [Verdict("a", False, f"said {leaked}")], error=f"oops {leaked}"
        ),
        bad=[
            BadResult("quiet-one", ["a"], []),
            BadResult(
                "odd", ["b"], [], error="the script has 2 model turn(s) but the agent took 1"
            ),
            BadResult("fine", ["a"], ["a"]),
        ],
    )


def test_a_self_check_as_data():
    data = checked_dict(
        [sound(), unsound(), CheckedScenario("bare", "Bare", unscripted=True)], when=WHEN
    )
    assert data["mode"] == "scripted" and data["when"] == "2026-11-16T09:30:05+08:00"
    assert data["ok"] is False
    fine, weak, bare = data["scenarios"]
    assert [s["status"] for s in data["scenarios"]] == ["ok", "failed", "unscripted"]
    assert fine["good"] == {"ok": True, "error": None, "failed": []}
    assert fine["bad"] == [
        {"name": "chatty", "expected": ["a"], "failed": ["a"], "caught": True, "error": None}
    ]
    assert bare["good"] is None and bare["bad"] == []
    assert weak["good"]["ok"] is False
    assert [b["caught"] for b in weak["bad"]] == [False, False, True]
    assert weak["bad"][1]["error"].startswith("the script has 2 model turn(s)")
    assert fake_openai_key() not in json.dumps(data)  # scrubbed here too
    json.dumps(data)


def test_a_self_check_where_everything_is_sound_is_ok():
    assert checked_dict([sound()], when=WHEN)["ok"] is True
    assert checked_dict([sound(), CheckedScenario("bare", "Bare", unscripted=True)], when=WHEN)[
        "ok"
    ]
    assert checked_dict([], when=WHEN)["ok"] is True


def test_the_self_check_report_explains_only_what_is_wrong():
    text = checked_markdown(checked_dict([sound(), unsound()], when=WHEN))
    assert text.startswith("# Scenario self-check, 2026-11-16T09:30:05+08:00\n")
    assert "| `sound` | ok | 1/1 |" in text
    assert "| `weak` | failed | 1/3 |" in text
    assert "## weak" in text and "## sound" not in text
    assert "- the good run did not pass: oops " in text
    assert "  - `a`: said " in text
    assert "- the bad run `quiet-one` should fail ['a'] but failed nothing" in text
    assert (
        "- the bad run `odd` should fail ['b'] but failed nothing "
        "(the script has 2 model turn(s) but the agent took 1)"
    ) in text
    assert "`fine`" not in text.split("## weak")[1]  # the caught one needs no explanation
    assert fake_openai_key() not in text


def test_what_a_bad_run_reports_is_scrubbed_and_cut_like_everything_else():
    leaked = fake_openai_key()
    odd = CheckedScenario(
        "weak",
        "Weak",
        good=TrialResult("weak", 0, [Verdict("a", True, "")]),
        bad=[BadResult("odd", ["b"], [], error=f"broken {leaked} " + "z" * 1000)],
    )
    data = checked_dict([odd], when=WHEN)
    shown = data["scenarios"][0]["bad"][0]["error"]
    assert leaked not in json.dumps(data) and leaked not in checked_markdown(data)
    assert len(shown) == report.SHOWN_CHARS and shown.endswith("\u2026")


def test_a_good_run_that_passed_is_not_called_out_when_only_a_bad_run_slipped_through():
    weak_only_bad = CheckedScenario(
        "weak",
        "Weak",
        good=TrialResult("weak", 0, [Verdict("a", True, "")]),
        bad=[BadResult("harmless", ["a"], [])],
    )
    text = checked_markdown(checked_dict([weak_only_bad], when=WHEN))
    assert "the good run did not pass" not in text
    assert "- the bad run `harmless` should fail ['a'] but failed nothing" in text


# ---- files -------------------------------------------------------------------------------------------------
def test_reports_are_written_privately_in_the_owners_folder(paths):
    data = {"mode": "live", "note": "你好"}
    json_file, md_file = save(paths, data, "# Report\n", when=WHEN)
    assert (
        json_file.parent == md_file.parent == reports_dir(paths) == paths.home / "evals" / "reports"
    )
    assert (json_file.name, md_file.name) == (
        "20261116-093005-live.json",
        "20261116-093005-live.md",
    )
    assert json.loads(json_file.read_text(encoding="utf-8")) == data
    assert "你好" in json_file.read_text(encoding="utf-8")  # readable, not \\u-escaped
    assert md_file.read_text(encoding="utf-8") == "# Report\n"
    assert json_file.read_text().endswith("}\n")
    assert json_file.read_text().startswith('{\n  "mode": "live",\n')  # indented: made to be read
    for file in (json_file, md_file):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    for folder in (json_file.parent, json_file.parent.parent):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_two_reports_in_the_same_second_never_overwrite_each_other(paths):
    first = save(paths, {"mode": "live", "n": 1}, "one\n", when=WHEN)
    second = save(paths, {"mode": "live", "n": 2}, "two\n", when=WHEN)
    third = save(paths, {"mode": "live", "n": 3}, "three\n", when=WHEN)
    assert [f[0].name for f in (first, second, third)] == [
        "20261116-093005-live.json",
        "20261116-093005-live-2.json",
        "20261116-093005-live-3.json",
    ]
    assert [f[1].read_text() for f in (first, second, third)] == ["one\n", "two\n", "three\n"]
    assert json.loads(first[0].read_text())["n"] == 1


def test_the_two_files_of_a_report_always_share_a_name(paths):
    folder = reports_dir(paths)
    folder.mkdir(parents=True)
    (folder / "20261116-093005-live.md").write_text("an earlier markdown")  # only one of the pair
    json_file, md_file = save(paths, {"mode": "live"}, "new\n", when=WHEN)
    assert json_file.stem == md_file.stem == "20261116-093005-live-2"
    assert (folder / "20261116-093005-live.md").read_text() == "an earlier markdown"


def test_a_leftover_json_alone_is_a_name_that_is_taken_too(paths):
    folder = reports_dir(paths)
    folder.mkdir(parents=True)
    (folder / "20261116-093005-live.json").write_text("an earlier json")
    json_file, md_file = save(paths, {"mode": "live"}, "new\n", when=WHEN)
    assert json_file.stem == md_file.stem == "20261116-093005-live-2"
    assert (folder / "20261116-093005-live.json").read_text() == "an earlier json"


def test_a_report_is_never_written_through_a_link_left_in_its_place(paths, tmp_path):
    folder = reports_dir(paths)
    folder.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.txt"
    (folder / "20261116-093005-live.json").symlink_to(elsewhere)  # points at nothing yet
    with pytest.raises(FileExistsError):
        save(paths, {"mode": "live"}, "x\n", when=WHEN)
    assert not elsewhere.exists()


def test_scripted_and_live_reports_have_their_own_names(paths):
    live = save(paths, {"mode": "live"}, "l\n", when=WHEN)[0].name
    scripted = save(paths, {"mode": "scripted"}, "s\n", when=WHEN)[0].name
    assert live == "20261116-093005-live.json" and scripted == "20261116-093005-scripted.json"
