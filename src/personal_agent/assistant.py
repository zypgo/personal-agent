"""User-facing workspace capabilities, bounded reads and durable action proposals.

The model can inspect and propose. Only an authenticated browser can begin an action;
the original V3/V4 endpoint remains the authority for its actual side effect. A lost
browser response never resets a begun proposal to a state that permits another send.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from personal_agent.collaboration.models import AccessDenied
from personal_agent.execution.models import ExecutionError
from personal_agent.orchestration.models import OrchestrationError
from personal_agent.security.redact import known_secret_values, redact

if TYPE_CHECKING:
    from personal_agent.app import App


class AssistantError(ValueError):
    pass


class AssistantConflict(AssistantError):
    pass


@dataclass(frozen=True)
class Capability:
    action: str
    title: str
    section: str
    fields: tuple[str, ...] = ()
    target: str = ""
    authority: str = "write"


CAPABILITIES = (
    Capability("create_space", "创建项目", "spaces", ("name",)),
    Capability("accept_project_invite", "加入已有项目", "spaces"),
    Capability("invite_member", "邀请成员", "members", ("role", "label"), authority="manage"),
    Capability("change_member", "调整成员权限", "members", ("role", "revoke"), "member", "manage"),
    Capability("pair_device", "配对本人设备", "devices", ("label",)),
    Capability("revoke_device", "撤销本人设备", "devices", target="device"),
    Capability("create_recovery_code", "重新生成恢复码", "devices"),
    Capability("bind_channel", "绑定聊天渠道", "channels"),
    Capability(
        "revoke_channel", "撤销渠道绑定", "channels", ("channel", "account_id", "sender_id")
    ),
    Capability("invite_node", "连接执行电脑", "nodes", ("label", "space_ids"), authority="admin"),
    Capability("revoke_node", "撤销执行电脑", "nodes", target="node", authority="admin"),
    Capability("write_knowledge", "编辑项目资料", "knowledge", ("path", "text", "revision")),
    Capability(
        "create_execution_task",
        "创建电脑任务",
        "tasks",
        ("title", "command", "input_paths", "output_paths", "timeout_s"),
    ),
    Capability(
        "revise_execution_task", "修改任务目标", "tasks", ("revision", "title"), "execution_task"
    ),
    Capability(
        "add_execution_step",
        "增加执行步骤",
        "tasks",
        ("expected_revision", "step_id", "command", "timeout_s", "input_paths", "output_paths"),
        "execution_task",
    ),
    Capability("cancel_execution_task", "取消电脑任务", "tasks", target="execution_task"),
    Capability(
        "create_agent",
        "连接 Agent",
        "agents",
        ("name", "kind", "wake_mode", "allowed_actor_ids", "shared_environment", "capabilities"),
    ),
    Capability(
        "edit_agent",
        "编辑 Agent 连接",
        "agents",
        ("name", "kind", "wake_mode", "allowed_actor_ids", "shared_environment", "capabilities"),
        "agent",
    ),
    Capability("rotate_agent_token", "更新 Agent 凭据", "agents", target="agent"),
    Capability("revoke_agent", "撤销 Agent 连接", "agents", target="agent"),
    Capability(
        "create_agent_task",
        "创建 Agent 协作",
        "agent_tasks",
        ("title", "steps", "timeout_s", "visibility"),
    ),
    Capability("cancel_agent_task", "取消 Agent 任务", "agent_tasks", target="agent_task"),
    Capability(
        "confirm_agent_stopped",
        "确认执行已停止",
        "agent_tasks",
        ("step_id", "attempt_id"),
        "agent_task",
    ),
    Capability("confirm_wake_stopped", "确认唤醒已停止", "agent_tasks", ("wake_id",), "agent_task"),
)
BY_ACTION = {item.action: item for item in CAPABILITIES}
TOPICS = (
    "overview",
    "spaces",
    "members",
    "tasks",
    "agent_tasks",
    "agents",
    "agent_occupancy",
    "agent_events",
    "execution_logs",
    "knowledge",
    "knowledge_history",
    "devices",
    "nodes",
    "channels",
    "diagnostics",
    "reviews",
    "cards",
)
DIRECT_TOOLS = {
    "computer_tasks": ["submit_job", "job_status", "cancel_job", "ask_task_input"],
    "agent_tasks": ["list_agents", "delegate_agents", "agent_task_status", "cancel_agent_task"],
    "memory_and_knowledge": ["memory", "search_history", "search_tool_history"],
    "reminders": ["schedule"],
}
_PRIVATE_FIELDS = frozenset(
    {
        "token",
        "worker_token",
        "token_hash",
        "lease_token",
        "secret_env",
        "content_base64",
        "authorization",
        "owner_authorization",
        "approver_authorization",
        "answer_authorization",
        "recovery_code",
        "execution_id",
    }
)
_DOCUMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,90}\.md")
MAX_PROPOSALS = 40
PROPOSAL_SECONDS = 86400


def _clean(value: Any) -> Any:
    """Preserve exact public structure; never return credential or authority internals."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in _PRIVATE_FIELDS}
    if isinstance(value, list | tuple):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        return redact(value, known_secret_values())
    return value


def _identity(app: App, actor_id: str, *, write: bool = False) -> tuple[Any, Any]:
    if app.identities is None:
        raise AssistantError("Workspace identities are unavailable.")
    member = app.identities.require_access(actor_id, app.space_id, write=write)
    return app.identities.principal(actor_id), member


def _root(app: App) -> App:
    return app.collaboration.root if app.collaboration is not None else app


def _allowed(capability: Capability, principal: Any, member: Any) -> bool:
    if not member.can_write:
        return False
    if capability.authority == "manage":
        return bool(member.can_manage)
    if capability.authority == "admin":
        return bool(principal.is_admin)
    return True


def capability_catalog(app: App, actor_id: str) -> dict[str, Any]:
    principal, member = _identity(app, actor_id)
    assert app.identities is not None
    is_project = app.identities.space(app.space_id).kind == "project"
    return {
        "space_id": app.space_id,
        "topics": list(TOPICS),
        "direct_tools": DIRECT_TOOLS,
        "actions": [
            {
                **asdict(item),
                "available": _allowed(item, principal, member)
                and (item.authority != "manage" or is_project),
                "mode": "user_card",
            }
            for item in CAPABILITIES
        ],
        "user_only": [
            "execution_approval",
            "agent_plan",
            "agent_result",
            "execution_question",
            "agent_question",
        ],
        "notice": "Proposals do not grant permission or execute actions. Credentials, invitations, approvals and acceptance stay in authenticated user controls.",
    }


def _authorize_action(
    app: App, actor_id: str, action: str, target_id: str, draft: dict[str, Any]
) -> Capability:
    principal, member = _identity(app, actor_id, write=True)
    spec = BY_ACTION.get(action)
    if spec is None:
        raise AssistantError("Unknown workspace action.")
    if not _allowed(spec, principal, member):
        raise AccessDenied("This workspace action is not available to this user.")
    if spec.authority == "manage":
        assert app.identities is not None
        if app.identities.space(app.space_id).kind != "project":
            raise AssistantError("Select a shared project before managing its membership.")
    if (
        not isinstance(target_id, str)
        or len(target_id) > 128
        or any(c in target_id for c in "/\\?#")
    ):
        raise AssistantError("Use a resource identifier, not a URL or path.")
    if spec.target and not target_id:
        raise AssistantError("This action needs an existing target_id.")
    if action == "accept_project_invite" and target_id:
        raise AssistantError("Enter the invitation only in the user's secure form.")
    root = _root(app)
    if spec.target == "member":
        assert app.identities is not None
        if not any(
            row["principal_id"] == target_id
            for row in app.identities.members(actor_id, app.space_id)
        ):
            raise AccessDenied("This member is unavailable.")
    elif spec.target == "device":
        if not any(
            str(item.id) == target_id for item in root.web_auth.sessions(principal_id=actor_id)
        ):
            raise AccessDenied("This device is unavailable.")
    elif spec.target == "node":
        if root.nodes is None or not any(item.id == target_id for item in root.nodes.list()):
            raise AccessDenied("This node is unavailable.")
    elif spec.target == "execution_task":
        if app.jobs is None:
            raise AssistantError("Computer tasks are unavailable.")
        task = app.jobs.get_task(target_id)
        if task["actor_id"] != actor_id and not member.can_manage:
            raise AccessDenied("Only the task requester or a project owner can change it.")
    elif spec.target == "agent":
        if app.orchestration is None:
            raise AssistantError("Agent collaboration is unavailable.")
        agent = app.orchestration.get_agent(target_id, actor_id)
        if agent["owner_id"] != actor_id:
            raise AccessDenied("Only the Agent owner can manage this connection.")
    elif spec.target == "agent_task":
        if app.orchestration is None:
            raise AssistantError("Agent collaboration is unavailable.")
        if action == "cancel_agent_task":
            task = app.orchestration.get_task(target_id, actor_id)
            if not task["can_manage"]:
                raise AccessDenied("This task cannot be changed by this user.")
        else:
            # Owning a resource permits stopping its execution, but does not grant
            # access to another requester's private task title, prompt or result.
            for agent in app.orchestration.list_agents(actor_id):
                if agent["owner_id"] != actor_id:
                    continue
                occupied = app.orchestration.owner_occupancy(agent["id"], actor_id)
                rows = occupied["attempts" if action == "confirm_agent_stopped" else "wakes"]
                if any(
                    row["task_id"] == target_id
                    and row["can_confirm_stopped"]
                    and (
                        row.get("step_id") == draft.get("step_id")
                        and row.get("attempt_id") == draft.get("attempt_id")
                        if action == "confirm_agent_stopped"
                        else row.get("wake_id") == draft.get("wake_id")
                    )
                    for row in rows
                ):
                    break
            else:
                raise AccessDenied("Only the resource owner can confirm this execution stopped.")
    if action == "write_knowledge":
        name = draft.get("path", target_id)
        if name and (not isinstance(name, str) or _DOCUMENT.fullmatch(name) is None):
            raise AssistantError("Use a simple Markdown filename.")
    return spec


def _prefix(actor_id: str) -> str:
    return "assistant:proposal:" + hashlib.sha256(actor_id.encode()).hexdigest() + ":"


def _records(app: App, actor_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(row["value"])
        for row in app.db.query(
            "SELECT value FROM kv WHERE key LIKE ? ORDER BY updated_at DESC",
            (_prefix(actor_id) + "%",),
        )
    ]


def _check_secret_input(value: dict[str, Any], *, max_chars: int = 40000) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, RecursionError) as exc:
        raise AssistantError("Use a finite, bounded JSON object.") from exc
    if len(encoded) > max_chars:
        raise AssistantError("The proposed form is too large.")
    if redact(encoded, known_secret_values()) != encoded:
        raise AssistantError("Do not put credentials in an action proposal or saved form.")


def prepare_action(
    app: App,
    actor_id: str,
    *,
    work_id: str,
    action_key: str,
    action: str,
    target_id: str = "",
    draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save only a user-owned proposal. This function cannot create a credential or approval."""
    draft = {} if draft is None else draft
    if (
        not isinstance(draft, dict)
        or not isinstance(action_key, str)
        or not 1 <= len(action_key.strip()) <= 128
    ):
        raise AssistantError("Give an action_key of 1 to 128 characters and a JSON object draft.")
    spec = _authorize_action(app, actor_id, action, target_id, draft)
    if set(draft) - set(spec.fields):
        raise AssistantError("The draft contains fields unavailable to the assistant.")
    _check_secret_input(draft)
    identifier = (
        "ac_"
        + hashlib.sha256(
            json.dumps([app.space_id, actor_id, work_id, action_key]).encode()
        ).hexdigest()[:40]
    )
    immutable = {"action": action, "target_id": target_id, "draft": draft}
    with app.db.tx():
        _authorize_action(app, actor_id, action, target_id, draft)
        authority = app.actor_snapshot(actor_id)
        key = _prefix(actor_id) + identifier
        previous = app.db.kv_get(key)
        if previous is not None:
            saved = json.loads(previous)
            if saved["authority"] != authority:
                raise AccessDenied("The action's authorization changed; request a new proposal.")
            if any(saved[name] != value for name, value in immutable.items()):
                raise AssistantConflict("This action_key already identifies a different proposal.")
            if saved["status"] == "proposed" and saved["expires_at"] <= app.clock.now():
                raise AssistantConflict("This action card expired; request a new proposal.")
            return _proposal_card(saved)
        if (
            sum(
                record["status"] in {"proposed", "executing", "uncertain"}
                # Old authority is permanently fenced and hidden from the inbox;
                # those receipts must not occupy the new authority's card quota.
                and record["authority"] == authority
                and (record["status"] != "proposed" or record["expires_at"] > app.clock.now())
                for record in _records(app, actor_id)
            )
            >= MAX_PROPOSALS
        ):
            raise AssistantConflict("Review existing action cards before proposing more.")
        record = {
            "id": identifier,
            "principal_id": actor_id,
            "space_id": app.space_id,
            "authority": authority,
            "status": "proposed",
            "created_at": app.clock.now(),
            "expires_at": app.clock.now() + PROPOSAL_SECONDS,
            **immutable,
        }
        app.db.kv_set(
            key, json.dumps(record, ensure_ascii=False, allow_nan=False), now=app.clock.now()
        )
    app.live.publish({"type": "assistant_changed", "principal_id": actor_id})
    return _proposal_card(record)


def _record(app: App, actor_id: str, card_id: str) -> dict[str, Any]:
    _identity(app, actor_id, write=True)
    if re.fullmatch(r"ac_[a-f0-9]{40}", card_id) is None:
        raise AssistantError("Action card is unavailable.")
    value = app.db.kv_get(_prefix(actor_id) + card_id)
    if value is None:
        raise AccessDenied("Action card is unavailable.")
    record: dict[str, Any] = json.loads(value)
    if (
        record["principal_id"] != actor_id
        or record["space_id"] != app.space_id
        or record["authority"] != app.actor_snapshot(actor_id)
    ):
        raise AccessDenied("The action's authorization changed; request a new proposal.")
    return record


def _final_payload(record: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from pydantic import BaseModel, ValidationError

    from personal_agent.channels.web import agent_contracts as agent_dto
    from personal_agent.channels.web import contracts as dto

    models: dict[str, type[BaseModel]] = {
        "create_space": dto.CreateSpace,
        "invite_member": dto.InviteMember,
        "change_member": dto.ChangeMember,
        "pair_device": dto.PairDevice,
        "revoke_channel": dto.RevokeChannel,
        "invite_node": dto.InviteNode,
        "write_knowledge": dto.WriteKnowledge,
        "create_execution_task": dto.CreateTask,
        "revise_execution_task": dto.ReviseTask,
        "add_execution_step": dto.AddStep,
        "create_agent": agent_dto.AgentInput,
        "edit_agent": agent_dto.AgentInput,
        "create_agent_task": agent_dto.TaskInput,
        "confirm_agent_stopped": agent_dto.StoppedInput,
        "confirm_wake_stopped": agent_dto.WakeStoppedInput,
    }
    action = record["action"]
    model = models.get(action)
    if not isinstance(payload, dict):
        raise AssistantError("The form payload must be an object.")
    if model is None:
        if payload:
            raise AssistantError("This action has no form fields.")
        return {}
    # Do not silently ignore actor/URL/credential fields as the legacy DTO may do.
    allowed = set(model.model_fields)
    if set(payload) - allowed:
        raise AssistantError("Unexpected form fields.")
    fixed = dict(payload)
    if action == "change_member" and fixed.get("principal_id") != record["target_id"]:
        raise AssistantConflict("The selected member changed.")
    if action in {"create_execution_task", "create_agent_task"}:
        fixed["client_request_id"] = "assistant:" + record["id"]
    if action == "confirm_agent_stopped":
        for key in ("step_id", "attempt_id"):
            if fixed.get(key) != record["draft"].get(key):
                raise AssistantConflict("The selected execution changed.")
    if action in {"create_agent", "edit_agent"}:
        refs = fixed.get("secret_env", {})
        if not isinstance(refs, dict) or any(
            not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None
            for value in refs.values()
        ):
            raise AssistantError(
                "Connection secrets must be environment-variable names, never values."
            )
    try:
        data = model.model_validate(fixed).model_dump()
    except ValidationError as exc:
        raise AssistantError(
            "Complete the form using its expected field types and limits."
        ) from exc
    _check_secret_input(data, max_chars=400000)
    return data


def begin_action(app: App, actor_id: str, card_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically fence a single browser send before any existing API is called."""
    with app.db.tx():
        record = _record(app, actor_id, card_id)
        if record["status"] != "proposed":
            raise AssistantConflict(
                "This action was already begun; inspect its outcome instead of sending again."
            )
        if record["expires_at"] <= app.clock.now():
            raise AssistantConflict("This action card expired; request a new proposal.")
        data = _final_payload(record, payload)
        _authorize_action(
            app, actor_id, record["action"], record["target_id"], {**record["draft"], **data}
        )
        record.update(
            status="executing",
            execution_id=secrets.token_hex(16),
            started_at=app.clock.now(),
            payload=data,
        )
        app.db.kv_set(
            _prefix(actor_id) + card_id, json.dumps(record, ensure_ascii=False), now=app.clock.now()
        )
    app.live.publish({"type": "assistant_changed", "principal_id": actor_id})
    return {
        **{key: record[key] for key in ("execution_id", "action", "target_id", "payload")},
        "routing": {"wake_id": record["draft"]["wake_id"]}
        if record["action"] == "confirm_wake_stopped"
        else {},
    }


def finish_action(
    app: App, actor_id: str, card_id: str, *, execution_id: str, outcome: str
) -> dict[str, Any]:
    if outcome not in {"succeeded", "failed", "uncertain"}:
        raise AssistantError("Use a fixed outcome; do not send credentials or response bodies.")
    with app.db.tx():
        record = _record(app, actor_id, card_id)
        if record.get("execution_id") != execution_id or not execution_id:
            raise AssistantConflict("This execution receipt does not match the action.")
        if record["status"] == outcome:
            return {"ok": True, "status": outcome}
        if record["status"] != "executing":
            raise AssistantConflict("The action already has a recorded outcome.")
        record.update(status=outcome, completed_at=app.clock.now())
        # The receipt deliberately has no free-text message or provider response field.
        app.db.kv_set(
            _prefix(actor_id) + card_id, json.dumps(record, ensure_ascii=False), now=app.clock.now()
        )
    app.live.publish({"type": "assistant_changed", "principal_id": actor_id})
    return {"ok": True, "status": outcome}


def dismiss_action(app: App, actor_id: str, card_id: str) -> dict[str, Any]:
    with app.db.tx():
        record = _record(app, actor_id, card_id)
        if record["status"] == "dismissed":
            return {"ok": True}
        if record["status"] != "proposed":
            raise AssistantConflict(
                "An execution with an unknown outcome cannot be dismissed or retried."
            )
        record["status"] = "dismissed"
        app.db.kv_set(
            _prefix(actor_id) + card_id, json.dumps(record, ensure_ascii=False), now=app.clock.now()
        )
    app.live.publish({"type": "assistant_changed", "principal_id": actor_id})
    return {"ok": True}


def _proposal_card(record: dict[str, Any]) -> dict[str, Any]:
    spec = BY_ACTION[record["action"]]
    pending = record["status"] == "proposed"
    return {
        "id": record["id"],
        "kind": "workspace_action",
        "title": spec.title,
        "summary": "表单已经预填好了。改一改就能提交。"
        if pending
        else "这个操作已经提交过。结果还没确认。",
        "status": record["status"],
        "space_id": record["space_id"],
        "data": {
            "action": record["action"],
            "target_id": record["target_id"],
            "draft": _clean(record["draft"]),
        },
        "actions": [{"id": "open", "label": "填写"}, {"id": "dismiss", "label": "暂不处理"}]
        if pending
        else [],
    }


def _card(
    identifier: str,
    kind: str,
    title: str,
    summary: str,
    status: str,
    data: dict[str, Any],
    actions: list[tuple[str, str]],
) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": kind,
        "title": title,
        "summary": summary,
        "status": status,
        "data": _clean(data),
        "actions": [{"id": key, "label": label} for key, label in actions],
    }


def resource_reviews(
    app: App, actor_id: str, *, include_cross_space_reviews: bool = False
) -> list[dict[str, Any]]:
    principal, _ = _identity(app, actor_id)
    if not principal.is_admin:
        return []
    # The owner of a computer may review another person's execution plan in their browser, but
    # that plan must never be copied into this space's model or tool history. Reviewing across
    # spaces takes an explicit request made from the browser, in the reviewer's own private
    # space. By default the model only ever sees this space's data.
    spaces = [app]
    if include_cross_space_reviews:
        assert app.identities is not None
        selected = app.identities.space(app.space_id)
        if selected.kind != "personal" or selected.owner_id != actor_id:
            raise AccessDenied("Cross-space resource reviews belong in your private assistant.")
        root = _root(app)
        spaces = root.collaboration.all_apps() if root.collaboration else [root]
    pending = []
    for space in spaces:
        if space.jobs is None:
            continue
        for task in space.jobs.list_tasks():
            for job in task.get("jobs", []):
                if job["state"] == "awaiting_approval" and job["approver_id"] == actor_id:
                    pending.append({"space_id": space.space_id, "title": task["title"], "job": job})
    return pending


def action_cards(
    app: App, actor_id: str, *, include_cross_space_reviews: bool = False
) -> dict[str, Any]:
    principal, member = _identity(app, actor_id)
    cards = []
    authority = app.actor_snapshot(actor_id, write=False)
    for record in _records(app, actor_id):
        if record["authority"] != authority or record["status"] not in {
            "proposed",
            "executing",
            "uncertain",
        }:
            continue
        if record["status"] == "proposed":
            if record["expires_at"] <= app.clock.now():
                continue
            try:
                _authorize_action(
                    app, actor_id, record["action"], record["target_id"], record["draft"]
                )
            except (AccessDenied, AssistantError, ExecutionError, OrchestrationError):
                continue
        cards.append(_proposal_card(record))
    for review in resource_reviews(
        app, actor_id, include_cross_space_reviews=include_cross_space_reviews
    ):
        job = review["job"]
        cards.append(
            _card(
                "job:" + job["id"],
                "execution_approval",
                review["title"],
                "这个电脑任务等你批准。",
                job["state"],
                {
                    "space_id": review["space_id"],
                    "job_id": job["id"],
                    "plan_hash": job["plan_hash"],
                    **job["plan"],
                    "job": job,
                },
                [("approve", "批准执行"), ("deny", "拒绝")],
            )
        )
    if app.jobs is not None:
        for task in app.jobs.list_tasks(limit=20):
            for question in task.get("questions", []):
                if question["state"] == "open" and question["task_revision"] == task["revision"]:
                    cards.append(
                        _card(
                            "question:" + question["id"],
                            "execution_question",
                            task["title"],
                            question["question"],
                            "waiting_input",
                            {"task": task, "question": question, "revision": task["revision"]},
                            [("answer", "发送回答")] if member.can_write else [],
                        )
                    )
            cards.append(
                _card(
                    "task:" + task["id"],
                    "task_status",
                    task["title"],
                    "电脑任务的进度和结果。",
                    task["state"],
                    {"task": task, "execution_kind": "execution"},
                    [],
                )
            )
    if app.orchestration is not None:
        for task in app.orchestration.list_tasks(actor_id)[:20]:
            if task["state"] == "planned":
                actions = (
                    [("approve", "确认并派发"), ("cancel", "取消这项计划")]
                    if task.get("can_approve")
                    else []
                )
                cards.append(
                    _card(
                        "agent-plan:" + task["id"],
                        "agent_plan",
                        task["title"],
                        "这份协作计划等你确认。",
                        task["state"],
                        {"task": task},
                        actions,
                    )
                )
            for step in task["steps"]:
                attempt = step.get("attempt")
                if attempt and step["state"] == "result_reported":
                    actions = []
                    if task.get("can_manage"):
                        if (
                            task.get("can_accept")
                            and attempt.get("result_eligible")
                            and (attempt.get("result") or {}).get("outcome") == "succeeded"
                        ):
                            actions.append(("accept", "验收通过"))
                        actions.append(("reject", "未通过"))
                    cards.append(
                        _card(
                            "agent-result:" + attempt["id"],
                            "agent_result",
                            task["title"],
                            "助手交回了结果。等你验收。",
                            attempt["state"],
                            {"task": task, "step": step, "attempt": attempt},
                            actions,
                        )
                    )
            for question in task.get("questions", []):
                if question["state"] == "open":
                    cards.append(
                        _card(
                            "agent-question:" + task["id"] + ":" + question["id"],
                            "agent_question",
                            task["title"],
                            question["text"],
                            "waiting_input",
                            {"task": task, "question": question},
                            [("answer", "发送回答")] if task.get("can_manage") else [],
                        )
                    )
            if task["state"] != "planned":
                cards.append(
                    _card(
                        "agent-task:" + task["id"],
                        "task_status",
                        task["title"],
                        "协作任务的进度和结果。",
                        task["state"],
                        {"task": task, "execution_kind": "agent"},
                        [],
                    )
                )
    assert app.identities is not None
    return {
        "space_id": app.space_id,
        "principal": _clean(asdict(principal)),
        "space": _clean(asdict(app.identities.space(app.space_id))),
        "role": member.role,
        "cards": cards,
    }


def inspect_workspace(
    app: App,
    actor_id: str,
    *,
    topic: str = "overview",
    resource_id: str = "",
    query: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    principal, member = _identity(app, actor_id)
    if (
        topic not in TOPICS
        or type(limit) is not int
        or not 1 <= limit <= 50
        or len(query) > 200
        or len(resource_id) > 128
    ):
        raise AssistantError("Use a supported topic, a bounded identifier/query and limit 1-50.")
    root = _root(app)
    assert app.identities is not None
    value: Any
    if topic == "spaces":
        value = [asdict(space) for space in app.identities.spaces_for(actor_id)]
    elif topic == "members":
        value = app.identities.members(actor_id, app.space_id)
    elif topic == "devices":
        value = [asdict(device) for device in root.web_auth.sessions(principal_id=actor_id)]
    elif topic == "channels":
        value = app.identities.channel_bindings(actor_id, app.space_id)
    elif topic == "nodes":
        value = (
            []
            if root.nodes is None
            else [
                node.to_dict()
                for node in root.nodes.list()
                if root.nodes.authorize(node.id, app.space_id)
            ]
        )
    elif topic == "tasks":
        value = (
            []
            if app.jobs is None
            else app.jobs.get_task(resource_id)
            if resource_id
            else app.jobs.list_tasks(limit=limit, summaries=True)
        )
    elif topic == "execution_logs":
        if app.jobs is None or not resource_id:
            raise AssistantError("Choose an execution attempt to inspect.")
        value = app.jobs.logs(resource_id, after=-1)
    elif topic in {"agents", "agent_tasks", "agent_occupancy", "agent_events"}:
        store = app.orchestration
        if store is None:
            raise AssistantError("Agent collaboration is unavailable.")
        if topic == "agents":
            value = (
                store.get_agent(resource_id, actor_id)
                if resource_id
                else store.list_agents(actor_id)[:limit]
            )
        elif topic == "agent_tasks":
            value = (
                store.get_task(resource_id, actor_id)
                if resource_id
                else [
                    {
                        key: task[key]
                        for key in (
                            "id",
                            "title",
                            "state",
                            "visibility",
                            "revision",
                            "created_at",
                            "updated_at",
                        )
                    }
                    for task in store.list_tasks(actor_id)[:limit]
                ]
            )
        elif topic == "agent_occupancy":
            value = store.owner_occupancy(resource_id, actor_id)
        else:
            value = store.events(resource_id, actor_id, after=0)
    elif topic in {"knowledge", "knowledge_history"}:
        prefix = "/memories/state/knowledge/"
        if resource_id and _DOCUMENT.fullmatch(resource_id) is None:
            raise AssistantError("Use a simple Markdown filename.")
        if topic == "knowledge_history":
            if not resource_id:
                raise AssistantError("Choose a document filename.")
            value = [
                {
                    "id": row["id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "created_at": row["created_at"],
                    "text": (row.get("new_content") or b"").decode()
                    if isinstance(row.get("new_content"), bytes)
                    else str(row.get("new_content") or ""),
                }
                for row in app.memory.history(prefix + resource_id, limit=limit)
            ]
        else:
            value = []
            for row in app.db.query(
                "SELECT path,revision,content FROM memory_documents WHERE path LIKE ? AND content IS NOT NULL ORDER BY path",
                (prefix + "%",),
            ):
                name = str(row["path"])[len(prefix) :]
                content = (
                    row["content"].decode()
                    if isinstance(row["content"], bytes)
                    else str(row["content"])
                )
                if (not resource_id or name == resource_id) and (
                    not query or query.casefold() in (name + content).casefold()
                ):
                    value.append(
                        {
                            "path": name,
                            "revision": row["revision"],
                            "text": content[:12000] if resource_id else content[:600],
                            "truncated": len(content) > (12000 if resource_id else 600),
                        }
                    )
                if len(value) >= limit:
                    break
    elif topic == "reviews":
        if not principal.is_admin:
            raise AccessDenied("Resource review requires the instance administrator.")
        value = resource_reviews(app, actor_id)
    elif topic == "cards":
        value = action_cards(app, actor_id)["cards"]
    elif topic == "diagnostics":
        apps = root.collaboration.all_apps() if root.collaboration else [root]
        spent = sum(space.budget.spent_today() for space in apps)
        value = {
            "budget": {
                "space_spent_usd": app.budget.spent_today(),
                "instance_remaining_usd": max(0, root.budget.daily_limit() - spent),
                "instance_limit_usd": root.budget.daily_limit(),
            },
            "outbox": app.outbox.counts(),
        }
    else:
        value = {
            "principal": asdict(principal),
            "space": asdict(app.identities.space(app.space_id)),
            "role": member.role,
            "spaces": [asdict(space) for space in app.identities.spaces_for(actor_id)],
            "tasks": inspect_workspace(app, actor_id, topic="tasks", limit=limit)["data"],
            "agent_tasks": inspect_workspace(app, actor_id, topic="agent_tasks", limit=limit)[
                "data"
            ],
            "diagnostics": inspect_workspace(app, actor_id, topic="diagnostics")["data"],
        }
    return {"space_id": app.space_id, "topic": topic, "data": _clean(value)}
