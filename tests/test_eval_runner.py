"""Running scenarios: scripted (can the checks tell good from bad?) and live (does the model behave?)."""

from __future__ import annotations

import json
import os

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pydantic import TypeAdapter

from helpers import usage
from personal_agent.config import Config
from personal_agent.evals import runner
from personal_agent.evals.checks import Verdict
from personal_agent.evals.runner import (
    BadResult,
    CheckedScenario,
    LiveRun,
    ScenarioResult,
    TrialResult,
    check_scenario,
    play_live,
    play_scripted,
    run_live,
    turns,
)
from personal_agent.evals.spec import Scenario, Script
from personal_agent.security.sandbox import SandboxProvider

exists = os.path.exists
NO_SHELL = SandboxProvider.fixed(None, "no shell here")
SCRIPT = TypeAdapter(Script)


def scenario(
    *, steps=None, checks=None, mock=None, seed=None, sid: str = "demo", title: str = "Demo"
) -> Scenario:
    data: dict = {
        "id": sid,
        "title": title,
        "seed": seed or {},
        "steps": steps or [{"id": "tick", "timer": {"name": "daily", "prompt": "Check."}}],
        "checks": checks or [{"kind": "silent", "step": "tick"}],
    }
    if mock is not None:
        data["mock"] = mock
    return Scenario.model_validate(data)


# ---- turning a script into what the scripted model says ---------------------------------------------
def test_each_step_becomes_a_round_of_tool_calls_then_closing_words():
    sc = scenario(
        steps=[
            {"id": "a", "message": "hello"},
            {"id": "b", "message": "again", "session": "other"},
            {"id": "c", "message": "last"},
        ],
        checks=[{"kind": "max_messages", "limit": 9}],
    )
    actions = SCRIPT.validate_python(
        {
            "a": [
                {"send": "first"},
                {"send": "second", "urgency": "urgent"},
                {"memory": {"command": "view", "path": "/memories"}},
                {"tool": {"name": "web_fetch", "args": {"url": "https://example.com"}}},
                {"say": "All done."},
            ],
            "b": [{"say": "Only talk."}],
        }
    )
    made = turns(sc, actions)
    assert len(made) == 4  # a: calls + closing, b: closing, c: closing
    calls = made[0]
    assert [c.name for c in calls] == ["send_message", "send_message", "memory", "web_fetch"]
    assert [c.call_id for c in calls] == ["scripted-1", "scripted-2", "scripted-3", "scripted-4"]
    assert json.loads(calls[0].arguments) == {"text": "first", "urgency": "normal"}
    assert json.loads(calls[1].arguments) == {"text": "second", "urgency": "urgent"}
    assert json.loads(calls[2].arguments) == {"command": "view", "path": "/memories"}
    assert json.loads(calls[3].arguments) == {"url": "https://example.com"}
    assert made[1][0].content[0].text == "All done."
    assert made[2][0].content[0].text == "Only talk."
    assert made[3][0].content[0].text == "Done."  # a step with nothing to do closes politely


def test_call_numbers_keep_counting_through_the_steps():
    sc = scenario(
        steps=[{"id": "a", "message": "x"}, {"id": "b", "message": "y"}],
        checks=[{"kind": "max_messages", "limit": 9}],
    )
    made = turns(sc, SCRIPT.validate_python({"a": [{"send": "1"}], "b": [{"send": "2"}]}))
    assert made[0][0].call_id == "scripted-1" and made[2][0].call_id == "scripted-2"


def test_closing_words_can_stand_anywhere_in_a_steps_list_and_a_say_alone_makes_no_calls():
    sc = scenario()
    made = turns(sc, SCRIPT.validate_python({"tick": [{"say": "Early"}, {"send": "later"}]}))
    assert len(made) == 2 and made[1][0].content[0].text == "Early"
    alone = turns(sc, SCRIPT.validate_python({"tick": [{"say": "Only this"}]}))
    assert len(alone) == 1


# ---- one scripted play -----------------------------------------------------------------------------------
GOOD = {"good": {"tick": [{"send": "A parcel arrived.", "urgency": "low"}]}}


async def test_a_scripted_run_is_graded_and_summarised():
    sc = scenario(
        checks=[
            {"kind": "says", "step": "tick", "any_of": ["parcel"]},
            {"kind": "not_urgent"},
        ],
        mock=GOOD,
    )
    result = await play_scripted(sc, sc.mock.good)
    assert result.error is None and result.ok and result.cost_usd == 0.0
    [step] = result.steps
    assert step.id == "tick" and step.messages == [("low", "A parcel arrived.")]
    assert step.tools == ["send_message"] and step.statuses == ["completed"]
    assert [v.id for v in result.verdicts] == ["ends-tick", "says-tick", "not_urgent"]
    assert (result.quiet_steps, result.noise) == (0, 0)


async def test_a_step_that_should_stay_quiet_counts_its_noise():
    sc = scenario(checks=[{"kind": "silent", "step": "tick"}], mock=GOOD)
    result = await play_scripted(sc, sc.mock.good)
    assert not result.ok and [v.id for v in result.failed] == ["silent-tick"]
    assert (result.quiet_steps, result.noise) == (1, 1)


async def test_a_scenario_that_cannot_be_played_is_an_error_and_not_a_failed_check():
    sc = scenario(
        steps=[{"id": "ask", "message": "/help"}],
        checks=[{"kind": "max_messages", "limit": 9}],
        mock={"good": {}},
    )
    result = await play_scripted(sc, sc.mock.good)
    assert result.error and "not taken as a message" in result.error
    assert not result.ok and result.verdicts == []


async def test_a_script_that_makes_the_agent_take_another_number_of_turns_is_an_error(monkeypatch):
    sc = scenario(checks=[{"kind": "max_messages", "limit": 9}], mock={"good": {}})
    monkeypatch.setattr(runner, "turns", lambda *_a: [[assistant_message("a")]] * 2)
    result = await play_scripted(sc, sc.mock.good)
    assert result.error == (
        "the script has 2 model turn(s) but the agent took 1: "
        "a step must make the agent do exactly what its actions say"
    )
    assert not result.ok


async def test_a_script_too_short_for_what_the_agent_does_says_so_instead_of_only_failing(
    monkeypatch,
):
    sc = scenario(checks=[{"kind": "max_messages", "limit": 9}], mock={"good": {}})
    one_call = [[function_call("send_message", {"text": "hi", "urgency": "low"}, call_id="c1")]]
    monkeypatch.setattr(runner, "turns", lambda *_a: one_call)  # the reply to the tool is missing
    result = await play_scripted(sc, sc.mock.good)
    assert not result.ok
    assert result.error == (
        "the script has 1 model turn(s) but the agent took 2: "
        "a step must make the agent do exactly what its actions say"
    )


# ---- does a scenario tell good from bad? -----------------------------------------------------------------
def checked(**mock) -> Scenario:
    return scenario(
        checks=[
            {"kind": "silent", "step": "tick", "id": "quiet"},
            {"kind": "not_urgent", "id": "calm"},
        ],
        mock=mock,
    )


async def test_a_scenario_whose_bad_runs_all_fail_what_they_should_is_sound():
    sc = checked(
        good={"tick": []},
        bad=[
            {"name": "chatty", "fails": ["quiet"], "script": {"tick": [{"send": "hi"}]}},
            {
                "name": "alarmist",
                "fails": ["calm"],
                "script": {"tick": [{"send": "FIRE", "urgency": "urgent"}]},
            },
        ],
    )
    result = await check_scenario(sc)
    assert result.ok and not result.unscripted and result.good.ok
    assert [(b.name, b.caught) for b in result.bad] == [("chatty", True), ("alarmist", True)]
    assert result.bad[1].failed == ["quiet", "calm"]  # it also broke the quiet, which is allowed


async def test_a_scenario_without_scripts_has_nothing_to_prove_and_is_said_to_be_untested():
    result = await check_scenario(scenario())
    assert result.unscripted and result.ok and result.good is None and result.bad == []


async def test_a_good_run_that_does_not_pass_makes_the_scenario_unsound():
    sc = checked(good={"tick": [{"send": "hi"}]}, bad=[])
    result = await check_scenario(sc)
    assert not result.ok and [v.id for v in result.good.failed] == ["quiet"]


async def test_a_bad_run_that_passes_every_check_shows_the_checks_are_too_weak():
    sc = checked(
        good={"tick": []},
        bad=[{"name": "harmless", "fails": ["quiet"], "script": {"tick": []}}],
    )
    result = await check_scenario(sc)
    [bad] = result.bad
    assert not bad.caught and bad.failed == [] and not result.ok


async def test_a_bad_run_must_fail_all_the_checks_it_names_not_just_one():
    sc = checked(
        good={"tick": []},
        bad=[
            {
                "name": "half",
                "fails": ["quiet", "calm"],
                "script": {"tick": [{"send": "hi"}]},  # chatty, but not urgent
            }
        ],
    )
    [bad] = (await check_scenario(sc)).bad
    assert not bad.caught and bad.failed == ["quiet"]


async def test_a_broken_bad_script_is_reported_only_when_no_check_explains_the_result(monkeypatch):
    real = runner.turns

    def padded(scenario, script):
        steps = real(scenario, script)
        return steps if script is scenario.mock.good else [*steps, [assistant_message("extra")]]

    monkeypatch.setattr(runner, "turns", padded)
    weak = checked(
        good={"tick": []},
        bad=[{"name": "harmless", "fails": ["quiet"], "script": {"tick": []}}],
    )
    [bad] = (await check_scenario(weak)).bad
    assert bad.failed == [] and bad.error and "model turn(s)" in bad.error
    caught = checked(
        good={"tick": []},
        bad=[{"name": "chatty", "fails": ["quiet"], "script": {"tick": [{"send": "hi"}]}}],
    )
    [bad] = (await check_scenario(caught)).bad
    assert bad.caught and bad.error is None


def test_how_results_are_summed_up():
    ok, no = Verdict("a", True, ""), Verdict("b", False, "")
    assert TrialResult("s", 0, [ok]).ok
    assert not TrialResult("s", 0, [ok, no]).ok
    assert not TrialResult("s", 0).ok  # nothing was checked
    assert not TrialResult("s", 0, [ok], error="boom").ok
    assert [v.id for v in TrialResult("s", 0, [ok, no]).failed] == ["b"]
    assert BadResult("x", ["a"], ["a", "b"]).caught
    assert not BadResult("x", ["a", "c"], ["a"]).caught
    assert not BadResult("x", ["a"], ["a"], error="broken").caught
    sound = TrialResult("s", 0, [ok])
    assert CheckedScenario("s", "S", good=sound, bad=[BadResult("x", ["a"], ["a"])]).ok
    assert not CheckedScenario("s", "S").ok  # no good run at all
    assert CheckedScenario("s", "S", unscripted=True).ok


def test_a_scenario_that_held_every_time_passed_every_trial_and_skipped_none():
    good, bad = TrialResult("s", 0, [Verdict("a", True, "")]), TrialResult("s", 1, [])
    assert ScenarioResult("s", "S", [good, good]).all_passed
    assert not ScenarioResult("s", "S", [good, bad]).all_passed
    assert ScenarioResult("s", "S", [good, bad]).passed == 1
    assert not ScenarioResult("s", "S", [good], skipped=1).all_passed
    assert not ScenarioResult("s", "S").all_passed
    run = LiveRun([ScenarioResult("s", "S", [good])], 0.0, 1.0, 1, "m", "low")
    assert run.ok
    assert not LiveRun([], 0.0, 1.0, 1, "m", "low").ok
    assert not LiveRun([ScenarioResult("s", "S", [bad])], 0.0, 1.0, 1, "m", "low").ok
    one_each = [ScenarioResult("a", "A", [good]), ScenarioResult("b", "B", [bad])]
    assert not LiveRun(
        one_each, 0.0, 1.0, 1, "m", "low"
    ).ok  # a run is as good as its worst scenario
    assert not LiveRun(one_each[::-1], 0.0, 1.0, 1, "m", "low").ok


# ---- live, with a stand-in for the model --------------------------------------------------------------
def owner(**extra) -> Config:
    return Config.model_validate(
        {"model": {"main": "owner-main", "reasoning_effort": "low"}, **extra}
    )


def quiet_model(**kw) -> ScriptedModel:
    return ScriptedModel([[assistant_message("Nothing new.")]] * 40, **kw)


async def live(scenarios, *, scripted=None, **kw):
    """`run_live` with a scripted model; returns the result, the model names asked for, the model."""
    scripted = scripted or quiet_model()
    names: list[str] = []

    def factory(name):
        names.append(name)
        return scripted

    kw.setdefault("owner", owner())
    kw.setdefault("trials", 2)
    kw.setdefault("cap_usd", 5.0)
    done = await run_live(scenarios, model_factory=factory, sandbox=NO_SHELL, **kw)
    return done, names, scripted


async def test_every_scenario_is_played_the_asked_number_of_times():
    first = scenario(sid="one", checks=[{"kind": "silent", "step": "tick"}])
    second = scenario(sid="two", checks=[{"kind": "silent", "step": "tick"}])
    seen: list[tuple[str, int]] = []
    done, _, model = await live(
        [first, second], trials=3, on_trial=lambda s, t: seen.append((s.id, t.index))
    )
    assert done.ok and done.trials == 3 and done.cap_usd == 5.0
    assert [(r.scenario_id, len(r.trials), r.passed) for r in done.results] == [
        ("one", 3, 3),
        ("two", 3, 3),
    ]
    assert seen == [("one", 0), ("one", 1), ("one", 2), ("two", 0), ("two", 1), ("two", 2)]
    assert len(model.calls) == 6
    assert done.cost_usd == 0.0


async def test_the_model_and_effort_are_the_owners_unless_the_run_says_otherwise():
    sc = scenario()
    done, names, model = await live([sc], trials=1)
    assert (done.model, done.effort) == ("owner-main", "low")
    assert set(names) == {"owner-main"}
    assert model.calls[0].model_settings.reasoning.effort == "low"
    done, names, model = await live([sc], trials=1, model="other-model", effort="high")
    assert (done.model, done.effort) == ("other-model", "high")
    assert set(names) == {"other-model"}
    assert model.calls[0].model_settings.reasoning.effort == "high"


async def test_one_trial_in_which_the_agent_speaks_is_noise_and_a_failed_trial():
    sc = scenario()
    talkative = ScriptedModel(
        [
            [assistant_message("Nothing new.")],
            [function_call("send_message", {"text": "hello!"}, call_id="x")],  # trial 2: speaks
            [assistant_message("sent")],
        ]
    )
    done, _, _ = await live([sc], scripted=talkative, trials=2)
    [result] = done.results
    assert [t.ok for t in result.trials] == [True, False]
    assert not result.all_passed and not done.ok
    assert [(t.quiet_steps, t.noise) for t in result.trials] == [(1, 0), (1, 1)]
    assert [v.id for v in result.trials[1].failed] == ["silent-tick"]


async def test_money_is_added_up_and_when_the_ceiling_is_reached_the_rest_is_not_played():
    sc1, sc2 = scenario(sid="one"), scenario(sid="two")
    pricey = quiet_model(default_usage=usage(2_000_000, 0))  # a few dollars for one request
    done, _, _ = await live([sc1, sc2], scripted=pricey, trials=3, cap_usd=1.0)
    first, second = done.results
    assert (len(first.trials), first.skipped, first.all_passed) == (1, 2, False)
    assert (len(second.trials), second.skipped, second.all_passed) == (0, 3, False)
    assert done.cost_usd == pytest.approx(first.trials[0].cost_usd) and done.cost_usd > 1.0
    assert not done.ok


async def test_each_trial_may_spend_only_what_is_left_of_the_ceiling(monkeypatch):
    caps: list[float | None] = []
    real = runner.eval_config

    def spy(*args, **kw):
        caps.append(kw.get("cap_usd"))
        return real(*args, **kw)

    monkeypatch.setattr(runner, "eval_config", spy)
    sc = scenario(seed={"config": {"budget": {"per_run_usd": 50, "daily_usd": 50}}})
    thrifty = quiet_model(default_usage=usage(100_000, 0))  # 20 cents a trial on a priced model
    priced = owner(model={"main": "gpt-6.1-sol"})
    done, _, _ = await live([sc], scripted=thrifty, trials=3, cap_usd=1.0, owner=priced)
    cost = done.results[0].trials[0].cost_usd
    assert cost == pytest.approx(0.2)
    assert caps == [pytest.approx(1.0), pytest.approx(1.0 - cost), pytest.approx(1.0 - 2 * cost)]


async def test_a_ceiling_with_nothing_left_to_start_a_trial_with_skips_it():
    sc = scenario()
    done, _, model = await live([sc], trials=2, cap_usd=runner.MIN_BUDGET_USD / 2)
    assert done.results[0].trials == [] and done.results[0].skipped == 2
    assert len(model.calls) == 0


async def test_a_trial_that_cannot_be_played_costs_nothing_and_says_why():
    sc = scenario(
        steps=[{"id": "ask", "message": "/help"}], checks=[{"kind": "max_messages", "limit": 1}]
    )
    done, _, _ = await live([sc], trials=1)
    [trial] = done.results[0].trials
    assert trial.error and "not taken as a message" in trial.error
    assert trial.cost_usd == 0 and not done.ok


async def test_a_config_the_scenario_breaks_is_reported_per_trial():
    sc = scenario(seed={"config": {"budget": {"per_run_usd": -1}}})
    done, _, _ = await live([sc], trials=1)
    [trial] = done.results[0].trials
    assert trial.error and "config in the seed is not valid" in trial.error


async def test_each_trial_gets_a_home_of_its_own_that_is_removed_afterwards(tmp_path, monkeypatch):
    homes: list[str] = []
    real = runner.play

    async def spy(sc, *, home, **kw):
        homes.append(str(home))
        assert not home.exists()
        trial = await real(sc, home=home, **kw)
        assert (home / "memory").is_dir()
        return trial

    monkeypatch.setattr(runner, "play", spy)
    await live([scenario()], trials=2)
    assert len(set(homes)) == 2
    assert not any(exists(h) for h in homes)


async def test_one_live_trial_has_the_index_it_was_given():
    result = await play_live(
        scenario(),
        4,
        owner=owner(),
        model=None,
        effort=None,
        cap_usd=1.0,
        model_factory=lambda _n: quiet_model(),
        sandbox=NO_SHELL,
    )
    assert result.index == 4 and result.ok
