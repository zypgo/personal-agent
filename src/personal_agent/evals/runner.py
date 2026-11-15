"""Running scenarios.

Two ways, for two questions.

*Scripted* asks: can these checks tell a good run from a bad one? A scripted model plays the good
run and each named bad run; the good one must pass every check, and each bad one must fail the
checks it is meant to. It needs no key and costs nothing, and it is what keeps a scenario honest.

*Live* asks: does the real model behave? Each scenario is played several times (a model is not
deterministic), against a throwaway home, with a ceiling on what the whole run may spend.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.testing import ScriptedModel, assistant_message, function_call

from personal_agent.config import Config
from personal_agent.evals.bench import BenchError, Trial, eval_config, play
from personal_agent.evals.checks import Verdict, judge
from personal_agent.evals.spec import Scenario, Script, Silent
from personal_agent.harness.models import ModelFactory
from personal_agent.security.sandbox import SandboxProvider

MIN_BUDGET_USD = 0.0005
"""Below this, nothing is left to start another trial with."""


@dataclass
class StepSummary:
    id: str
    messages: list[tuple[str, str]]
    """(urgency, text) of each message the agent sent in the step."""
    tools: list[str]
    statuses: list[str]


@dataclass
class TrialResult:
    scenario_id: str
    index: int
    verdicts: list[Verdict] = field(default_factory=list)
    steps: list[StepSummary] = field(default_factory=list)
    cost_usd: float = 0.0
    requests: int = 0
    quiet_steps: int = 0
    """Steps that ask the agent to stay silent."""
    noise: int = 0
    """Messages it sent in those steps anyway."""
    error: str | None = None
    """The trial could not be played at all (not the same as a check that failed)."""

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.verdicts) and all(v.ok for v in self.verdicts)

    @property
    def failed(self) -> list[Verdict]:
        return [v for v in self.verdicts if not v.ok]


@dataclass
class ScenarioResult:
    scenario_id: str
    title: str
    trials: list[TrialResult] = field(default_factory=list)
    skipped: int = 0
    """Trials not run because the spending ceiling was reached."""

    @property
    def passed(self) -> int:
        return sum(t.ok for t in self.trials)

    @property
    def all_passed(self) -> bool:
        """Every trial passed and none was skipped (what an always-on agent needs: not luck)."""
        return bool(self.trials) and self.passed == len(self.trials) and not self.skipped


@dataclass
class LiveRun:
    results: list[ScenarioResult]
    cost_usd: float
    cap_usd: float
    trials: int
    model: str
    effort: str

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.all_passed for r in self.results)


@dataclass
class BadResult:
    name: str
    expected: list[str]
    failed: list[str]
    error: str | None = None

    @property
    def caught(self) -> bool:
        return self.error is None and set(self.expected) <= set(self.failed)


@dataclass
class CheckedScenario:
    scenario_id: str
    title: str
    good: TrialResult | None = None
    bad: list[BadResult] = field(default_factory=list)
    unscripted: bool = False

    @property
    def ok(self) -> bool:
        if self.unscripted:
            return True
        return self.good is not None and self.good.ok and all(b.caught for b in self.bad)


# ---- scripts -------------------------------------------------------------------------------------
def turns(scenario: Scenario, script: Script) -> list[list[Any]]:
    """What the scripted model says, in order: per step one round of tool calls (if any), then the
    closing words."""
    out: list[list[Any]] = []
    counter = 0
    for step in scenario.steps:
        actions = script.get(step.id, [])
        calls: list[Any] = []
        for action in actions:
            counter += 1
            call_id = f"scripted-{counter}"
            if action.send is not None:
                args = {"text": action.send, "urgency": action.urgency}
                calls.append(function_call("send_message", args, call_id=call_id))
            elif action.memory is not None:
                calls.append(function_call("memory", action.memory, call_id=call_id))
            elif action.tool is not None:
                calls.append(function_call(action.tool.name, action.tool.args, call_id=call_id))
        if calls:
            out.append(calls)
        closing = next((a.say for a in actions if a.say is not None), "Done.")
        out.append([assistant_message(closing)])
    return out


def _summarise(scenario: Scenario, trial: Trial, index: int) -> TrialResult:
    quiet = {c.step for c in scenario.checks if isinstance(c, Silent)}
    return TrialResult(
        scenario_id=scenario.id,
        index=index,
        verdicts=judge(trial),
        steps=[
            StepSummary(
                id=s.id,
                messages=[(m.urgency, m.text) for m in s.messages],
                tools=list(s.tools),
                statuses=[r.status for r in s.runs],
            )
            for s in trial.steps
        ],
        cost_usd=trial.cost_usd,
        requests=trial.requests,
        quiet_steps=len(quiet),
        noise=sum(len(s.messages) for s in trial.steps if s.id in quiet),
    )


async def play_scripted(scenario: Scenario, script: Script) -> TrialResult:
    steps = turns(scenario, script)
    model = ScriptedModel(steps)
    try:
        with tempfile.TemporaryDirectory(prefix="pa-eval-") as folder:
            trial = await play(
                scenario,
                home=Path(folder) / "home",
                config=eval_config(None, scenario),
                model_factory=lambda _name: model,
                sandbox=SandboxProvider.fixed(None, "scripted runs need no shell"),
            )
            result = _summarise(scenario, trial, 0)
    except BenchError as exc:
        return TrialResult(scenario.id, 0, error=str(exc))
    if len(model.calls) != len(steps):
        result.error = (
            f"the script has {len(steps)} model turn(s) but the agent took {len(model.calls)}: "
            "a step must make the agent do exactly what its actions say"
        )
    return result


async def check_scenario(scenario: Scenario) -> CheckedScenario:
    """Does the scenario tell its good run from each of its bad ones?"""
    checked = CheckedScenario(scenario.id, scenario.title)
    if scenario.mock is None:
        checked.unscripted = True
        return checked
    checked.good = await play_scripted(scenario, scenario.mock.good)
    for bad in scenario.mock.bad:
        played = await play_scripted(scenario, bad.script)
        checked.bad.append(
            BadResult(
                bad.name,
                list(bad.fails),
                [v.id for v in played.failed],
                error=played.error if played.error and not played.failed else None,
            )
        )
    return checked


# ---- live --------------------------------------------------------------------------------------------
async def play_live(
    scenario: Scenario,
    index: int,
    *,
    owner: Config,
    model: str | None,
    effort: str | None,
    cap_usd: float,
    model_factory: ModelFactory | None = None,
    sandbox: SandboxProvider | None = None,
) -> TrialResult:
    try:
        config = eval_config(owner, scenario, model=model, effort=effort, cap_usd=cap_usd)
        with tempfile.TemporaryDirectory(prefix="pa-eval-") as folder:
            trial = await play(
                scenario,
                home=Path(folder) / "home",
                config=config,
                model_factory=model_factory,
                sandbox=sandbox,
            )
            return _summarise(scenario, trial, index)
    except BenchError as exc:
        return TrialResult(scenario.id, index, error=str(exc))


async def run_live(
    scenarios: Sequence[Scenario],
    *,
    owner: Config,
    trials: int,
    cap_usd: float,
    model: str | None = None,
    effort: str | None = None,
    on_trial: Callable[[Scenario, TrialResult], None] | None = None,
    model_factory: ModelFactory | None = None,
    sandbox: SandboxProvider | None = None,
) -> LiveRun:
    """Play each scenario `trials` times with the real model, stopping when `cap_usd` is spent."""
    spent = 0.0
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        outcome = ScenarioResult(scenario.id, scenario.title)
        for index in range(trials):
            remaining = cap_usd - spent
            if remaining < MIN_BUDGET_USD:
                outcome.skipped = trials - index
                break
            played = await play_live(
                scenario,
                index,
                owner=owner,
                model=model,
                effort=effort,
                cap_usd=remaining,
                model_factory=model_factory,
                sandbox=sandbox,
            )
            spent += played.cost_usd
            outcome.trials.append(played)
            if on_trial:
                on_trial(scenario, played)
        results.append(outcome)
    return LiveRun(
        results,
        cost_usd=spent,
        cap_usd=cap_usd,
        trials=trials,
        model=model or owner.model.main,
        effort=effort or owner.model.reasoning_effort,
    )
