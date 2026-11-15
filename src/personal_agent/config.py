"""User configuration (`config.toml`) and environment loading.

Secrets never live in `config.toml`; they come from the environment / `.env`.
Unknown keys are rejected so typos are noticed instead of silently ignored.
"""

from __future__ import annotations

import os
import tomllib
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_agent.clock import parse_quiet_hours
from personal_agent.paths import Paths

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentConfig(_Section):
    name: str = "Assistant"
    owner_name: str | None = None
    timezone: str | None = None
    """IANA zone name. `None` = detect from the system."""
    max_turns: int = Field(25, ge=1, le=200)
    """Model calls allowed in one run before the harness stops it."""
    history_items: int = Field(400, ge=20)
    """How many of the newest session items the model sees. The log keeps everything; this is a
    stopgap until summarising compaction lands."""


class ModelConfig(_Section):
    main: str = "gpt-6.1-sol"
    """Conversations and background work."""
    fast: str = "gpt-6-luna"
    """Cheap side jobs (summaries, guardian review, titles)."""
    reasoning_effort: ReasoningEffort = "medium"
    reasoning_context: Literal["auto", "current_turn", "all_turns"] | None = None
    verbosity: Literal["low", "medium", "high"] | None = None
    base_url: str | None = None
    """OpenAI-compatible endpoint. `None` = the OpenAI default / `OPENAI_BASE_URL`."""
    api: Literal["responses", "chat_completions"] = "responses"
    """`chat_completions` for OpenAI-compatible servers that do not implement the Responses API."""
    store: bool = False
    """False: OpenAI does not keep responses; the full history lives in our own state.db."""
    reasoning_item_ids: Literal["preserve", "omit"] = "preserve"
    """`omit` strips reasoning item IDs when history is replayed to the API. Use it only as a
    fallback if `pa doctor --live` reports that the API rejects replayed reasoning items."""
    timeout_s: float = Field(180.0, gt=0)
    """Per model call attempt."""
    max_retries: int = Field(3, ge=0, le=10)


class MemoryConfig(_Section):
    max_file_bytes: int = Field(65_536, ge=1_024)
    max_total_bytes: int = Field(4_194_304, ge=65_536)
    max_files: int = Field(500, ge=10)
    view_max_chars: int = Field(16_000, ge=1_000)
    index_max_lines: int = Field(200, ge=10)
    """Only the first N lines of state/MEMORY.md are injected into every run."""
    index_max_bytes: int = Field(25_000, ge=1_000)


class RuntimeConfig(_Section):
    concurrency: int = Field(2, ge=1, le=16)
    """Sessions worked on at the same time (one session is only ever handled by one run)."""
    run_timeout_s: float = Field(900.0, gt=0)
    """A single run that takes longer than this is stopped."""
    poll_s: float = Field(1.0, ge=0.05)
    """How often an idle worker checks the inbox for events added by other processes."""
    shutdown_grace_s: float = Field(30.0, ge=0)
    """On Ctrl-C or SIGTERM, runs in flight get this long to finish; the rest are put back untouched."""
    tool_timeout_s: float = Field(120.0, gt=0)
    """A single tool call that takes longer than this is abandoned (its result says so)."""
    tool_output_chars: int = Field(20_000, ge=1_000)
    """Longer tool output is cut; the model is told how much was left out."""


class PolicyConfig(_Section):
    mode: Literal["read_only", "supervised", "auto", "autonomous"] = "supervised"
    """read_only: look but never act. supervised: asks before anything that changes the world.
    auto: a small reviewer model decides, escalating to you when unsure. autonomous: no
    questions (only your deny rules and always_ask still apply)."""
    always_ask: list[str] = []
    """Patterns like "shell:*" or "mcp_*". These ask even in auto and autonomous mode."""
    allow: list[str] = []
    """Patterns that are pre-approved, for example "shell:git status*"."""
    deny: list[str] = []
    """Patterns that are never allowed in any mode."""
    allow_when_tainted: list[str] = []
    """Allow patterns that stay allowed after the run has read untrusted content."""
    approval_timeout_hours: float = Field(12.0, gt=0)
    """An approval request nobody answers within this time counts as a refusal."""
    i_understand_autonomous_risk: bool = False

    @model_validator(mode="after")
    def _autonomous_must_be_acknowledged(self) -> PolicyConfig:
        if self.mode == "autonomous" and not self.i_understand_autonomous_risk:
            raise ValueError(
                'policy.mode = "autonomous" lets the agent act without asking; '
                "set policy.i_understand_autonomous_risk = true to confirm"
            )
        return self


class BudgetConfig(_Section):
    per_run_usd: float = Field(1.0, gt=0)
    """A single run that spends more than this is stopped and reported (usually a loop)."""
    daily_usd: float = Field(5.0, gt=0)
    """Across all runs since local midnight. Reaching it pauses work until tomorrow; nothing is lost."""


class NotifyConfig(_Section):
    channel: str = "cli"
    """Where messages go when no conversation is waiting (scheduled work, alerts)."""
    quiet_hours: str | None = None
    """Local time window such as "23:00-08:00". Only urgent messages are delivered inside it."""
    max_per_hour: int = Field(6, ge=1)
    """Non-urgent messages per channel per hour; the rest wait for the next hour."""

    @field_validator("quiet_hours")
    @classmethod
    def _quiet_hours_are_valid(cls, value: str | None) -> str | None:
        if value is not None:
            parse_quiet_hours(value)
        return value


class SchedulerConfig(_Section):
    misfire_grace_s: int = Field(3600, ge=0)
    """A recurring schedule that is overdue by more than this (the computer was asleep) is
    treated according to its misfire policy instead of firing as if on time."""
    min_interval_s: int = Field(300, ge=60)
    """Shortest allowed gap between two firings of a recurring schedule."""
    max_timers: int = Field(50, ge=1)


class Config(_Section):
    agent: AgentConfig = AgentConfig()
    model: ModelConfig = ModelConfig()
    memory: MemoryConfig = MemoryConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    policy: PolicyConfig = PolicyConfig()
    budget: BudgetConfig = BudgetConfig()
    notify: NotifyConfig = NotifyConfig()
    scheduler: SchedulerConfig = SchedulerConfig()


DEFAULT_CONFIG_TOML = """\
# personal-agent configuration. Secrets do NOT go here: put OPENAI_API_KEY etc. in .env.

[agent]
name = "Assistant"
# owner_name = "Your Name"
# timezone = "Asia/Shanghai"     # IANA name; default: detected from this machine
max_turns = 25                   # model calls per run before the harness stops it
history_items = 400              # newest conversation items the model sees (the log keeps all)

[model]
main = "gpt-6.1-sol"             # conversations and background work
fast = "gpt-6-luna"              # cheap side jobs (summaries, guardian review)
reasoning_effort = "medium"      # none | minimal | low | medium | high | xhigh | max
# reasoning_context = "all_turns"  # auto | current_turn | all_turns
# base_url = "https://api.openai.com/v1"   # any OpenAI-compatible endpoint
# api = "chat_completions"       # for compatible servers without the Responses API
store = false                    # false: OpenAI keeps no copy; history lives in state.db
# reasoning_item_ids = "omit"    # fallback if `pa doctor --live` says replayed reasoning is rejected
timeout_s = 180
max_retries = 3

[memory]
max_file_bytes = 65536
max_total_bytes = 4194304
max_files = 500
view_max_chars = 16000
index_max_lines = 200            # lines of memory/state/MEMORY.md injected into every run
index_max_bytes = 25000

[runtime]
concurrency = 2                  # conversations worked on at once
run_timeout_s = 900              # a run longer than this is stopped
tool_timeout_s = 120             # a single tool call longer than this is abandoned
shutdown_grace_s = 30            # on stop, runs in flight get this long to finish

[policy]
mode = "supervised"              # read_only | supervised | auto | autonomous
# always_ask = ["shell:*"]       # patterns that ask in every mode except read_only
# allow = ["shell:git status*"]  # patterns that are pre-approved
# deny = ["shell:rm -rf *"]      # patterns that are never allowed
approval_timeout_hours = 12      # an unanswered request counts as a refusal

[budget]
per_run_usd = 1.0                # one run above this is stopped and reported
daily_usd = 5.0                  # all runs since local midnight; reaching it pauses work, loses nothing

[notify]
channel = "cli"                  # where unprompted messages go
# quiet_hours = "23:00-08:00"    # local time; only urgent messages get through
max_per_hour = 6                 # non-urgent messages per channel per hour

[scheduler]
misfire_grace_s = 3600           # how late a recurring job may run after the computer slept
min_interval_s = 300             # shortest allowed gap between firings of a recurring job
max_timers = 50
"""


DEFAULT_ENV = """\
# Secrets for personal-agent. This file is private (mode 600) and never leaves this machine.
# Put your key after the equals sign. Do not paste it into chat or commit it.
OPENAI_API_KEY=

# Optional: an OpenAI-compatible endpoint instead of api.openai.com
# OPENAI_BASE_URL=
"""


class ConfigError(Exception):
    pass


def load_config(paths: Paths) -> Config:
    """Read `config.toml` (optional) and validate it."""
    data: dict = {}
    if paths.config_file.exists():
        try:
            data = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {paths.config_file}: {exc}") from exc
    try:
        return Config.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError -> readable message
        raise ConfigError(f"invalid {paths.config_file}:\n{exc}") from exc


def load_env(paths: Paths) -> None:
    """Load `$PA_HOME/.env` into the process environment (never overriding real env vars).

    Only that one file is read: a `.env` in whatever directory the CLI happens to run in
    may belong to an unrelated project and must not leak into this agent.
    """
    if paths.env_file.exists():
        load_dotenv(paths.env_file, override=False)


def api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") or None
