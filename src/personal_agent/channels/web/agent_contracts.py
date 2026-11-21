"""Strict V4 browser/worker contracts. Authenticate before reading any body."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from personal_agent.channels.web.contracts import RequestModel

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
AgentKind = Literal["grok", "muse", "personal_agent", "codex", "cursor"]
WakeMode = Literal["manual", "grok_routine", "native_schedule", "verified_hook"]


class AgentInput(RequestModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: AgentKind
    allowed_actor_ids: Annotated[list[str], Field(max_length=32)] = Field(default_factory=list)
    shared_environment: bool = False
    capabilities: Annotated[list[str], Field(max_length=32)] = Field(default_factory=list)
    secret_env: dict[str, str] = Field(default_factory=dict)
    wake_mode: WakeMode = "manual"


class StepInput(RequestModel):
    id: Identifier
    agent_id: Identifier
    prompt: Annotated[str, Field(min_length=1, max_length=16000)]
    depends_on: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    acceptance: Annotated[str, Field(max_length=4000)] = (
        "Return the requested result with supporting evidence."
    )
    required: bool = True


class TaskInput(RequestModel):
    client_request_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=200)]
    steps: Annotated[list[StepInput], Field(min_length=1, max_length=16)]
    timeout_s: Annotated[float, Field(ge=60, le=604800, allow_inf_nan=False)] = 86400
    visibility: Literal["private", "space"] = "private"


class PlanDecision(RequestModel):
    revision: Annotated[int, Field(ge=1)]
    plan_hash: Annotated[str, Field(min_length=64, max_length=64)]


class ResultDecision(PlanDecision):
    step_id: Identifier
    attempt_id: Identifier
    reason: Annotated[str, Field(max_length=4000)] = ""


class AnswerInput(RequestModel):
    answer: Annotated[str, Field(min_length=1, max_length=8000)]


class SnoozeInput(RequestModel):
    hours: Annotated[float, Field(gt=0, le=720, allow_inf_nan=False)] = 24


class StoppedInput(RequestModel):
    step_id: Identifier
    attempt_id: Identifier
    note: Annotated[str, Field(min_length=1, max_length=1000)]


class WakeStoppedInput(RequestModel):
    note: Annotated[str, Field(min_length=1, max_length=1000)]


class HelloInput(RequestModel):
    capabilities: Annotated[list[str], Field(max_length=32)] = Field(default_factory=list)
    runtime_version: Annotated[str | None, Field(min_length=1, max_length=100)] = None


class ClaimInput(RequestModel):
    worker_id: Identifier
    task_id: Identifier | None = None
    lease_seconds: Annotated[int, Field(ge=30, le=900)] = 300


class AttemptInput(RequestModel):
    generation: Annotated[int, Field(ge=1)]
    lease_token: Annotated[str, Field(min_length=1, max_length=200)] = Field(repr=False)


class HeartbeatInput(AttemptInput):
    lease_seconds: Annotated[int, Field(ge=30, le=900)] = 300
    state: Literal["running", "waiting_input"] = "running"


class UncertainInput(AttemptInput):
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class EventInput(AttemptInput):
    sequence: Annotated[int, Field(ge=1, le=2**53 - 1)]
    kind: Literal["progress", "started", "checkpoint"] = "progress"
    text: Annotated[str, Field(max_length=8000)] = ""


class FileInput(RequestModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    mime: Annotated[str, Field(max_length=100)] = "application/octet-stream"
    content_base64: Annotated[str, Field(max_length=2796204)] = Field(repr=False)
    sha256: Annotated[str, Field(min_length=64, max_length=64)]


class ResultInput(AttemptInput):
    status: Literal["succeeded", "failed", "cancelled"] = "succeeded"
    text: Annotated[str, Field(max_length=64000)] = ""
    artifacts: Annotated[list[FileInput], Field(max_length=8)] = Field(default_factory=list)


class QuestionInput(AttemptInput):
    request_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    choices: Annotated[list[str], Field(max_length=8)] = Field(default_factory=list)
