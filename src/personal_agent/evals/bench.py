"""Playing a scenario: a throwaway home, the agent built the way it always is, one event after another.

Nothing here touches the owner's real agent. Each trial gets its own home folder and database, a
configuration made from the owner's model and budget settings (and nothing else, so their MCP
servers, channels and quiet hours cannot change what is measured), and the agent's own `send_message`
and memory tools. What the agent said is read from the outbox, the way a channel would read it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from personal_agent.app import App, build_app
from personal_agent.channels.ingress import Ingress
from personal_agent.config import Config
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.events import Kind, NewEvent, Trust
from personal_agent.core.outbox import OutMessage
from personal_agent.core.runs import RunRecord
from personal_agent.core.worker import Worker, WorkResult
from personal_agent.evals.spec import Scenario, Seed, Step
from personal_agent.paths import Paths
from personal_agent.security.sandbox import SandboxProvider

KEPT_SECTIONS = ("agent", "model", "budget", "memory")
"""The owner's settings that decide how the agent thinks. Everything else is left at its default."""


class BenchError(Exception):
    """The scenario could not be played (as opposed to: the agent did not behave)."""


class Collector:
    """A channel that remembers what it was asked to deliver."""

    name = "cli"

    def __init__(self) -> None:
        self.sent: list[OutMessage] = []

    async def send(self, message: OutMessage) -> None:
        self.sent.append(message)


@dataclass
class StepResult:
    id: str
    messages: list[OutMessage] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.messages]


@dataclass
class Trial:
    scenario: Scenario
    steps: list[StepResult]
    home: Path
    """The throwaway home; its memory is read by the checks, so it must outlive `play`."""
    cost_usd: float = 0.0
    requests: int = 0

    @property
    def seeded(self) -> dict[str, str]:
        return self.scenario.seed.memory_files()

    def step(self, step_id: str) -> StepResult:
        return next(s for s in self.steps if s.id == step_id)


def eval_config(
    owner: Config | None,
    scenario: Scenario,
    *,
    model: str | None = None,
    effort: str | None = None,
    cap_usd: float | None = None,
) -> Config:
    """The configuration a trial runs with.

    The owner's model and budget settings, then what the scenario says, then the evaluation's own
    rules: messages go to one collector with no quiet hours or hourly limit (whether to speak is
    what is measured, not the delivery policy, which has its own tests), and `cap_usd` bounds what
    one trial may spend, per run and per day."""
    sections: dict[str, dict[str, Any]] = {}
    if owner is not None:
        for name in KEPT_SECTIONS:
            sections[name] = getattr(owner, name).model_dump(mode="json")
    for name, values in scenario.seed.config.items():
        sections[name] = {**sections.get(name, {}), **values}
    sections["notify"] = {"channel": "cli", "quiet_hours": None, "max_per_hour": 1000}
    if model:
        sections["model"] = {**sections.get("model", {}), "main": model}
    if effort:
        sections["model"] = {**sections.get("model", {}), "reasoning_effort": effort}
    if cap_usd is not None:
        budget, defaults = sections.get("budget", {}), Config().budget
        sections["budget"] = {
            "per_run_usd": min(budget.get("per_run_usd", defaults.per_run_usd), cap_usd),
            "daily_usd": min(budget.get("daily_usd", defaults.daily_usd), cap_usd),
        }
    try:
        return Config.model_validate(sections)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise BenchError(
            f"{scenario.id}: the config in the seed is not valid ({where}: {first['msg']})"
        ) from exc


def _write_seed(paths: Paths, seed: Seed) -> None:
    targets = {paths.memory_dir / rel: text for rel, text in seed.memory_files().items()}
    targets.update({paths.workspace_dir / rel: text for rel, text in seed.files.items()})
    for file, text in targets.items():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        file.chmod(0o600)


async def _deliver(app: App) -> list[OutMessage]:
    """Hand every message that is due to a collector, as the dispatcher would to a channel."""
    sink = Collector()
    for _ in range(10):
        report = await Dispatcher(app, {"cli": sink}).deliver_once()
        if not report.sent:
            break
    return sink.sent


async def _drain(app: App, session_key: str, limit: int = 20) -> list[WorkResult]:
    worker = Worker(app)
    results: list[WorkResult] = []
    for _ in range(limit):
        result = await worker.run_once(session_key=session_key, on_failure="drop")
        if result is None:
            return results
        results.append(result)
    raise BenchError(f"the agent never went idle in {session_key!r}")


def _event(step: Step) -> NewEvent:
    if step.timer is not None:
        return NewEvent(
            source="timer",
            kind=Kind.TIMER,
            session_key=step.session_key,
            payload={"name": step.timer.name, "prompt": step.timer.prompt},
            trust=Trust.OWNER,
        )
    assert step.webhook is not None
    return NewEvent(
        source=f"webhook:{step.webhook.name}",
        kind=Kind.WEBHOOK,
        session_key=step.session_key,
        payload={
            "name": step.webhook.name,
            "event": step.webhook.event,
            "body": step.webhook.body,
            "instructions": step.webhook.instructions,
        },
        trust=Trust.UNTRUSTED,
    )


LOOKBACK = 200
"""How many of a conversation's latest runs to look through (a step makes a handful)."""


def _runs_of(app: App, session_key: str) -> list[RunRecord]:
    """The conversation's runs, oldest first. Read from the run log, not from what the worker
    returned: a run that failed or ran out of budget is in the log, and a worker that raised
    returns no outcome for it."""
    return list(reversed(app.runs.recent(LOOKBACK, session_key=session_key)))


async def _play_step(app: App, step: Step) -> StepResult:
    earlier = {run.id for run in _runs_of(app, step.session_key)}
    if step.kind == "message":
        accepted = await Ingress(app, session_key=step.session_key).owner_message(
            channel="cli", text=step.message or ""
        )
        if accepted.status != "queued":
            raise BenchError(
                f"step {step.id!r}: the message was not taken as a message "
                f"({accepted.status}: {accepted.reply or 'no reason given'})"
            )
    else:
        app.inbox.add(_event(step))
    results = await _drain(app, step.session_key)
    messages = await _deliver(app)
    runs = [run for run in _runs_of(app, step.session_key) if run.id not in earlier]
    tools = [row.tool for run in runs for row in reversed(app.audit.recent(200, run_id=run.id))]
    failures = [r.failure.summary for r in results if r.failure is not None]
    return StepResult(step.id, messages, runs, tools, failures)


async def play(
    scenario: Scenario,
    *,
    home: Path,
    config: Config,
    model_factory: Any = None,
    sandbox: SandboxProvider | None = None,
) -> Trial:
    """Run every step of `scenario` in the (empty) folder `home`, and say what happened."""
    paths = Paths(home=home)
    paths.ensure()
    _write_seed(paths, scenario.seed)
    app = build_app(
        home=home,
        config=config,
        model_factory=model_factory,
        sandbox=sandbox,
        read_env=False,
    )
    try:
        for text in scenario.seed.sent:
            app.outbox.enqueue(session_key="main", channel="cli", text=text)
        await _deliver(app)  # delivered, so they are in the owner's history as after a real day
        steps = [await _play_step(app, step) for step in scenario.steps]
    finally:
        await app.stop_services()
        app.close()
    runs = [run for s in steps for run in s.runs]
    return Trial(
        scenario,
        steps,
        home,
        cost_usd=sum(r.cost_usd for r in runs),
        requests=sum(r.requests for r in runs),
    )
