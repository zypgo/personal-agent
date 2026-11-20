"""The public V3 JSON contract, shared by validation and OpenAPI generation.

The API deliberately authenticates *before* reading a request body and revalidates after that
await. FastAPI's automatic body injection would move that boundary, so endpoints use these
strict Pydantic models explicitly. Unknown fields remain ignored for backwards compatibility;
in particular a client cannot supply actor, approver, or authorization metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, RootModel, model_validator

from personal_agent.execution.models import MAX_COMMAND_CHARS, MAX_FILE_BYTES, MAX_FILES

Identifier = Annotated[str, Field(max_length=80)]
Label = Annotated[str, Field(max_length=100)]
Code = Annotated[str, Field(max_length=200)]
PlanHash = Annotated[str, Field(max_length=64)]
Revision = Annotated[int, Field(ge=1)]
Paths = Annotated[list[str], Field(max_length=32)]
Grants = Annotated[list[str], Field(max_length=32)]
Output = Annotated[str, Field(max_length=100000)]
Role = Literal["owner", "member", "viewer"]


class RequestModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    errors: ClassVar[dict[str, str]] = {}


class CreateSpace(RequestModel):
    name: Label


class ChangeMember(RequestModel):
    principal_id: Identifier
    revoke: bool = False
    role: Role | None = None

    @model_validator(mode="after")
    def role_for_change(self) -> ChangeMember:
        if not self.revoke and self.role is None:
            raise ValueError("A role is required when changing membership.")
        return self


class InviteMember(RequestModel):
    role: Role = "member"
    label: Label = ""


class RedeemCode(RequestModel):
    code: Code = Field(repr=False)


class Enroll(RedeemCode):
    display_name: Label = "Member"
    require_existing_session: bool = False


class PairDevice(RequestModel):
    label: Label = ""


class RevokeChannel(RequestModel):
    channel: Annotated[str, Field(max_length=200)]
    account_id: Annotated[str, Field(max_length=200)]
    sender_id: Annotated[str, Field(max_length=200)]


class ExecutionInput(RequestModel):
    command: Annotated[str, Field(max_length=MAX_COMMAND_CHARS)]
    timeout_s: Annotated[float, Field(ge=1, le=3600, allow_inf_nan=False)] = 900
    input_paths: Paths = Field(default_factory=list)
    output_paths: Paths = Field(default_factory=list)


class CreateTask(ExecutionInput):
    title: Annotated[str, Field(max_length=200)]
    client_request_id: Annotated[str, Field(max_length=128)]


class ReviseTask(RequestModel):
    revision: Revision
    title: Annotated[str, Field(max_length=200)]
    errors: ClassVar[dict[str, str]] = {"revision": "revision must be a positive integer."}


class AddStep(ExecutionInput):
    expected_revision: Revision = Field(
        validation_alias=AliasChoices("expected_revision", "revision")
    )
    step_id: Annotated[str, Field(max_length=2)]
    errors: ClassVar[dict[str, str]] = {
        "expected_revision": "expected_revision must be a positive integer.",
        "revision": "expected_revision must be a positive integer.",
    }


class AskQuestion(RequestModel):
    expected_revision: Revision
    question: Annotated[str, Field(max_length=1000)]
    choices: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=10)] = Field(
        default_factory=list
    )
    request_id: Annotated[str, Field(max_length=128)]
    errors: ClassVar[dict[str, str]] = {
        "expected_revision": "expected_revision must be a positive integer."
    }


class AnswerQuestion(RequestModel):
    expected_revision: Revision
    answer: Annotated[str, Field(max_length=4000)]
    errors: ClassVar[dict[str, str]] = {
        "expected_revision": "expected_revision must be a positive integer."
    }


class ApproveJob(RequestModel):
    plan_hash: PlanHash
    allowed: bool
    errors: ClassVar[dict[str, str]] = {"allowed": "allowed must be a boolean."}


class ReviewJob(ApproveJob):
    space_id: Identifier
    job_id: Annotated[str, Field(max_length=64)]


class WriteKnowledge(RequestModel):
    path: Annotated[str, Field(max_length=100)]
    text: Annotated[str, Field(max_length=30000)]
    revision: Annotated[int, Field(ge=0)] = 0


class InviteNode(RequestModel):
    space_ids: Grants = Field(default_factory=list)
    label: Label = ""


class PairNode(RedeemCode):
    capabilities: Grants = Field(default_factory=list)


class ClaimNode(RequestModel):
    capabilities: Grants = Field(default_factory=list)


class NodeAttempt(RequestModel):
    space_id: Identifier
    generation: int
    plan_hash: PlanHash


class AdmitAttempt(NodeAttempt):
    resource_id: Annotated[str, Field(max_length=256)]


class ObserveAttempt(NodeAttempt):
    state: Annotated[str, Field(max_length=30)]
    sequence: Annotated[int, Field(ge=0)] | None = None
    stdout: Output = ""
    stderr: Output = ""


class TransferFile(RequestModel):
    path: Annotated[str, Field(max_length=500)]
    size: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)]
    sha256: Annotated[str, Field(max_length=64)]
    content_base64: Annotated[str, Field(max_length=4 * ((MAX_FILE_BYTES + 2) // 3))] = Field(
        repr=False
    )


class CompleteAttempt(NodeAttempt):
    status: Annotated[str, Field(max_length=30)]
    exit_code: Annotated[int, Field(ge=-256, le=255)] | None = None
    stdout: Output = ""
    stderr: Output = ""
    artifacts: Annotated[list[TransferFile], Field(max_length=MAX_FILES)] | None = None


class CleanupAttempt(NodeAttempt):
    released: bool
    errors: ClassVar[dict[str, str]] = {"released": "released must be a boolean."}


NODE_ACTIONS: dict[str, type[NodeAttempt]] = {
    "status": NodeAttempt,
    "admit": AdmitAttempt,
    "observe": ObserveAttempt,
    "complete": CompleteAttempt,
    "cleanup": CleanupAttempt,
}


class NodeOperationRequest(
    RootModel[NodeAttempt | AdmitAttempt | ObserveAttempt | CompleteAttempt | CleanupAttempt]
):
    """The operation in the URL selects the exact request variant."""


class ResponseModel(BaseModel):
    """Known fields are typed; stores may add compatible fields without truncating a response."""

    model_config = ConfigDict(extra="allow")


class Principal(ResponseModel):
    id: str
    display_name: str
    is_admin: bool
    authz_version: int
    created_at: float


class Space(ResponseModel):
    id: str
    kind: Literal["personal", "project"]
    name: str
    owner_id: str
    created_at: float


class IdentityResponse(ResponseModel):
    principal: Principal
    spaces: list[Space]
    space_id: str
    role: Role


class SpaceResponse(ResponseModel):
    space: Space


class OkResponse(ResponseModel):
    ok: bool


class EnrollmentResponse(OkResponse):
    recovery_code: str | None


class ErrorResponse(BaseModel):
    error: str


class CodeResponse(ResponseModel):
    code: str


class InvitationResponse(CodeResponse):
    expires_at: float


class TaskRecord(ResponseModel):
    id: str
    actor_id: str
    title: str
    state: str
    revision: int
    created_at: float
    updated_at: float
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)


class TaskResponse(ResponseModel):
    task: TaskRecord


class TaskDetailResponse(TaskResponse):
    jobs: list[dict[str, Any]]


class TasksResponse(ResponseModel):
    tasks: list[TaskRecord]


class JobResponse(ResponseModel):
    job: dict[str, Any]


class QuestionResponse(ResponseModel):
    question: dict[str, Any]


class MembersResponse(ResponseModel):
    members: list[dict[str, Any]]


class DevicesResponse(ResponseModel):
    devices: list[dict[str, Any]]


class ChannelsResponse(ResponseModel):
    bindings: list[dict[str, Any]]


class ReviewsResponse(ResponseModel):
    pending: list[dict[str, Any]]


class LogsResponse(ResponseModel):
    logs: list[dict[str, Any]]
    snapshots: bool


class KnowledgeDocument(ResponseModel):
    path: str
    text: str
    revision: int


class KnowledgeResponse(ResponseModel):
    documents: list[KnowledgeDocument]


class HistoryResponse(ResponseModel):
    versions: list[dict[str, Any]]


class NodesResponse(ResponseModel):
    nodes: list[dict[str, Any]]


class PairNodeResponse(ResponseModel):
    node: dict[str, Any]
    token: str


class ClaimResponse(ResponseModel):
    attempt: dict[str, Any] | None


class AttemptResponse(ResponseModel):
    accepted: bool
    attempt: dict[str, Any]


class AttemptStatusResponse(ResponseModel):
    authorized: bool
    desired_state: str
    state: str
    generation: int


class NodeOperationResponse(RootModel[AttemptResponse | AttemptStatusResponse]):
    pass


class DiagnosticsResponse(ResponseModel):
    budget: dict[str, float]
    tasks: dict[str, int]
    outbox: dict[str, int]
    nodes: list[dict[str, Any]]
    cleanup_pending: int


@dataclass(frozen=True)
class RouteContract:
    path: str
    handler: str
    method: str = "GET"
    request: type[BaseModel] | None = None
    response: type[BaseModel] | None = None
    status: int = 200
    authority: Literal["session", "space", "node", "public"] = "space"


ROUTES = (
    RouteContract("/me", "me", response=IdentityResponse),
    RouteContract("/spaces", "create_space", "POST", CreateSpace, SpaceResponse, 201, "session"),
    RouteContract("/members", "members", response=MembersResponse),
    RouteContract("/members", "members", "POST", ChangeMember, MembersResponse),
    RouteContract("/invites", "invite", "POST", InviteMember, InvitationResponse, 201),
    RouteContract("/enroll", "enroll", "POST", Enroll, EnrollmentResponse, authority="public"),
    RouteContract("/recover", "recover", "POST", RedeemCode, OkResponse, authority="public"),
    RouteContract("/recovery-code", "recovery", "POST", response=CodeResponse, authority="session"),
    RouteContract("/devices", "devices", response=DevicesResponse, authority="session"),
    RouteContract(
        "/devices/pair", "device_pair", "POST", PairDevice, InvitationResponse, authority="session"
    ),
    RouteContract(
        "/devices/{device_id:int}/revoke",
        "device_revoke",
        "POST",
        response=OkResponse,
        authority="session",
    ),
    RouteContract("/channel-binding", "channel_pair", "POST", response=InvitationResponse),
    RouteContract("/channel-bindings", "channel_bindings", response=ChannelsResponse),
    RouteContract("/channel-bindings/revoke", "channel_revoke", "POST", RevokeChannel, OkResponse),
    RouteContract("/tasks", "tasks", response=TasksResponse),
    RouteContract("/tasks", "tasks", "POST", CreateTask, TaskResponse, 201),
    RouteContract("/tasks/{task_id}", "task", response=TaskDetailResponse),
    RouteContract("/tasks/{task_id}/cancel", "cancel", "POST", response=TaskResponse),
    RouteContract("/tasks/{task_id}/revise", "revise", "POST", ReviseTask, TaskResponse),
    RouteContract("/tasks/{task_id}/steps", "step", "POST", AddStep, JobResponse, 201),
    RouteContract(
        "/tasks/{task_id}/questions", "question", "POST", AskQuestion, QuestionResponse, 201
    ),
    RouteContract(
        "/tasks/{task_id}/questions/{question_id}/answer",
        "answer_question",
        "POST",
        AnswerQuestion,
        QuestionResponse,
    ),
    RouteContract("/jobs/{job_id}/approve", "approve", "POST", ApproveJob, JobResponse),
    RouteContract("/review-jobs", "review", response=ReviewsResponse, authority="session"),
    RouteContract("/review-jobs", "review", "POST", ReviewJob, JobResponse, authority="session"),
    RouteContract("/attempts/{attempt_id}/logs", "logs", response=LogsResponse),
    RouteContract("/knowledge", "knowledge", response=KnowledgeResponse),
    RouteContract("/knowledge", "knowledge", "POST", WriteKnowledge, KnowledgeResponse),
    RouteContract("/knowledge/history", "knowledge_history", response=HistoryResponse),
    RouteContract("/diagnostics", "diagnostics", response=DiagnosticsResponse),
    RouteContract("/nodes", "node_list", response=NodesResponse),
    RouteContract(
        "/node-invites", "node_invite", "POST", InviteNode, InvitationResponse, 201, "session"
    ),
    RouteContract(
        "/nodes/{node_id}/revoke", "node_revoke", "POST", response=OkResponse, authority="session"
    ),
    RouteContract(
        "/nodes/pair", "node_pair", "POST", PairNode, PairNodeResponse, authority="public"
    ),
    RouteContract("/nodes/claim", "node_claim", "POST", ClaimNode, ClaimResponse, authority="node"),
    RouteContract(
        "/nodes/attempts/{attempt_id}/{action}",
        "node_attempt",
        "POST",
        NodeOperationRequest,
        NodeOperationResponse,
        authority="node",
    ),
)
