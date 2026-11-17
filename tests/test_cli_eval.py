"""`pa eval`: the owner's way to see whether the agent behaves, without spending money by accident."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import invoke
from personal_agent.commands import evals as evals_commands
from personal_agent.config import DEFAULT_CONFIG_TOML
from personal_agent.evals import runner
from personal_agent.evals.checks import Verdict
from personal_agent.evals.runner import (
    LiveRun,
    ScenarioResult,
    StepSummary,
    TrialResult,
)
from personal_agent.evals.spec import load_catalog


def flat(output: str) -> str:
    return " ".join(output.split())


BUILTIN = [s.id for s in load_catalog()]

USER_SCENARIO = """\
id: my-own
title: My own scenario
tags: [mine]
steps:
  - id: tick
    timer: {name: daily, prompt: "Check."}
checks:
  - {kind: silent, step: tick, id: quiet}
mock:
  good:
    tick: []
  bad:
    - name: chatty
      fails: [quiet]
      script:
        tick:
          - send: hello
"""

UNSOUND = USER_SCENARIO.replace("id: my-own", "id: too-weak").replace(
    "          - send: hello", "          - say: fine"
)  # the "bad" run does nothing wrong, so the check could never fail


@pytest.fixture
def initialised(home):
    assert invoke("init").exit_code == 0
    return home


def user_folder(home: Path) -> Path:
    folder = home / "evals"
    folder.mkdir(exist_ok=True)
    return folder


def reports(home: Path, suffix: str) -> list[Path]:
    return sorted((home / "evals" / "reports").glob(f"*{suffix}"))


# ---- list and show ----------------------------------------------------------------------------------
def test_the_scenarios_there_are_are_listed_with_where_they_come_from(home):
    result = invoke("eval", "list")
    out = flat(result.output)
    assert result.exit_code == 0
    for scenario_id in BUILTIN:
        assert scenario_id in out
    assert out.count("(built-in)") == len(BUILTIN)
    assert "[always-on, honesty]" in out


def test_asking_for_nothing_in_particular_lists_them_too(home):
    assert flat(invoke("eval").output) == flat(invoke("eval", "list").output)


def test_the_owners_own_scenarios_are_listed_beside_the_built_in_ones(initialised):
    (user_folder(initialised) / "mine.yaml").write_text(USER_SCENARIO)
    out = flat(invoke("eval", "list").output)
    assert "my-own [mine] (evals/mine.yaml)" in out and "My own scenario" in out


def test_the_list_can_be_narrowed_by_tag(initialised):
    (user_folder(initialised) / "mine.yaml").write_text(USER_SCENARIO)
    out = flat(invoke("eval", "list", "--tag", "mine").output)
    assert "my-own" in out and BUILTIN[0] not in out
    expected = {s.id for s in load_catalog() if "honesty" in s.tags}
    shown = flat(invoke("eval", "list", "--tag", "honesty").output)
    assert all(i in shown for i in expected) and "my-own" not in shown
    assert "no scenarios" in invoke("eval", "list", "--tag", "nothing-has-this").output


def test_two_tags_select_what_has_either_of_them(initialised):
    found = list(load_catalog())
    honest = {s.id for s in found if "honesty" in s.tags}
    either = {s.id for s in found if {"honesty", "memory"} & set(s.tags)}
    assert honest < either  # the second tag brings scenarios of its own...
    assert not any({"honesty", "memory"} <= set(s.tags) for s in found)  # ...and none has both
    listed = flat(invoke("eval", "list", "--tag", "honesty", "--tag", "memory").output)
    assert all(i in listed for i in either) and "news-once" not in listed
    ran = invoke("eval", "run", "--tag", "honesty", "--tag", "memory", "--no-report")
    assert ran.exit_code == 0, ran.output
    assert {i for i in BUILTIN if i in ran.output} == either


def test_a_broken_file_is_named_and_the_others_still_listed(initialised):
    (user_folder(initialised) / "broken.yaml").write_text("id: x\n")
    result = invoke("eval", "list")
    assert result.exit_code == 0
    assert "skipped: broken.yaml:" in flat(result.output) and BUILTIN[0] in result.output


def test_a_scenario_is_shown_step_by_step_with_what_it_demands(home):
    out = flat(invoke("eval", "show", "preferences-are-followed").output)
    assert "preferences-are-followed" in out
    assert "starts with preferences: instructions.md" in out
    assert (
        "steps ask: the owner writes (in main): What is a good way to start learning to cook?"
        in out
    )
    assert "checks" in out and "says-ask: in `ask` the agent says something matching" in out
    assert (
        "memory_untouched: the owner's files stay exactly as written: preferences/instructions.md"
        in out
    )
    assert "A question in English must still get a Chinese answer" in out  # what it is about
    scenario = next(s for s in load_catalog() if s.id == "preferences-are-followed")
    assert scenario.mock is not None and scenario.mock.bad
    assert f"scripted: a good run and {len(scenario.mock.bad)} bad run(s)" in out


def test_a_scenario_that_starts_with_history_and_a_schedule_shows_both(home):
    out = flat(invoke("eval", "show", "quiet-when-nothing-new").output)
    assert "already told the owner:" in out
    assert "schedule" in out and "fires:" in out


def test_a_scenario_without_scripts_says_its_checks_are_unproven(initialised):
    bare = USER_SCENARIO.split("mock:")[0]
    (user_folder(initialised) / "bare.yaml").write_text(bare)
    out = flat(invoke("eval", "show", "my-own").output)
    assert "no scripts: its checks have not been shown to be able to fail" in out


def test_showing_a_scenario_that_does_not_exist_names_the_ones_that_do(home):
    result = invoke("eval", "show", "nope")
    out = flat(result.output)
    assert result.exit_code == 2
    assert "No scenario named 'nope'. Known:" in out and BUILTIN[0] in out


# ---- scripted runs -----------------------------------------------------------------------------------------
def test_a_scripted_run_proves_the_checks_without_a_key_and_says_so(initialised):
    result = invoke("eval", "run", "quiet-when-nothing-new", "--no-report")
    out = flat(result.output)
    assert result.exit_code == 0, result.output
    assert "ok quiet-when-nothing-new the good run passes, 3/3 bad runs caught" in out
    assert "Every scenario tells its good run from its bad ones." in out
    assert not reports(initialised, ".md")


def test_a_report_is_saved_in_the_home_unless_told_not_to(initialised):
    result = invoke("eval", "run", "quiet-when-nothing-new")
    assert result.exit_code == 0 and "Report:" in flat(result.output)
    [md] = reports(initialised, "-scripted.md")
    [js] = reports(initialised, "-scripted.json")
    assert md.name.removesuffix(".md") == js.name.removesuffix(".json")
    assert json.loads(js.read_text())["scenarios"][0]["id"] == "quiet-when-nothing-new"


def test_without_a_home_nothing_is_saved_and_the_owner_is_told(home):
    result = invoke("eval", "run", "quiet-when-nothing-new")
    assert result.exit_code == 0
    assert "No report saved: there is no home yet" in flat(result.output)
    assert not home.exists()


def test_a_report_that_cannot_be_written_is_said_and_does_not_hide_the_result(
    initialised, monkeypatch
):
    def refuse(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(evals_commands.reports, "save", refuse)
    result = invoke("eval", "run", "quiet-when-nothing-new")
    out = flat(result.output)
    assert result.exit_code == 0, result.output
    assert "No report saved: No space left on device" in out
    assert "Every scenario tells its good run from its bad ones." in out


def test_verbose_shows_every_check_and_every_bad_run(initialised):
    out = flat(invoke("eval", "run", "quiet-when-nothing-new", "--no-report", "-v").output)
    assert "ok ends-" in out and "caught reports-that-nothing-changed" in out
    assert "must fail" in out


def test_several_scenarios_run_in_the_order_asked_and_none_twice(initialised):
    result = invoke(
        "eval", "run", "news-once", "quiet-when-nothing-new", "news-once", "--no-report"
    )
    out = result.output
    assert out.count("news-once") == 1 and out.count("quiet-when-nothing-new") == 1
    assert out.index("news-once") < out.index("quiet-when-nothing-new")


def test_a_run_can_be_narrowed_by_tag(initialised):
    result = invoke("eval", "run", "--tag", "honesty", "--no-report")
    assert result.exit_code == 0
    shown = [i for i in BUILTIN if i in result.output]
    assert shown == [s.id for s in load_catalog() if "honesty" in s.tags]


def test_a_scenario_whose_bad_run_gets_away_fails_the_run_and_says_which(initialised):
    (user_folder(initialised) / "weak.yaml").write_text(UNSOUND)
    result = invoke("eval", "run", "too-weak")
    out = flat(result.output)
    assert result.exit_code == 1
    assert "FAIL too-weak the good run passes, 0/1 bad runs caught" in out
    assert "NOT caught chatty: must fail quiet; failed nothing" in out
    assert "Every scenario tells" not in out
    [js] = reports(initialised, "-scripted.json")  # a failing run is the one worth a report
    assert json.loads(js.read_text())["ok"] is False


def test_a_good_run_that_fails_is_shown_even_without_verbose(initialised):
    broken = USER_SCENARIO.replace("id: my-own", "id: noisy").replace(
        "    tick: []\n  bad:", "    tick:\n      - send: hi\n  bad:"
    )
    (user_folder(initialised) / "noisy.yaml").write_text(broken)
    result = invoke("eval", "run", "noisy", "--no-report")
    out = flat(result.output)
    assert result.exit_code == 1
    assert "the good run FAILS" in out and "no quiet: said 1 message(s)" in out
    assert "ok ends-tick" not in out  # what held is not listed unless asked for (-v)


def test_a_scenario_without_scripts_is_run_as_untested_and_does_not_fail(initialised):
    (user_folder(initialised) / "bare.yaml").write_text(USER_SCENARIO.split("mock:")[0])
    result = invoke("eval", "run", "my-own", "--no-report")
    assert result.exit_code == 0
    assert "my-own has no scripts, so its checks were not tested" in flat(result.output)


def test_a_broken_file_is_reported_and_the_rest_still_runs(initialised):
    (user_folder(initialised) / "broken.yaml").write_text("id: x\n")
    result = invoke("eval", "run", "quiet-when-nothing-new", "--no-report")
    assert result.exit_code == 0
    assert "skipped: broken.yaml:" in flat(result.output)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["nope"], "No scenario named 'nope'. Known:"),
        (["--tag", "nothing-has-this"], "No scenario matches."),
        (["news-once", "--tag", "memory"], "No scenario matches."),
        (["--effort", "enormous"], "--effort is one of"),
    ],
)
def test_a_request_that_makes_no_sense_is_refused_before_anything_runs(initialised, args, message):
    result = invoke("eval", "run", *args, "--no-report")
    assert result.exit_code == 2 and message in flat(result.output)


@pytest.mark.parametrize("args", [["--trials", "0"], ["--trials", "11"], ["--max-cost", "0"]])
def test_limits_that_are_out_of_range_are_refused(initialised, args):
    assert invoke("eval", "run", *args).exit_code == 2


# ---- live, with no model to talk to ------------------------------------------------------------------------
class Spy:
    """Stands in for `run_live`: records what it was asked and reports a canned outcome."""

    def __init__(self, results: list[ScenarioResult], *, cost: float = 0.1234):
        self.results = results
        self.cost = cost
        self.calls: list[tuple[list, dict]] = []

    async def __call__(self, scenarios, **kw):
        self.calls.append((list(scenarios), kw))
        for result in self.results:
            for t in result.trials:
                kw["on_trial"](next(s for s in scenarios if s.id == result.scenario_id), t)
        return LiveRun(
            self.results,
            cost_usd=self.cost,
            cap_usd=kw["cap_usd"],
            trials=kw["trials"],
            model=kw["model"] or "from-config",
            effort=kw["effort"] or "from-config",
        )


def good_trial(index=0, *, scenario_id="quiet-when-nothing-new", cost=0.05, quiet=1, noise=0):
    return TrialResult(
        scenario_id,
        index,
        verdicts=[Verdict("ends-tick", True, "ok")],
        steps=[StepSummary("tick", [], [], ["silent"])],
        cost_usd=cost,
        quiet_steps=quiet,
        noise=noise,
    )


def bad_trial(
    index=0, *, scenario_id="quiet-when-nothing-new", error=None, said="said 1 message(s): 'hi'"
):
    return TrialResult(
        scenario_id,
        index,
        verdicts=[Verdict("silent-tick", False, said)],
        steps=[StepSummary("tick", [("normal", "hi")], ["send_message"], ["completed"])],
        cost_usd=0.07,
        quiet_steps=1,
        noise=1,
        error=error,
    )


@pytest.fixture
def with_key(initialised, monkeypatch):
    """A home with a (fake) key in its own `.env`, as `pa init` leaves it for the owner to fill in."""
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")  # so that what load_env sets is undone
    monkeypatch.delenv("OPENAI_API_KEY")
    (initialised / ".env").write_text("OPENAI_API_KEY=" + "sk-" + "test-" + "k" * 30 + "\n")
    return initialised


def test_live_needs_a_home_and_spends_nothing_without_one(home, monkeypatch):
    spy = Spy([])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "--live")
    assert result.exit_code == 2 and "Run `pa init` first" in flat(result.output)
    assert spy.calls == []


def test_live_needs_a_key_and_says_where_to_put_it(initialised, monkeypatch):
    spy = Spy([])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "--live")
    out = flat(result.output)
    assert result.exit_code == 2
    assert "OPENAI_API_KEY is not set" in out and "nothing was spent" in out
    assert str(initialised / ".env") in result.output.replace("\n", "")
    assert spy.calls == []


def test_a_custom_endpoint_needs_no_openai_key(initialised, monkeypatch):
    (initialised / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.replace("[model]", '[model]\nbase_url = "http://127.0.0.1:9/v1"', 1)
    )
    spy = Spy([ScenarioResult("quiet-when-nothing-new", "Quiet", [good_trial()])])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "quiet-when-nothing-new", "--live", "--no-report")
    assert result.exit_code == 0, result.output
    assert len(spy.calls) == 1


def test_the_base_url_variable_stands_for_a_custom_endpoint_too(initialised, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    spy = Spy([ScenarioResult("quiet-when-nothing-new", "Quiet", [good_trial()])])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "quiet-when-nothing-new", "--live", "--no-report")
    assert result.exit_code == 0, result.output
    assert len(spy.calls) == 1


def test_live_preflight_accepts_the_selected_model_key(initialised, monkeypatch):
    (initialised / "config.toml").write_text('[model]\napi_key_env = "PA_MODEL_AUTH"\n')
    monkeypatch.setenv("PA_MODEL_AUTH", "custom-credential-value")
    spy = Spy([ScenarioResult("quiet-when-nothing-new", "Quiet", [good_trial()])])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "quiet-when-nothing-new", "--live", "--no-report")
    assert result.exit_code == 0, result.output
    assert len(spy.calls) == 1
    assert "custom-credential-value" not in result.output


def test_live_preflight_refuses_a_missing_selected_key_even_with_a_custom_url(
    initialised, monkeypatch
):
    (initialised / "config.toml").write_text(
        '[model]\napi_key_env = "PA_MODEL_AUTH"\nbase_url = "http://127.0.0.1:9/v1"\n'
    )
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-provider-credential")
    spy = Spy([])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "quiet-when-nothing-new", "--live", "--no-report")
    assert result.exit_code == 2
    assert "PA_MODEL_AUTH is not set" in flat(result.output)
    assert spy.calls == []


def test_a_config_that_cannot_be_read_stops_a_live_run_before_it_starts(initialised, monkeypatch):
    spy = Spy([])
    monkeypatch.setattr(runner, "run_live", spy)
    (initialised / "config.toml").write_text("[budget]\nper_run_usd = -3\n")
    result = invoke("eval", "run", "--live")
    assert result.exit_code == 2 and spy.calls == []


def test_a_live_run_passes_the_owners_choices_on_and_reports_what_it_cost(with_key, monkeypatch):
    spy = Spy([ScenarioResult("quiet-when-nothing-new", "Quiet", [good_trial(0), good_trial(1)])])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke(
        "eval", "run", "quiet-when-nothing-new", "--live", "--trials", "2", "--max-cost", "0.5",
        "--model", "some-model", "--effort", "high",
    )  # fmt: skip
    out = flat(result.output)
    assert result.exit_code == 0, result.output
    [(chosen, kw)] = spy.calls
    assert [s.id for s in chosen] == ["quiet-when-nothing-new"]
    assert (kw["trials"], kw["cap_usd"], kw["model"], kw["effort"]) == (
        2,
        0.5,
        "some-model",
        "high",
    )
    assert kw["owner"].model.main  # the owner's configuration, for everything else
    assert (
        "Live: 1 scenario(s) x 2 trial(s) on some-model (high); spending is capped at $0.50." in out
    )
    assert "quiet-when-nothing-new trial 1/2 pass $0.0500" in out
    assert "quiet-when-nothing-new trial 2/2 pass $0.0500" in out
    assert "ok quiet-when-nothing-new 2/2 trials passed" in out
    assert "Spent $0.1234 of the $0.50 ceiling." in out
    assert "Noise: 0 message(s) in 2 step(s) that asked for silence (0.0 per quiet step)." in out
    [js] = reports(with_key, "-live.json")
    assert json.loads(js.read_text())["model"] == "some-model"


def test_a_live_run_with_the_owners_defaults_names_them(with_key, monkeypatch):
    spy = Spy([ScenarioResult("quiet-when-nothing-new", "Quiet", [good_trial()])])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "quiet-when-nothing-new", "--live", "--no-report")
    out = flat(result.output)
    assert result.exit_code == 0
    assert "Live: 1 scenario(s) x 1 trial(s) on gpt-" in out
    assert "spending is capped at $1.00." in out
    kw = spy.calls[0][1]
    assert kw["model"] is None and kw["effort"] is None and kw["cap_usd"] == 1.0


def test_a_live_failure_exits_with_one_and_shows_the_first_failed_trial_only(with_key, monkeypatch):
    spy = Spy(
        [
            ScenarioResult(
                "quiet-when-nothing-new",
                "Quiet",
                [good_trial(0), bad_trial(1), bad_trial(2, said="said 1 message(s): 'later'")],
            )
        ]
    )
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "quiet-when-nothing-new", "--live", "--trials", "3")
    out = flat(result.output)
    assert result.exit_code == 1
    assert "FAIL quiet-when-nothing-new 1/3 trials passed" in out
    assert out.count("no silent-tick: said 1 message(s): 'hi'") == 1
    assert "'later'" not in out  # the second failed trial is in the report, not on the screen
    assert "Noise: 2 message(s) in 3 step(s) that asked for silence (0.667 per quiet step)." in out
    assert "trial 2/3 FAIL $0.0700" in out
    [md] = reports(with_key, "-live.md")
    assert "something did not hold" in md.read_text()


def test_a_trial_that_could_not_be_played_is_shown_with_its_reason(with_key, monkeypatch):
    spy = Spy(
        [
            ScenarioResult(
                "quiet-when-nothing-new",
                "Quiet",
                [TrialResult("quiet-when-nothing-new", 0, error="step 'ask': bad message")],
            )
        ]
    )
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "quiet-when-nothing-new", "--live", "--no-report")
    assert result.exit_code == 1 and "step 'ask': bad message" in flat(result.output)


def test_scenarios_left_unplayed_because_the_money_ran_out_are_said_to_be_failures(
    with_key, monkeypatch
):
    spy = Spy(
        [
            ScenarioResult("quiet-when-nothing-new", "Quiet", [good_trial()], skipped=2),
            ScenarioResult("news-once", "News", [], skipped=3),
        ],
        cost=1.02,
    )
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "quiet-when-nothing-new", "news-once", "--live", "--no-report")
    out = flat(result.output)
    assert result.exit_code == 1
    assert (
        "FAIL quiet-when-nothing-new 1/1 trials passed (2 not run: spending ceiling reached)" in out
    )
    assert "FAIL news-once 0/0 trials passed (3 not run: spending ceiling reached)" in out


def test_no_noise_line_when_no_scenario_asked_for_silence(with_key, monkeypatch):
    spy = Spy([ScenarioResult("news-once", "News", [good_trial(scenario_id="news-once", quiet=0)])])
    monkeypatch.setattr(runner, "run_live", spy)
    result = invoke("eval", "run", "news-once", "--live", "--no-report")
    assert result.exit_code == 0 and "Noise:" not in result.output
