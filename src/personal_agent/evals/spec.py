"""The scenario file format.

A scenario is data, not code: what the agent already knows (`seed`), what happens to it (`steps`),
and what has to be true afterwards (`checks`). Unknown keys are errors, so a typo does not turn a
check into one that is silently skipped.

`mock` holds scripts for a scripted model: one good run, and runs that go wrong in a named way. They
are what lets `pa eval` test the checks themselves: a check that no run can fail proves nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from personal_agent.parsing import load_yaml
from personal_agent.paths import Paths
from personal_agent.validation import problems

MAX_FILE_BYTES = 256 * 1024
"""A scenario is a page of YAML. Anything much larger is not one."""

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_TIMER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
RUN_ENDS = ("completed", "silent", "failed", "cancelled", "paused_budget")


class ScenarioError(Exception):
    """A scenario file that cannot be used; the message says where and why."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _relative(path: str) -> str:
    """A path inside a scenario's own folder: relative, no `..`, no backslashes."""
    pure = PurePosixPath(path)
    if (
        not path.strip()
        or "\\" in path
        or "\x00" in path
        or pure.is_absolute()
        or ".." in pure.parts
        or len(path) > 200
    ):
        raise ValueError(f"{path!r} is not a plain relative path")
    return str(pure)


def _regex(pattern: str | None) -> str | None:
    if pattern is None:
        return None
    if len(pattern) > 300:
        raise ValueError("a pattern may be at most 300 characters")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{pattern!r} is not a regular expression ({exc})") from exc
    return pattern


# ---- what the agent already has --------------------------------------------------------------
class Seed(_Model):
    preferences: dict[str, str] = Field(default_factory=dict)
    """Files the owner wrote (`memory/preferences`): the agent reads them and may not change them."""
    memory: dict[str, str] = Field(default_factory=dict)
    """Files the agent keeps (`memory/state`)."""
    tasks: dict[str, str] = Field(default_factory=dict)
    """Progress files of jobs under way (`memory/state/tasks/<name>.md`)."""
    files: dict[str, str] = Field(default_factory=dict)
    """Files in the agent's workspace."""
    sent: list[str] = Field(default_factory=list)
    """Messages the owner has already been sent (delivered), oldest first."""
    config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Overrides of config sections (`budget`, `agent`, ...), as in config.toml."""

    @field_validator("preferences", "memory", "files")
    @classmethod
    def _plain_paths(cls, value: dict[str, str]) -> dict[str, str]:
        return {_relative(name): text for name, text in value.items()}

    @field_validator("tasks")
    @classmethod
    def _task_files(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if "/" in name or not name.endswith(".md") or name.startswith("."):
                raise ValueError(f"{name!r} must be a plain file name ending in .md")
        return value

    def memory_files(self) -> dict[str, str]:
        """Everything seeded under the memory folder, by its path relative to it."""
        files = {f"preferences/{name}": text for name, text in self.preferences.items()}
        files.update({f"state/{name}": text for name, text in self.memory.items()})
        files.update({f"state/tasks/{name}": text for name, text in self.tasks.items()})
        return files


# ---- what happens ----------------------------------------------------------------------------
class TimerEvent(_Model):
    name: str
    prompt: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _TIMER_NAME.match(value):
            raise ValueError("a schedule's name is lowercase letters, digits, dots, dashes")
        return value


class WebhookEvent(_Model):
    name: str
    event: str | None = None
    body: Any = None
    instructions: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _TIMER_NAME.match(value):
            raise ValueError("a webhook's name is lowercase letters, digits, dots, dashes")
        return value


class Step(_Model):
    id: str
    message: str | None = None
    """The owner writes this."""
    timer: TimerEvent | None = None
    """A schedule fires."""
    webhook: WebhookEvent | None = None
    """An outside system calls."""
    session: str | None = None
    """For messages: the conversation (default `main`)."""
    ends: list[str] = Field(default_factory=lambda: ["completed", "silent"])
    """How the run may end. Anything else fails the scenario: a run that crashed said nothing, and
    must not be mistaken for a run that chose to stay quiet."""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("an id is lowercase letters, digits, dashes, underscores")
        return value

    @field_validator("ends")
    @classmethod
    def _ends(cls, value: list[str]) -> list[str]:
        unknown = [v for v in value if v not in RUN_ENDS]
        if unknown or not value:
            raise ValueError(f"`ends` takes one or more of {', '.join(RUN_ENDS)}")
        return value

    @model_validator(mode="after")
    def _one_event(self) -> Step:
        given = [k for k in ("message", "timer", "webhook") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError("a step has exactly one of `message`, `timer`, `webhook`")
        if self.session is not None and self.message is None:
            raise ValueError("`session` only applies to a `message` step")
        if self.session is not None and not _TIMER_NAME.match(self.session):
            raise ValueError("`session` is lowercase letters, digits, dots, dashes")
        return self

    @property
    def kind(self) -> Literal["message", "timer", "webhook"]:
        if self.message is not None:
            return "message"
        return "timer" if self.timer is not None else "webhook"

    @property
    def session_key(self) -> str:
        if self.timer is not None:
            return f"timer:{self.timer.name}"
        if self.webhook is not None:
            return f"webhook:{self.webhook.name}"
        return self.session or "main"


# ---- what has to be true afterwards -------------------------------------------------------------
class _Check(_Model):
    id: str | None = None
    """How reports and mock scripts name this check; defaults to its kind (and step)."""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        if value is not None and not _NAME.match(value):
            raise ValueError("an id is lowercase letters, digits, dashes, underscores")
        return value


class Silent(_Check):
    kind: Literal["silent"]
    step: str


class Says(_Check):
    kind: Literal["says"]
    step: str | None = None
    any_of: list[str] = Field(default_factory=list)
    all_of: list[str] = Field(default_factory=list)
    pattern: str | None = None

    @field_validator("pattern")
    @classmethod
    def _valid_pattern(cls, value: str | None) -> str | None:
        return _regex(value)

    @model_validator(mode="after")
    def _something_to_look_for(self) -> Says:
        if not (self.any_of or self.all_of or self.pattern):
            raise ValueError("`says` needs `any_of`, `all_of` or `pattern`")
        return self


class NeverSays(_Check):
    kind: Literal["never_says"]
    step: str | None = None
    any_of: list[str] = Field(default_factory=list)
    pattern: str | None = None

    @field_validator("pattern")
    @classmethod
    def _valid_pattern(cls, value: str | None) -> str | None:
        return _regex(value)

    @model_validator(mode="after")
    def _something_to_look_for(self) -> NeverSays:
        if not (self.any_of or self.pattern):
            raise ValueError("`never_says` needs `any_of` or `pattern`")
        return self


class MaxMessages(_Check):
    kind: Literal["max_messages"]
    step: str | None = None
    limit: int = Field(ge=0)


class NotUrgent(_Check):
    kind: Literal["not_urgent"]
    step: str | None = None


class ToolsUsed(_Check):
    kind: Literal["tools_used"]
    step: str | None = None
    tools: list[str] = Field(min_length=1)


class ToolsNotUsed(_Check):
    kind: Literal["tools_not_used"]
    step: str | None = None
    tools: list[str] = Field(min_length=1)


class OnlyTools(_Check):
    kind: Literal["only_tools"]
    step: str | None = None
    allowed: list[str]


class Profile(_Check):
    kind: Literal["profile"]
    step: str
    expect: str


class MemoryHas(_Check):
    kind: Literal["memory_has"]
    path: str
    """A glob, relative to the memory folder (`state/tasks/*.md`)."""
    contains: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _plain_path(cls, value: str) -> str:
        return _relative(value)


class MemoryUntouched(_Check):
    kind: Literal["memory_untouched"]
    paths: list[str] = Field(min_length=1)

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        return [_relative(p) for p in value]


class SpendUnder(_Check):
    kind: Literal["spend_under"]
    usd: float = Field(gt=0)


Check = Annotated[
    Silent
    | Says
    | NeverSays
    | MaxMessages
    | NotUrgent
    | ToolsUsed
    | ToolsNotUsed
    | OnlyTools
    | Profile
    | MemoryHas
    | MemoryUntouched
    | SpendUnder,
    Field(discriminator="kind"),
]


# ---- scripts for the scripted model -----------------------------------------------------------------
class ToolCall(_Model):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Action(_Model):
    """One thing the scripted model does in a step. A step's actions are one round of tool calls,
    then the model's closing words (`say`, or "Done.")."""

    send: str | None = None
    urgency: Literal["urgent", "normal", "low"] = "normal"
    memory: dict[str, Any] | None = None
    tool: ToolCall | None = None
    say: str | None = None

    @model_validator(mode="after")
    def _one_thing(self) -> Action:
        given = [k for k in ("send", "memory", "tool", "say") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError("an action is exactly one of `send`, `memory`, `tool`, `say`")
        return self


Script = dict[str, list[Action]]


class BadRun(_Model):
    name: str
    fails: list[str] = Field(min_length=1)
    """The checks this run must be caught by."""
    script: Script


class Mock(_Model):
    good: Script = Field(default_factory=dict)
    bad: list[BadRun] = Field(default_factory=list)


# ---- the scenario -------------------------------------------------------------------------------------
class Scenario(_Model):
    id: str
    title: str
    about: str = ""
    tags: list[str] = Field(default_factory=list)
    seed: Seed = Field(default_factory=Seed)
    steps: list[Step] = Field(min_length=1)
    checks: list[Check] = Field(min_length=1)
    mock: Mock | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("an id is lowercase letters, digits, dashes, underscores")
        return value

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        if any(not _NAME.match(t) for t in value):
            raise ValueError("a tag is lowercase letters, digits, dashes, underscores")
        return value

    def labelled_checks(self) -> list[tuple[str, Check]]:
        """The checks with the names reports and scripts use: the id a check was given, or its
        kind (and step) made unique with a counter. A given id is never changed."""
        taken = {c.id for c in self.checks if c.id}
        out: list[tuple[str, Check]] = []
        for check in self.checks:
            if check.id:
                out.append((check.id, check))
                continue
            step = getattr(check, "step", None)
            base = f"{check.kind}-{step}" if step else check.kind
            name, count = base, 1
            while name in taken:
                count += 1
                name = f"{base}-{count}"
            taken.add(name)
            out.append((name, check))
        return out

    def check_ids(self) -> list[str]:
        """Every name a check can fail under: the written ones, and for each step the implicit one
        that its run ended as it may."""
        return [f"ends-{s.id}" for s in self.steps] + [name for name, _ in self.labelled_checks()]

    def step(self, step_id: str) -> Step:
        return next(s for s in self.steps if s.id == step_id)

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        """The parts must fit each other: what refers to a step, a file or a check names one that
        exists, and nothing is named twice."""
        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique")
        self._checks_name_real_steps(step_ids)
        self._check_ids_are_distinct()
        self._untouched_files_are_seeded()
        if self.mock is not None:
            self._mock_fits(self.mock, step_ids)
        return self

    def _checks_name_real_steps(self, step_ids: list[str]) -> None:
        for _, check in self.labelled_checks():
            step = getattr(check, "step", None)
            if step is not None and step not in step_ids:
                raise ValueError(f"a check refers to the step {step!r}, which does not exist")

    def _check_ids_are_distinct(self) -> None:
        given = [c.id for c in self.checks if c.id]
        if len(set(given)) != len(given):
            raise ValueError("check ids must be unique")
        if set(given) & {f"ends-{s.id}" for s in self.steps}:
            raise ValueError("an id may not be one of the implicit `ends-<step>` names")

    def _untouched_files_are_seeded(self) -> None:
        known = set(self.seed.memory_files())
        for _, check in self.labelled_checks():
            if isinstance(check, MemoryUntouched):
                missing = [p for p in check.paths if p not in known]
                if missing:
                    raise ValueError(
                        f"`memory_untouched` names {missing[0]!r}, which the seed does not create"
                    )

    def _mock_fits(self, mock: Mock, step_ids: list[str]) -> None:
        scripts = [("good", mock.good)] + [(b.name, b.script) for b in mock.bad]
        for who, script in scripts:
            unknown = [k for k in script if k not in step_ids]
            if unknown:
                raise ValueError(f"the {who} script has a step {unknown[0]!r} that does not exist")
        valid = set(self.check_ids())
        for bad in mock.bad:
            unknown = [f for f in bad.fails if f not in valid]
            if unknown:
                raise ValueError(
                    f"the bad run {bad.name!r} must fail {unknown[0]!r}, which is no check here"
                )
        names = [b.name for b in mock.bad]
        if len(set(names)) != len(names):
            raise ValueError("bad runs need distinct names")


# ---- loading --------------------------------------------------------------------------------------------
def _problems(error: ValidationError) -> str:
    found = problems(error)
    lines = [f"{where or '(top level)'}: {what}" for where, what in found[:6]]
    more = len(found) - 6
    return "; ".join(lines) + (f"; and {more} more" if more > 0 else "")


def parse(text: str, *, source: str = "scenario") -> Scenario:
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{source}: not valid YAML ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{source}: a scenario is a mapping of keys, not {type(data).__name__}")
    try:
        return Scenario.model_validate(data)
    except ValidationError as exc:
        raise ScenarioError(f"{source}: {_problems(exc)}") from exc


def load_file(path: Path) -> Scenario:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ScenarioError(f"{path.name}: larger than {MAX_FILE_BYTES // 1024} KiB")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ScenarioError(f"{path.name}: cannot be read ({type(exc).__name__})") from exc
    return parse(text, source=path.name)


def builtin_dir() -> Path:
    return Path(__file__).parent / "scenarios"


def user_dir(paths: Paths) -> Path:
    return paths.home / "evals"


@dataclass
class Catalog:
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)
    """`built-in`, or the file a user's scenario came from."""
    problems: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[Scenario]:
        return iter(self.scenarios.values())


def _read_folder(folder: Path, label: str, catalog: Catalog) -> None:
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            scenario = load_file(path)
        except ScenarioError as exc:
            catalog.problems.append(str(exc))
            continue
        if scenario.id in catalog.scenarios:
            catalog.problems.append(
                f"{path.name}: the id {scenario.id!r} is already used by "
                f"{catalog.origin[scenario.id]}; this file is ignored"
            )
            continue
        catalog.scenarios[scenario.id] = scenario
        catalog.origin[scenario.id] = label if label == "built-in" else f"{label}/{path.name}"


def load_catalog(paths: Paths | None = None) -> Catalog:
    """The built-in scenarios, then the owner's own (`PA_HOME/evals/*.yaml`). A broken file of the
    owner's is reported and skipped, never allowed to hide the others."""
    catalog = Catalog()
    _read_folder(builtin_dir(), "built-in", catalog)
    if paths is not None:
        _read_folder(user_dir(paths), "evals", catalog)
    return catalog
