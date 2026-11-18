"""Durable plans, grants and fenced attempts for remote personal agents."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
from collections.abc import Callable
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.db import Database, loads
from personal_agent.orchestration.models import (
    ACTIVE_ATTEMPTS,
    AGENT_KINDS,
    AVAILABILITIES,
    MAX_RESULT_CHARS,
    STEP_TERMINAL,
    TASK_TERMINAL,
    WAKE_MODES,
    OrchestrationAccessDenied,
    OrchestrationConflict,
    OrchestrationError,
    OrchestrationNotFound,
    artifact_metadata,
    bounded_text,
    canonical,
    digest,
    duration,
    event_data,
    identifier,
    secret_references,
    steps_plan,
    string_list,
)

AuthorizationSnapshot = Callable[[str], dict[str, int]]


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


class OrchestrationStore:
    def __init__(
        self,
        db: Database,
        clock: Clock | None = None,
        authorization_snapshot: AuthorizationSnapshot | None = None,
        *,
        space_id: str | None = None,
        space_owner_id: str | None = None,
        read_authorization_snapshot: AuthorizationSnapshot | None = None,
    ) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self.authorization_snapshot = authorization_snapshot
        self.read_authorization_snapshot = read_authorization_snapshot
        self.space_id = space_id
        self.space_owner_id = space_owner_id

    def _snapshot(self, actor_id: str, *, write: bool = True) -> dict[str, int]:
        identifier(actor_id, "actor ID")
        callback = (
            self.authorization_snapshot
            if write
            else (self.read_authorization_snapshot or self.authorization_snapshot)
        )
        if callback is None:
            return {}
        try:
            value = callback(actor_id)
        except Exception as exc:
            raise OrchestrationAccessDenied("Current space membership is required.") from exc
        if not isinstance(value, dict) or any(
            not isinstance(k, str) or type(v) is not int for k, v in value.items()
        ):
            raise OrchestrationAccessDenied("Invalid authorization snapshot.")
        return dict(value)

    def _matches(self, actor_id: str, saved: str | None) -> bool:
        try:
            return bool(self._snapshot(actor_id) == loads(saved, {}))
        except OrchestrationAccessDenied:
            return False

    def _agent(self, agent_id: str) -> sqlite3.Row:
        row = self.db.one("SELECT * FROM orchestration_agents WHERE id=?", (agent_id,))
        if row is None:
            raise OrchestrationNotFound("Agent not found.")
        return row

    def _task(self, task_id: str) -> sqlite3.Row:
        row = self.db.one("SELECT * FROM orchestration_tasks WHERE id=?", (task_id,))
        if row is None:
            raise OrchestrationNotFound("Task not found.")
        return row

    def _step(self, task_id: str, step_id: str) -> sqlite3.Row:
        row = self.db.one(
            "SELECT * FROM orchestration_steps WHERE task_id=? AND id=?", (task_id, step_id)
        )
        if row is None:
            raise OrchestrationNotFound("Step not found.")
        return row

    def _agent_current(self, row: sqlite3.Row) -> bool:
        return row["state"] == "active" and self._matches(
            row["owner_id"], row["owner_authorization"]
        )

    def _can_use_agent(self, row: sqlite3.Row, actor_id: str) -> bool:
        return self._agent_current(row) and (
            actor_id == row["owner_id"] or actor_id in loads(row["allowed_actor_ids"], [])
        )

    def _require_agent_owner(self, row: sqlite3.Row, actor_id: str) -> None:
        self._snapshot(actor_id)
        if row["owner_id"] != actor_id:
            raise OrchestrationAccessDenied("Only the agent owner can change its grants.")

    def _view_agent(self, row: sqlite3.Row, *, owner: bool = False) -> dict[str, Any]:
        result = {
            key: row[key]
            for key in (
                "id",
                "owner_id",
                "name",
                "kind",
                "availability",
                "wake_mode",
                "state",
                "generation",
                "runtime_version",
                "last_seen_at",
                "created_at",
                "updated_at",
            )
        }
        result.update(
            shared_environment=bool(row["shared_environment"]),
            capabilities=loads(row["capabilities"], []),
            observed_capabilities=loads(row["observed_capabilities"], []),
        )
        # A grant to use someone else's agent does not reveal their other grants or env refs.
        if owner:
            result["allowed_actor_ids"] = loads(row["allowed_actor_ids"], [])
            result["secret_env"] = loads(row["secret_env"], {})
        return result

    def _require_task_access(
        self, task: sqlite3.Row, actor_id: str, *, write: bool = False
    ) -> None:
        snapshot = self._snapshot(actor_id, write=write)
        if actor_id == task["actor_id"]:
            if snapshot != loads(task["authorization"], {}) and (
                write or task["visibility"] != "space"
            ):
                raise OrchestrationAccessDenied("This task belongs to an earlier authorization.")
            return
        if task["visibility"] != "space":
            raise OrchestrationNotFound("Task not found.")
        if write and actor_id != self.space_owner_id:
            raise OrchestrationAccessDenied(
                "Only the requester or space owner can change this task."
            )

    def _task_authorized(self, task: sqlite3.Row) -> bool:
        if not self._matches(task["actor_id"], task["authorization"]):
            return False
        if task["approved_by"] and not self._matches(
            task["approved_by"], task["approver_authorization"]
        ):
            return False
        for step in self.db.query(
            "SELECT * FROM orchestration_steps WHERE task_id=?", (task["id"],)
        ):
            agent = self._agent(step["agent_id"])
            if agent["generation"] != step["agent_generation"] or not self._can_use_agent(
                agent, task["actor_id"]
            ):
                return False
            if (
                task["visibility"] == "private"
                and agent["shared_environment"]
                and agent["owner_id"] != task["actor_id"]
            ):
                return False
        return True

    def authorized(self, task_id: str) -> bool:
        return self._task_authorized(self._task(task_id))

    def _event(
        self,
        task_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
        attempt_id: str | None = None,
        sequence: int | None = None,
        event_hash: str | None = None,
    ) -> int:
        cursor = self.db.insert(
            """INSERT INTO orchestration_events
               (task_id,step_id,attempt_id,event_type,data,worker_sequence,event_hash,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                task_id,
                step_id,
                attempt_id,
                event_type,
                canonical(data or {}),
                sequence,
                event_hash,
                self.clock.now(),
            ),
        )
        assert cursor is not None
        return cursor

    # ---- agent ownership, grants and single-display credentials -----------------------
    def create_agent(
        self,
        actor_id: str,
        *,
        name: str,
        kind: str,
        allowed_actor_ids: list[str] | None = None,
        shared_environment: bool = False,
        capabilities: list[str] | None = None,
        availability: str = "unknown",
        secret_env: dict[str, str] | None = None,
        wake_mode: str = "manual",
    ) -> dict[str, Any]:
        snapshot = self._snapshot(actor_id)
        name = bounded_text(name, "agent name", 200)
        if (
            not isinstance(kind, str)
            or kind not in AGENT_KINDS
            or not isinstance(availability, str)
            or availability not in AVAILABILITIES
            or not isinstance(wake_mode, str)
            or wake_mode not in WAKE_MODES
        ):
            raise OrchestrationError("Invalid agent kind, availability or wake mode.")
        if not isinstance(shared_environment, bool):
            raise OrchestrationError("Shared environment must be a boolean.")
        if kind == "grok":
            shared_environment = True
        allowed = string_list(
            [] if allowed_actor_ids is None else allowed_actor_ids, "allowed actor ID"
        )
        declared = string_list([] if capabilities is None else capabilities, "capability")
        refs = secret_references({} if secret_env is None else secret_env)
        agent_id = _id("ag")
        token = "pa4_" + secrets.token_urlsafe(32)
        now = self.clock.now()
        with self.db.tx():
            self.db.execute(
                """INSERT INTO orchestration_agents
                   (id,owner_id,name,kind,allowed_actor_ids,shared_environment,capabilities,
                    availability,secret_env,wake_mode,token_hash,owner_authorization,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    agent_id,
                    actor_id,
                    name,
                    kind,
                    canonical(allowed),
                    int(shared_environment),
                    canonical(declared),
                    availability,
                    canonical(refs),
                    wake_mode,
                    hashlib.sha256(token.encode()).hexdigest(),
                    canonical(snapshot),
                    now,
                    now,
                ),
            )
        return {"agent": self._view_agent(self._agent(agent_id), owner=True), "worker_token": token}

    def get_agent(self, agent_id: str, actor_id: str) -> dict[str, Any]:
        self._snapshot(actor_id, write=False)
        agent = self._agent(agent_id)
        if agent["owner_id"] != actor_id and not self._can_use_agent(agent, actor_id):
            raise OrchestrationNotFound("Agent not found.")
        return self._view_agent(agent, owner=agent["owner_id"] == actor_id)

    def list_agents(self, actor_id: str) -> list[dict[str, Any]]:
        self._snapshot(actor_id, write=False)
        return [
            self._view_agent(row, owner=row["owner_id"] == actor_id)
            for row in self.db.query("SELECT * FROM orchestration_agents ORDER BY created_at,id")
            if row["owner_id"] == actor_id or self._can_use_agent(row, actor_id)
        ]

    def update_agent(self, agent_id: str, actor_id: str, **changes: Any) -> dict[str, Any]:
        allowed_fields = {
            "name",
            "kind",
            "allowed_actor_ids",
            "shared_environment",
            "capabilities",
            "availability",
            "secret_env",
            "wake_mode",
        }
        if not changes or set(changes) - allowed_fields:
            raise OrchestrationError("Invalid agent update fields.")
        with self.db.tx():
            row = self._agent(agent_id)
            self._require_agent_owner(row, actor_id)
            if row["state"] != "active":
                raise OrchestrationConflict("A revoked agent cannot be changed.")
            values = {key: row[key] for key in allowed_fields}
            for key, value in changes.items():
                if key == "name":
                    value = bounded_text(value, "agent name", 200)
                elif key in {"allowed_actor_ids", "capabilities"}:
                    value = canonical(string_list(value, key))
                elif key == "secret_env":
                    value = canonical(secret_references(value))
                elif key == "shared_environment":
                    if not isinstance(value, bool):
                        raise OrchestrationError("Shared environment must be a boolean.")
                    value = int(value)
                elif (
                    (key == "kind" and value not in AGENT_KINDS)
                    or (key == "availability" and value not in AVAILABILITIES)
                    or (key == "wake_mode" and value not in WAKE_MODES)
                ):
                    raise OrchestrationError("Invalid agent declaration.")
                values[key] = value
            if values["kind"] == "grok":
                values["shared_environment"] = 1
            fields = sorted(values)
            self.db.execute(
                "UPDATE orchestration_agents SET "
                + ",".join(f"{key}=?" for key in fields)
                + ",generation=generation+1,owner_authorization=?,updated_at=? WHERE id=?",
                [
                    *(values[key] for key in fields),
                    canonical(self._snapshot(actor_id)),
                    self.clock.now(),
                    agent_id,
                ],
            )
            self._fence_agent_tasks(agent_id, "agent_configuration_changed")
            return self._view_agent(self._agent(agent_id), owner=True)

    def rotate_agent_token(self, agent_id: str, actor_id: str) -> dict[str, Any]:
        with self.db.tx():
            row = self._agent(agent_id)
            self._require_agent_owner(row, actor_id)
            if row["state"] != "active":
                raise OrchestrationConflict("A revoked agent cannot issue a credential.")
            token = "pa4_" + secrets.token_urlsafe(32)
            self.db.execute(
                """UPDATE orchestration_agents SET token_hash=?,generation=generation+1,
                   owner_authorization=?,updated_at=? WHERE id=?""",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    canonical(self._snapshot(actor_id)),
                    self.clock.now(),
                    agent_id,
                ),
            )
            self._fence_agent_tasks(agent_id, "agent_credential_rotated")
            return {
                "agent": self._view_agent(self._agent(agent_id), owner=True),
                "worker_token": token,
            }

    def revoke_agent(self, agent_id: str, actor_id: str) -> dict[str, Any]:
        with self.db.tx():
            row = self._agent(agent_id)
            self._require_agent_owner(row, actor_id)
            if row["state"] != "revoked":
                self.db.execute(
                    """UPDATE orchestration_agents SET state='revoked',generation=generation+1,
                       updated_at=? WHERE id=?""",
                    (self.clock.now(), agent_id),
                )
                self._fence_agent_tasks(agent_id, "agent_revoked")
            return self._view_agent(self._agent(agent_id), owner=True)

    def authenticate_worker(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            raise OrchestrationAccessDenied("Invalid worker credential.")
        row = self.db.one(
            "SELECT * FROM orchestration_agents WHERE token_hash=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        if row is None or not self._agent_current(row):
            raise OrchestrationAccessDenied("Invalid worker credential.")
        return self._view_agent(row)

    def hello(
        self,
        agent_id: str,
        *,
        capabilities: list[str] | None = None,
        runtime_version: str | None = None,
    ) -> dict[str, Any]:
        declared = string_list([] if capabilities is None else capabilities, "observed capability")
        if runtime_version is not None:
            bounded_text(runtime_version, "runtime version", 200)
        with self.db.tx():
            if not self._agent_current(self._agent(agent_id)):
                raise OrchestrationAccessDenied("Agent authorization was revoked.")
            self.db.execute(
                """UPDATE orchestration_agents SET observed_capabilities=?,runtime_version=?,
                   last_seen_at=?,availability='online',updated_at=? WHERE id=?""",
                (
                    canonical(declared),
                    runtime_version,
                    self.clock.now(),
                    self.clock.now(),
                    agent_id,
                ),
            )
            return self._view_agent(self._agent(agent_id))

    def worker_status(self, agent_id: str) -> dict[str, Any]:
        self.reconcile()
        row = self._agent(agent_id)
        if not self._agent_current(row):
            raise OrchestrationAccessDenied("Agent authorization was revoked.")
        active = self.db.query(
            """SELECT * FROM orchestration_attempts WHERE agent_id=? AND agent_generation=?
               AND state IN ('claimed','running','waiting_input','uncertain') ORDER BY claimed_at""",
            (agent_id, row["generation"]),
        )
        return {
            "agent": self._view_agent(row),
            "attempts": [self._view_attempt(a, worker=True, payload=False) for a in active],
        }

    # ---- immutable plans and explicit approval ---------------------------------------
    def create_task(
        self,
        actor_id: str,
        *,
        request_id: str,
        title: str,
        steps: list[dict[str, Any]],
        deadline_at: float | None = None,
        visibility: str = "private",
    ) -> dict[str, Any]:
        snapshot = self._snapshot(actor_id)
        request_id = identifier(request_id, "request ID")
        title = bounded_text(title, "task title", 300)
        clean = steps_plan(steps)
        if not isinstance(visibility, str) or visibility not in {"private", "space"}:
            raise OrchestrationError("Invalid task visibility.")
        if deadline_at is not None and (
            not isinstance(deadline_at, int | float)
            or isinstance(deadline_at, bool)
            or not math.isfinite(deadline_at)
        ):
            raise OrchestrationError("Invalid task deadline.")
        # The first receipt owns its deadline. HTTP clients commonly turn a relative
        # timeout into now+timeout again after losing the creation response.
        request_hash = digest({"title": title, "steps": clean, "visibility": visibility})
        with self.db.tx():
            existing = self.db.one(
                "SELECT * FROM orchestration_tasks WHERE actor_id=? AND request_id=?",
                (actor_id, request_id),
            )
            if existing is not None:
                self._require_task_access(existing, actor_id)
                if existing["request_hash"] != request_hash:
                    raise OrchestrationConflict("Request ID already belongs to another plan.")
                return self._view_task(existing, actor_id=actor_id)
            now = self.clock.now()
            deadline = now + 86400 if deadline_at is None else float(deadline_at)
            if not now < deadline <= now + 30 * 86400:
                raise OrchestrationError("The deadline must be within the next 30 days.")
            for step in clean:
                agent = self._agent(step["agent_id"])
                if not self._can_use_agent(agent, actor_id):
                    raise OrchestrationAccessDenied(
                        "The requester is not authorized to use a selected agent."
                    )
                if (
                    visibility == "private"
                    and agent["shared_environment"]
                    and agent["owner_id"] != actor_id
                ):
                    raise OrchestrationAccessDenied(
                        "A private task cannot use another owner's shared environment."
                    )
                step["agent_generation"] = agent["generation"]
            task_id = _id("task")
            plan = {
                "revision": 1,
                "title": title,
                "steps": clean,
                "deadline_at": deadline,
                "visibility": visibility,
            }
            plan_hash = digest(plan)
            self.db.execute(
                """INSERT INTO orchestration_tasks
                   (id,actor_id,request_id,request_hash,title,visibility,plan_hash,plan,authorization,
                    state,deadline_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'planned',?,?,?)""",
                (
                    task_id,
                    actor_id,
                    request_id,
                    request_hash,
                    title,
                    visibility,
                    plan_hash,
                    canonical(plan),
                    canonical(snapshot),
                    deadline,
                    now,
                    now,
                ),
            )
            for position, step in enumerate(clean):
                self.db.execute(
                    """INSERT INTO orchestration_steps
                       (task_id,id,position,agent_id,agent_generation,prompt,acceptance,depends_on,required,state)
                       VALUES (?,?,?,?,?,?,?,?,?,'planned')""",
                    (
                        task_id,
                        step["id"],
                        position,
                        step["agent_id"],
                        step["agent_generation"],
                        step["prompt"],
                        step["acceptance"],
                        canonical(step["depends_on"]),
                        int(step["required"]),
                    ),
                )
            self._event(task_id, "task.created", {"revision": 1, "plan_hash": plan_hash})
            return self._view_task(self._task(task_id), actor_id=actor_id)

    def _require_plan(self, task: sqlite3.Row, revision: int, plan_hash: str) -> None:
        if (
            type(revision) is not int
            or revision != task["revision"]
            or plan_hash != task["plan_hash"]
        ):
            raise OrchestrationConflict("The plan revision or hash is stale.")

    def approve_task(
        self, task_id: str, actor_id: str, *, revision: int, plan_hash: str
    ) -> dict[str, Any]:
        self.reconcile()
        with self.db.tx():
            task = self._task(task_id)
            self._require_task_access(task, actor_id, write=True)
            self._require_plan(task, revision, plan_hash)
            if not self._task_authorized(task):
                raise OrchestrationConflict("A plan authorization changed; create a new plan.")
            if task["approved_at"] is not None:
                return self._view_task(task, actor_id=actor_id)
            if task["state"] != "planned" or task["deadline_at"] <= self.clock.now():
                raise OrchestrationConflict("This plan can no longer be approved.")
            self.db.execute(
                """UPDATE orchestration_tasks SET state='queued',approved_by=?,approver_authorization=?,
                   approved_at=?,updated_at=? WHERE id=?""",
                (
                    actor_id,
                    canonical(self._snapshot(actor_id)),
                    self.clock.now(),
                    self.clock.now(),
                    task_id,
                ),
            )
            self.db.execute(
                "UPDATE orchestration_steps SET state='queued' WHERE task_id=?", (task_id,)
            )
            self._event(
                task_id,
                "task.approved",
                {"actor_id": actor_id, "revision": revision, "plan_hash": plan_hash},
            )
            return self._view_task(self._task(task_id), actor_id=actor_id)

    def get_task(self, task_id: str, actor_id: str) -> dict[str, Any]:
        self.reconcile()
        task = self._task(task_id)
        self._require_task_access(task, actor_id)
        return self._view_task(task, actor_id=actor_id)

    def list_tasks(self, actor_id: str) -> list[dict[str, Any]]:
        self.reconcile()
        self._snapshot(actor_id, write=False)
        result = []
        for row in self.db.query(
            "SELECT * FROM orchestration_tasks WHERE actor_id=? OR visibility='space' ORDER BY created_at DESC,id",
            (actor_id,),
        ):
            try:
                self._require_task_access(row, actor_id)
            except (OrchestrationAccessDenied, OrchestrationNotFound):
                continue
            result.append(self._view_task(row, actor_id=actor_id))
        return result

    def _question_view(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["choices"] = loads(row["choices"], [])
        return result

    def _view_task(self, task: sqlite3.Row, *, actor_id: str | None = None) -> dict[str, Any]:
        result = {
            key: task[key]
            for key in (
                "id",
                "actor_id",
                "request_id",
                "title",
                "visibility",
                "revision",
                "plan_hash",
                "state",
                "desired_state",
                "stop_reason",
                "approved_by",
                "approved_at",
                "deadline_at",
                "created_at",
                "updated_at",
            )
        }
        result["space_id"] = self.space_id
        result["steps"] = []
        for step in self.db.query(
            "SELECT * FROM orchestration_steps WHERE task_id=? ORDER BY position", (task["id"],)
        ):
            view = {
                key: step[key]
                for key in (
                    "id",
                    "agent_id",
                    "agent_generation",
                    "prompt",
                    "acceptance",
                    "state",
                    "generation",
                    "current_attempt",
                    "accepted_by",
                    "accepted_at",
                    "rejection_reason",
                )
            }
            view["required"] = bool(step["required"])
            recipient = self._agent(step["agent_id"])
            view.update(
                agent_name=recipient["name"],
                agent_kind=recipient["kind"],
                agent_owner_id=recipient["owner_id"],
                shared_environment=bool(recipient["shared_environment"]),
            )
            view["depends_on"] = loads(step["depends_on"], [])
            attempt = self.db.one(
                "SELECT * FROM orchestration_attempts WHERE id=?", (step["current_attempt"],)
            )
            view["attempt"] = None if attempt is None else self._view_attempt(attempt)
            result["steps"].append(view)
        result["questions"] = [
            self._question_view(row)
            for row in self.db.query(
                "SELECT * FROM orchestration_questions WHERE task_id=? ORDER BY created_at,id",
                (task["id"],),
            )
        ]
        result["wakes"] = [
            {
                **self._view_wake(row),
                "can_confirm_stopped": bool(
                    actor_id is not None
                    and row["state"] in {"sent", "uncertain"}
                    and row["claimed_at"] is None
                    and self._agent(row["agent_id"])["owner_id"] == actor_id
                ),
            }
            for row in self.db.query(
                "SELECT * FROM orchestration_wakes WHERE task_id=? ORDER BY reserved_at,id",
                (task["id"],),
            )
        ]
        can_manage = False
        if actor_id is not None:
            try:
                self._require_task_access(task, actor_id, write=True)
                can_manage = True
            except (OrchestrationAccessDenied, OrchestrationNotFound):
                pass
        for step in result["steps"]:
            attempt = step["attempt"]
            step["can_confirm_stopped"] = bool(
                actor_id is not None
                and self._agent(step["agent_id"])["owner_id"] == actor_id
                and attempt
                and (
                    attempt["state"] == "uncertain"
                    or (
                        attempt["desired_state"] == "cancel" and attempt["state"] in ACTIVE_ATTEMPTS
                    )
                )
            )
        authorized = can_manage and self._task_authorized(task)
        result["can_manage"] = can_manage
        result["can_approve"] = authorized and task["state"] == "planned"
        result["can_accept"] = (
            authorized
            and task["desired_state"] == "run"
            and any(
                step["state"] == "result_reported"
                and step["attempt"] is not None
                and step["attempt"]["result_eligible"]
                and step["attempt"]["result"].get("outcome") == "succeeded"
                for step in result["steps"]
            )
        )
        return result

    # ---- single-admission claims, renewable leases and worker receipts ----------------
    def _view_attempt(
        self,
        attempt: sqlite3.Row,
        *,
        worker: bool = False,
        payload: bool = False,
    ) -> dict[str, Any]:
        result = {
            key: attempt[key]
            for key in (
                "task_id",
                "step_id",
                "agent_id",
                "agent_generation",
                "worker_id",
                "generation",
                "lease_expires_at",
                "state",
                "desired_state",
                "last_sequence",
                "claimed_at",
                "last_seen_at",
                "finished_at",
                "result_received_at",
                "stopped_by",
                "stopped_at",
                "stop_note",
            )
        }
        result["attempt_id"] = attempt["id"]
        result["id"] = attempt["id"]
        result["result"] = loads(attempt["result"])
        result["result_eligible"] = bool(attempt["result_eligible"])
        result["desired_cancel"] = attempt["desired_state"] == "cancel"
        if worker:
            result["lease_token"] = attempt["lease_token"]
            task = self._task(attempt["task_id"])
            current = self._task_authorized(task) and task["state"] != "revoked"
            result["questions"] = (
                []
                if not current
                else [
                    self._question_view(row)
                    for row in self.db.query(
                        "SELECT * FROM orchestration_questions WHERE attempt_id=? ORDER BY created_at,id",
                        (attempt["id"],),
                    )
                ]
            )
            if payload and current:
                step = self._step(attempt["task_id"], attempt["step_id"])
                dependencies = []
                for dep_id in loads(step["depends_on"], []):
                    dep = self._step(attempt["task_id"], dep_id)
                    if dep["state"] != "accepted":
                        raise OrchestrationConflict("A dependency is no longer accepted.")
                    receipt = self.db.row(
                        "SELECT result FROM orchestration_attempts WHERE id=?",
                        (dep["current_attempt"],),
                    )
                    accepted = loads(receipt["result"], {})
                    dependencies.append(
                        {
                            "step_id": dep_id,
                            "text": accepted.get("text", ""),
                            "artifacts": accepted.get("artifacts", []),
                        }
                    )
                result["payload"] = {
                    "prompt": step["prompt"],
                    "acceptance": step["acceptance"],
                    "requester_id": task["actor_id"],
                    "agent_owner_id": self._agent(attempt["agent_id"])["owner_id"],
                    "shared_environment": bool(
                        self._agent(attempt["agent_id"])["shared_environment"]
                    ),
                    "visibility": task["visibility"],
                    "dependencies": dependencies,
                    "plan_hash": task["plan_hash"],
                    "revision": task["revision"],
                    "deadline_at": task["deadline_at"],
                    "space_id": self.space_id,
                }
        return result

    def _worker_attempt(
        self,
        attempt_id: str,
        agent_id: str,
        *,
        generation: int | None = None,
        lease_token: str | None = None,
        require_lease: bool = False,
    ) -> sqlite3.Row:
        if require_lease and (
            type(generation) is not int or not isinstance(lease_token, str) or not lease_token
        ):
            raise OrchestrationConflict("Current generation and lease token are required.")
        attempt = self.db.one(
            "SELECT * FROM orchestration_attempts WHERE id=? AND agent_id=?", (attempt_id, agent_id)
        )
        if attempt is None:
            raise OrchestrationNotFound("Attempt not found.")
        agent = self._agent(agent_id)
        if not self._agent_current(agent):
            raise OrchestrationAccessDenied("Agent authorization was revoked.")
        step = self._step(attempt["task_id"], attempt["step_id"])
        if agent["generation"] != attempt["agent_generation"] or (
            step["current_attempt"] != attempt_id or step["generation"] != attempt["generation"]
        ):
            raise OrchestrationConflict("The attempt was fenced by a newer authorization.")
        if generation is not None and (
            type(generation) is not int or generation != attempt["generation"]
        ):
            raise OrchestrationConflict("Stale attempt generation.")
        if lease_token is not None and (
            not isinstance(lease_token, str)
            or not hmac.compare_digest(lease_token.encode(), attempt["lease_token"].encode())
        ):
            raise OrchestrationConflict("Stale attempt lease.")
        return attempt

    def _require_live(self, attempt: sqlite3.Row) -> sqlite3.Row:
        task = self._task(attempt["task_id"])
        if not self._task_authorized(task):
            raise OrchestrationConflict("Task authorization changed.")
        if (
            attempt["state"] not in ACTIVE_ATTEMPTS
            or attempt["lease_expires_at"] <= self.clock.now()
        ):
            raise OrchestrationConflict("The lease is no longer live; execution must not restart.")
        if task["desired_state"] == "cancel" or task["deadline_at"] <= self.clock.now():
            raise OrchestrationConflict("Cancellation was requested.")
        return task

    def _ready_steps(
        self, agent_id: str | None = None, task_id: str | None = None
    ) -> list[sqlite3.Row]:
        rows = self.db.query(
            """SELECT s.* FROM orchestration_steps s JOIN orchestration_tasks t ON t.id=s.task_id
               WHERE s.state='queued' AND t.approved_at IS NOT NULL AND t.desired_state='run'
               AND t.state NOT IN ('succeeded','failed','cancelled','expired','revoked','uncertain')
               AND t.deadline_at>? AND (? IS NULL OR s.agent_id=?) AND (? IS NULL OR s.task_id=?)
               ORDER BY t.created_at,s.position,t.id""",
            (self.clock.now(), agent_id, agent_id, task_id, task_id),
        )
        return [
            row
            for row in rows
            if all(
                self._step(row["task_id"], dep)["state"] == "accepted"
                for dep in loads(row["depends_on"], [])
            )
        ]

    def claim(
        self,
        agent_id: str,
        worker_id: str,
        *,
        task_id: str | None = None,
        lease_seconds: float = 300,
    ) -> dict[str, Any]:
        """worker_id is a durable *claim nonce*, reused only to recover that admission.

        This gives one admitted attempt, not exactly-once external execution. The worker
        must durably record the attempt before starting a provider and reuse that receipt.
        """
        worker_id = identifier(worker_id, "worker claim nonce")
        lease = duration(lease_seconds)
        with self.db.tx():
            self._reconcile_locked()
            agent = self._agent(agent_id)
            if not self._agent_current(agent):
                raise OrchestrationAccessDenied("Agent authorization was revoked.")
            existing = self.db.one(
                "SELECT * FROM orchestration_attempts WHERE agent_id=? AND worker_id=?",
                (agent_id, worker_id),
            )
            if existing is not None:
                existing = self._worker_attempt(existing["id"], agent_id)
                if task_id is not None and task_id != existing["task_id"]:
                    raise OrchestrationConflict("Claim nonce already belongs to another task.")
                return {"attempt": self._view_attempt(existing, worker=True, payload=True)}
            # One unresolved execution per identity; an expired worker may still be running.
            if self.db.scalar(
                """SELECT count(*) FROM orchestration_attempts WHERE agent_id=?
                   AND state IN ('claimed','running','waiting_input','uncertain')""",
                (agent_id,),
            ):
                return {"attempt": None}
            for step in self._ready_steps(agent_id, task_id):
                task = self._task(step["task_id"])
                if not self._task_authorized(task):
                    self._invalidate_task(task, "authorization_changed")
                    continue
                now = self.clock.now()
                attempt_id = _id("attempt")
                # A claim ID, not a standalone credential. The separately hashed bearer
                # credential and current agent/task generations authorize every use.
                claim_id = secrets.token_hex(24)
                generation = step["generation"] + 1
                self.db.execute(
                    """INSERT INTO orchestration_attempts
                       (id,task_id,step_id,agent_id,agent_generation,worker_id,generation,lease_token,
                        lease_expires_at,state,claimed_at,last_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?,'claimed',?,?)""",
                    (
                        attempt_id,
                        task["id"],
                        step["id"],
                        agent_id,
                        agent["generation"],
                        worker_id,
                        generation,
                        claim_id,
                        min(now + lease, task["deadline_at"]),
                        now,
                        now,
                    ),
                )
                self.db.execute(
                    """UPDATE orchestration_steps SET state='claimed',generation=?,current_attempt=?
                       WHERE task_id=? AND id=? AND state='queued'""",
                    (generation, attempt_id, task["id"], step["id"]),
                )
                self._event(
                    task["id"],
                    "attempt.claimed",
                    {"generation": generation},
                    step_id=step["id"],
                    attempt_id=attempt_id,
                )
                self.db.execute(
                    """UPDATE orchestration_wakes SET claimed_at=?,attempt_id=?
                       WHERE agent_id=? AND task_id=? AND agent_generation=? AND claimed_at IS NULL""",
                    (now, attempt_id, agent_id, task["id"], agent["generation"]),
                )
                self._refresh_task(task["id"])
                attempt = self.db.row(
                    "SELECT * FROM orchestration_attempts WHERE id=?", (attempt_id,)
                )
                return {"attempt": self._view_attempt(attempt, worker=True, payload=True)}
        return {"attempt": None}

    def attempt_status(self, attempt_id: str, agent_id: str) -> dict[str, Any]:
        self.reconcile()
        return self._view_attempt(self._worker_attempt(attempt_id, agent_id), worker=True)

    def stop_receipt(self, attempt_id: str, agent_id: str) -> dict[str, Any]:
        """Let the same resource reconcile local locks after credential rotation.

        Current worker authentication is still required by the transport. This
        deliberately bypasses attempt-generation fencing only for the minimal
        stop attestation, never for payloads, results, questions or lease tokens.
        """
        agent = self._agent(agent_id)
        if not self._agent_current(agent):
            raise OrchestrationAccessDenied("Agent authorization was revoked.")
        attempt = self.db.one(
            "SELECT id,state,generation,agent_generation,stopped_by,stopped_at "
            "FROM orchestration_attempts WHERE id=? AND agent_id=?",
            (attempt_id, agent_id),
        )
        if attempt is None:
            raise OrchestrationNotFound("Attempt not found.")
        return {
            "attempt_id": attempt["id"],
            **{
                key: attempt[key]
                for key in ("state", "generation", "agent_generation", "stopped_by", "stopped_at")
            },
        }

    def heartbeat(
        self,
        attempt_id: str,
        agent_id: str,
        *,
        generation: int,
        lease_token: str,
        lease_seconds: float = 300,
        state: str = "running",
    ) -> dict[str, Any]:
        lease = duration(lease_seconds)
        if not isinstance(state, str) or state not in ACTIVE_ATTEMPTS:
            raise OrchestrationError("Invalid heartbeat state.")
        self.reconcile()
        with self.db.tx():
            attempt = self._worker_attempt(
                attempt_id,
                agent_id,
                generation=generation,
                lease_token=lease_token,
                require_lease=True,
            )
            # A worker must still be able to discover cancellation through its old live run.
            if attempt["desired_state"] == "cancel":
                return self._view_attempt(attempt, worker=True)
            task = self._require_live(attempt)
            if state == "claimed" and attempt["state"] != "claimed":
                raise OrchestrationConflict("An attempt cannot return to its claimed state.")
            if state == "running" and self.db.scalar(
                "SELECT count(*) FROM orchestration_questions WHERE attempt_id=? AND state='open'",
                (attempt_id,),
            ):
                state = "waiting_input"
            now = self.clock.now()
            self.db.execute(
                "UPDATE orchestration_attempts SET state=?,lease_expires_at=?,last_seen_at=? WHERE id=?",
                (state, min(now + lease, task["deadline_at"]), now, attempt_id),
            )
            self.db.execute(
                "UPDATE orchestration_steps SET state=? WHERE task_id=? AND id=?",
                (state, task["id"], attempt["step_id"]),
            )
            self.db.execute(
                "UPDATE orchestration_agents SET last_seen_at=?,availability='online' WHERE id=?",
                (now, agent_id),
            )
            if state != attempt["state"]:
                self._event(
                    task["id"],
                    "attempt." + state,
                    step_id=attempt["step_id"],
                    attempt_id=attempt_id,
                )
            self._refresh_task(task["id"])
            return self._view_attempt(self._worker_attempt(attempt_id, agent_id), worker=True)

    def report_event(
        self,
        attempt_id: str,
        agent_id: str,
        *,
        generation: int,
        lease_token: str,
        sequence: int,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        identifier(event_type, "event type")
        if type(sequence) is not int or sequence < 1:
            raise OrchestrationError("Event sequence must be a positive integer.")
        clean = event_data(data)
        event_hash = digest({"event_type": event_type, "data": clean})
        self.reconcile()
        with self.db.tx():
            attempt = self._worker_attempt(
                attempt_id,
                agent_id,
                generation=generation,
                lease_token=lease_token,
                require_lease=True,
            )
            previous = self.db.one(
                "SELECT event_hash FROM orchestration_events WHERE attempt_id=? AND worker_sequence=?",
                (attempt_id, sequence),
            )
            if previous is not None:
                if previous["event_hash"] != event_hash:
                    raise OrchestrationConflict("Event sequence already has different content.")
                return self._view_attempt(attempt, worker=True)
            self._require_live(attempt)
            if sequence != attempt["last_sequence"] + 1:
                raise OrchestrationConflict("Events must arrive in contiguous sequence order.")
            self._event(
                attempt["task_id"],
                "worker." + event_type,
                clean,
                step_id=attempt["step_id"],
                attempt_id=attempt_id,
                sequence=sequence,
                event_hash=event_hash,
            )
            self.db.execute(
                "UPDATE orchestration_attempts SET last_sequence=? WHERE id=?",
                (sequence, attempt_id),
            )
            return self._view_attempt(self._worker_attempt(attempt_id, agent_id), worker=True)

    def report_result(
        self,
        attempt_id: str,
        agent_id: str,
        *,
        generation: int,
        lease_token: str,
        text: str,
        artifacts: list[dict[str, Any]] | None = None,
        outcome: str = "succeeded",
    ) -> dict[str, Any]:
        text = bounded_text(text, "result text", MAX_RESULT_CHARS, empty=True)
        clean = artifact_metadata([] if artifacts is None else artifacts)
        if not isinstance(outcome, str) or outcome not in {"succeeded", "failed", "cancelled"}:
            raise OrchestrationError("Invalid result outcome.")
        if outcome == "succeeded" and not text.strip() and not clean:
            raise OrchestrationError("A successful result needs text or an artifact.")
        result = {"text": text, "artifacts": clean, "outcome": outcome}
        result_hash = digest(result)
        self.reconcile()
        with self.db.tx():
            attempt = self._worker_attempt(
                attempt_id,
                agent_id,
                generation=generation,
                lease_token=lease_token,
                require_lease=True,
            )
            task = self._task(attempt["task_id"])
            if not self._task_authorized(task):
                raise OrchestrationConflict(
                    "Task authorization changed; this receipt cannot publish a result."
                )
            if attempt["result_hash"] is not None:
                if attempt["result_hash"] != result_hash:
                    raise OrchestrationConflict("This attempt already reported a different result.")
                return self._view_attempt(attempt, worker=True)
            if attempt["state"] not in ACTIVE_ATTEMPTS | {"uncertain"}:
                raise OrchestrationConflict("This attempt can no longer report a result.")
            now = self.clock.now()
            eligible = (
                attempt["state"] in ACTIVE_ATTEMPTS
                and attempt["lease_expires_at"] > now
                and task["deadline_at"] > now
                and task["desired_state"] == "run"
            )
            # A late report is evidence, not permission to publish or release dependencies.
            if outcome == "cancelled":
                state = "cancelled"
                eligible = False
            else:
                state = "result_reported" if eligible else "uncertain"
            self.db.execute(
                """UPDATE orchestration_attempts SET state=?,result=?,result_hash=?,result_received_at=?,
                   result_eligible=?,finished_at=?,last_seen_at=? WHERE id=?""",
                (state, canonical(result), result_hash, now, int(eligible), now, now, attempt_id),
            )
            self.db.execute(
                "UPDATE orchestration_steps SET state=? WHERE task_id=? AND id=?",
                (state, task["id"], attempt["step_id"]),
            )
            self.db.execute(
                "UPDATE orchestration_questions SET state='closed' WHERE attempt_id=? AND state='open'",
                (attempt_id,),
            )
            self._event(
                task["id"],
                "attempt." + state,
                {"outcome": outcome, "eligible": eligible},
                step_id=attempt["step_id"],
                attempt_id=attempt_id,
            )
            self._refresh_task(task["id"])
            return self._view_attempt(self._worker_attempt(attempt_id, agent_id), worker=True)

    def ask_question(
        self,
        attempt_id: str,
        agent_id: str,
        *,
        generation: int,
        lease_token: str,
        question_id: str,
        text: str,
        choices: list[str] | None = None,
    ) -> dict[str, Any]:
        question_id = identifier(question_id, "question ID")
        text = bounded_text(text, "question", 8_000)
        options = [] if choices is None else choices
        if not isinstance(options, list) or len(options) > 16:
            raise OrchestrationError("Invalid question choices.")
        options = [bounded_text(option, "question choice", 300) for option in options]
        if len(set(options)) != len(options):
            raise OrchestrationError("Question choices must be distinct.")
        self.reconcile()
        with self.db.tx():
            attempt = self._worker_attempt(
                attempt_id,
                agent_id,
                generation=generation,
                lease_token=lease_token,
                require_lease=True,
            )
            existing = self.db.one(
                "SELECT * FROM orchestration_questions WHERE task_id=? AND id=?",
                (attempt["task_id"], question_id),
            )
            if existing is not None:
                if (
                    existing["attempt_id"] != attempt_id
                    or existing["text"] != text
                    or loads(existing["choices"], []) != options
                ):
                    raise OrchestrationConflict("Question ID already has different content.")
                return self._view_attempt(attempt, worker=True)
            self._require_live(attempt)
            if (
                self.db.scalar(
                    "SELECT count(*) FROM orchestration_questions WHERE attempt_id=?", (attempt_id,)
                )
                >= 32
            ):
                raise OrchestrationError("The question limit for this attempt was reached.")
            self.db.execute(
                """INSERT INTO orchestration_questions (task_id,id,step_id,attempt_id,text,choices,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    attempt["task_id"],
                    question_id,
                    attempt["step_id"],
                    attempt_id,
                    text,
                    canonical(options),
                    self.clock.now(),
                ),
            )
            self.db.execute(
                "UPDATE orchestration_attempts SET state='waiting_input' WHERE id=?", (attempt_id,)
            )
            self.db.execute(
                "UPDATE orchestration_steps SET state='waiting_input' WHERE task_id=? AND id=?",
                (attempt["task_id"], attempt["step_id"]),
            )
            self._event(
                attempt["task_id"],
                "question.asked",
                {"question_id": question_id},
                step_id=attempt["step_id"],
                attempt_id=attempt_id,
            )
            self._refresh_task(attempt["task_id"])
            return self._view_attempt(self._worker_attempt(attempt_id, agent_id), worker=True)

    def answer_question(
        self, task_id: str, question_id: str, actor_id: str, *, answer: str
    ) -> dict[str, Any]:
        answer = bounded_text(answer, "answer", 8_000)
        self.reconcile()
        with self.db.tx():
            task = self._task(task_id)
            self._require_task_access(task, actor_id, write=True)
            row = self.db.one(
                "SELECT * FROM orchestration_questions WHERE task_id=? AND id=?",
                (task_id, question_id),
            )
            if row is None:
                raise OrchestrationNotFound("Question not found.")
            if row["state"] == "answered":
                if row["answer"] != answer:
                    raise OrchestrationConflict("This question already has another answer.")
                return self._view_task(task, actor_id=actor_id)
            if row["state"] != "open":
                raise OrchestrationConflict("This question is no longer open.")
            attempt = self.db.row(
                "SELECT * FROM orchestration_attempts WHERE id=?", (row["attempt_id"],)
            )
            self._require_live(attempt)
            options = loads(row["choices"], [])
            if options and answer not in options:
                raise OrchestrationError("Choose one of the question's offered answers.")
            self.db.execute(
                "UPDATE orchestration_questions SET state='answered',answer=?,answered_by=?,answered_at=? WHERE task_id=? AND id=?",
                (answer, actor_id, self.clock.now(), task_id, question_id),
            )
            self._event(
                task_id,
                "question.answered",
                {"question_id": question_id},
                step_id=row["step_id"],
                attempt_id=row["attempt_id"],
            )
            return self._view_task(self._task(task_id), actor_id=actor_id)

    # ---- acceptance, cancellation and durable recovery --------------------------------
    def accept_result(
        self,
        task_id: str,
        step_id: str,
        actor_id: str,
        *,
        attempt_id: str,
        revision: int,
        plan_hash: str,
    ) -> dict[str, Any]:
        self.reconcile()
        with self.db.tx():
            task = self._task(task_id)
            self._require_task_access(task, actor_id, write=True)
            self._require_plan(task, revision, plan_hash)
            step = self._step(task_id, step_id)
            if step["current_attempt"] != attempt_id:
                raise OrchestrationConflict("This is not the step's current attempt.")
            if step["state"] == "accepted":
                return self._view_task(task, actor_id=actor_id)
            if (
                not self._task_authorized(task)
                or task["desired_state"] != "run"
                or task["state"] in TASK_TERMINAL
            ):
                raise OrchestrationConflict("This task is no longer authorized for acceptance.")
            attempt = self.db.row("SELECT * FROM orchestration_attempts WHERE id=?", (attempt_id,))
            if (
                step["state"] != "result_reported"
                or attempt["state"] != "result_reported"
                or not attempt["result_eligible"]
            ):
                raise OrchestrationConflict(
                    "Only a current, timely reported result can be accepted."
                )
            if loads(attempt["result"], {}).get("outcome") != "succeeded":
                raise OrchestrationConflict(
                    "A failed or cancelled execution cannot be accepted as successful."
                )
            now = self.clock.now()
            self.db.execute(
                "UPDATE orchestration_steps SET state='accepted',accepted_by=?,accepted_at=? WHERE task_id=? AND id=?",
                (actor_id, now, task_id, step_id),
            )
            self.db.execute(
                "UPDATE orchestration_attempts SET state='accepted' WHERE id=?", (attempt_id,)
            )
            self._event(
                task_id,
                "result.accepted",
                {"actor_id": actor_id, "revision": revision, "plan_hash": plan_hash},
                step_id=step_id,
                attempt_id=attempt_id,
            )
            self._refresh_task(task_id)
            return self._view_task(self._task(task_id), actor_id=actor_id)

    def reject_result(
        self,
        task_id: str,
        step_id: str,
        actor_id: str,
        *,
        attempt_id: str,
        revision: int,
        plan_hash: str,
        reason: str = "",
    ) -> dict[str, Any]:
        reason = bounded_text(reason, "rejection reason", 4_000, empty=True)
        self.reconcile()
        with self.db.tx():
            task = self._task(task_id)
            self._require_task_access(task, actor_id, write=True)
            self._require_plan(task, revision, plan_hash)
            step = self._step(task_id, step_id)
            if step["current_attempt"] != attempt_id:
                raise OrchestrationConflict("This is not the step's current attempt.")
            if step["state"] == "rejected":
                if step["rejection_reason"] != reason:
                    raise OrchestrationConflict(
                        "This result was already rejected with another reason."
                    )
                return self._view_task(task, actor_id=actor_id)
            if not self._task_authorized(task) or step["state"] != "result_reported":
                raise OrchestrationConflict("Only a current reported result can be rejected.")
            self.db.execute(
                "UPDATE orchestration_steps SET state='rejected',rejection_reason=? WHERE task_id=? AND id=?",
                (reason, task_id, step_id),
            )
            self.db.execute(
                "UPDATE orchestration_attempts SET state='rejected',result_eligible=0 WHERE id=?",
                (attempt_id,),
            )
            self._event(
                task_id,
                "result.rejected",
                {"actor_id": actor_id, "reason": reason},
                step_id=step_id,
                attempt_id=attempt_id,
            )
            self._refresh_task(task_id)
            return self._view_task(self._task(task_id), actor_id=actor_id)

    def cancel_task(self, task_id: str, actor_id: str) -> dict[str, Any]:
        self.reconcile()
        with self.db.tx():
            task = self._task(task_id)
            self._require_task_access(task, actor_id, write=True)
            if task["state"] in TASK_TERMINAL or task["desired_state"] == "cancel":
                return self._view_task(task, actor_id=actor_id)
            self._stop_unclaimed(task_id, "cancelled")
            self.db.execute(
                "UPDATE orchestration_tasks SET desired_state='cancel',stop_reason='user_cancelled',updated_at=? WHERE id=?",
                (self.clock.now(), task_id),
            )
            self._event(task_id, "task.cancel_requested", {"actor_id": actor_id})
            self._refresh_task(task_id)
            return self._view_task(self._task(task_id), actor_id=actor_id)

    def confirm_stopped(
        self,
        task_id: str,
        step_id: str,
        actor_id: str,
        attempt_id: str,
        note: str,
    ) -> dict[str, Any]:
        """An accountable owner attests external execution stopped; never requeue it."""
        note = bounded_text(note, "stop confirmation note", 1_000)
        self.reconcile()
        with self.db.tx():
            task = self._task(task_id)
            step = self._step(task_id, step_id)
            self._require_agent_owner(self._agent(step["agent_id"]), actor_id)
            if step["current_attempt"] != attempt_id:
                raise OrchestrationConflict("This is not the step's current attempt.")
            attempt = self.db.row("SELECT * FROM orchestration_attempts WHERE id=?", (attempt_id,))
            if attempt["state"] == "stopped" or (
                attempt["state"] == "accepted" and attempt["stopped_at"] is not None
            ):
                if attempt["stop_note"] != note or attempt["stopped_by"] != actor_id:
                    raise OrchestrationConflict(
                        "This attempt already has a different stop confirmation."
                    )
            elif attempt["state"] == "accepted":
                # An accepted result is not proof that its native execution stopped.
                # Record only the owner's explicit attestation. Preserve acceptance,
                # result eligibility and downstream work, including after token rotation.
                self.db.execute(
                    """UPDATE orchestration_attempts SET stopped_by=?,stopped_at=?,stop_note=?
                       WHERE id=?""",
                    (actor_id, self.clock.now(), note, attempt_id),
                )
                self._event(
                    task_id,
                    "attempt.stop_confirmed",
                    {"actor_id": actor_id, "note": note},
                    step_id=step_id,
                    attempt_id=attempt_id,
                )
            else:
                if attempt["state"] != "uncertain" and not (
                    attempt["state"] in ACTIVE_ATTEMPTS and attempt["desired_state"] == "cancel"
                ):
                    raise OrchestrationConflict(
                        "Only an accepted, uncertain or cancellation-pending attempt needs confirmation."
                    )
                now = self.clock.now()
                self.db.execute(
                    """UPDATE orchestration_attempts SET state='stopped',desired_state='cancel',result_eligible=0,
                       stopped_by=?,stopped_at=?,stop_note=?,finished_at=? WHERE id=?""",
                    (actor_id, now, note, now, attempt_id),
                )
                step_state = (
                    "revoked"
                    if task["state"] == "revoked"
                    else ("cancelled" if task["stop_reason"] == "user_cancelled" else "failed")
                )
                self.db.execute(
                    "UPDATE orchestration_steps SET state=? WHERE task_id=? AND id=?",
                    (step_state, task_id, step_id),
                )
                self._event(
                    task_id,
                    "attempt.stop_confirmed",
                    {"actor_id": actor_id, "note": note},
                    step_id=step_id,
                    attempt_id=attempt_id,
                )
                self._refresh_task(task_id)
            task = self._task(task_id)
            try:
                self._require_task_access(task, actor_id)
            except (OrchestrationAccessDenied, OrchestrationNotFound):
                return {
                    "task_id": task_id,
                    "step_id": step_id,
                    "attempt_id": attempt_id,
                    "state": "accepted" if attempt["state"] == "accepted" else "stopped",
                    "redacted": True,
                }
            return self._view_task(task, actor_id=actor_id)

    def _stop_unclaimed(self, task_id: str, state: str) -> None:
        self.db.execute(
            "UPDATE orchestration_steps SET state=? WHERE task_id=? AND state IN ('planned','queued','result_reported')",
            (state, task_id),
        )
        self.db.execute(
            """UPDATE orchestration_attempts SET desired_state='cancel',result_eligible=0,
               state=CASE WHEN state='result_reported' THEN ? ELSE state END
               WHERE task_id=? AND state NOT IN ('accepted','rejected','failed','cancelled','revoked')""",
            (state, task_id),
        )
        self.db.execute(
            "UPDATE orchestration_questions SET state='closed' WHERE task_id=? AND state='open'",
            (task_id,),
        )

    def _invalidate_task(self, task: sqlite3.Row, reason: str) -> None:
        if task["state"] in TASK_TERMINAL:
            return
        self._stop_unclaimed(task["id"], "revoked")
        self.db.execute(
            """UPDATE orchestration_attempts SET state='uncertain',desired_state='cancel',result_eligible=0
               WHERE task_id=? AND state IN ('claimed','running','waiting_input')""",
            (task["id"],),
        )
        self.db.execute(
            "UPDATE orchestration_steps SET state='uncertain' WHERE task_id=? AND state IN ('claimed','running','waiting_input')",
            (task["id"],),
        )
        self.db.execute(
            "UPDATE orchestration_tasks SET state='revoked',desired_state='cancel',stop_reason=?,updated_at=? WHERE id=?",
            (reason, self.clock.now(), task["id"]),
        )
        self._event(task["id"], "task.revoked", {"reason": reason})

    def _fence_agent_tasks(self, agent_id: str, reason: str) -> None:
        for task in self.db.query(
            """SELECT DISTINCT t.* FROM orchestration_tasks t JOIN orchestration_steps s ON s.task_id=t.id
               WHERE s.agent_id=? AND t.state NOT IN ('succeeded','failed','cancelled','expired','revoked')""",
            (agent_id,),
        ):
            self._invalidate_task(task, reason)

    def _uncertain(self, attempt: sqlite3.Row, reason: str) -> None:
        self.db.execute(
            "UPDATE orchestration_attempts SET state='uncertain',desired_state='cancel',result_eligible=0 WHERE id=?",
            (attempt["id"],),
        )
        self.db.execute(
            "UPDATE orchestration_steps SET state='uncertain' WHERE task_id=? AND id=?",
            (attempt["task_id"], attempt["step_id"]),
        )
        self._stop_unclaimed(attempt["task_id"], "cancelled")
        self.db.execute(
            "UPDATE orchestration_tasks SET desired_state='cancel',stop_reason=COALESCE(stop_reason,?) WHERE id=?",
            (reason, attempt["task_id"]),
        )
        self._event(
            attempt["task_id"],
            "attempt.uncertain",
            {"reason": reason},
            step_id=attempt["step_id"],
            attempt_id=attempt["id"],
        )
        self._refresh_task(attempt["task_id"])

    def mark_uncertain(
        self,
        attempt_id: str,
        agent_id: str,
        *,
        generation: int,
        lease_token: str,
        reason: str = "execution_status_unknown",
    ) -> dict[str, Any]:
        reason = bounded_text(reason, "uncertainty reason", 1_000)
        self.reconcile()
        with self.db.tx():
            attempt = self._worker_attempt(
                attempt_id,
                agent_id,
                generation=generation,
                lease_token=lease_token,
                require_lease=True,
            )
            if attempt["state"] in ACTIVE_ATTEMPTS:
                self._uncertain(attempt, reason)
            elif attempt["state"] != "uncertain":
                raise OrchestrationConflict("This attempt already has a settled outcome.")
            return self._view_attempt(self._worker_attempt(attempt_id, agent_id), worker=True)

    def _refresh_task(self, task_id: str) -> None:
        task = self._task(task_id)
        if task["state"] in TASK_TERMINAL:
            return
        rows = self.db.query("SELECT * FROM orchestration_steps WHERE task_id=?", (task_id,))
        # A failed prerequisite permanently blocks descendants in this immutable plan.
        while True:
            states = {row["id"]: row["state"] for row in rows}
            blocked = [
                row
                for row in rows
                if row["state"] in {"planned", "queued"}
                and any(
                    states[dep] in STEP_TERMINAL - {"accepted"}
                    for dep in loads(row["depends_on"], [])
                )
            ]
            if not blocked:
                break
            for row in blocked:
                self.db.execute(
                    "UPDATE orchestration_steps SET state='skipped' WHERE task_id=? AND id=?",
                    (task_id, row["id"]),
                )
            rows = self.db.query("SELECT * FROM orchestration_steps WHERE task_id=?", (task_id,))
        states_list = [row["state"] for row in rows]
        required_failure = any(
            row["required"] and row["state"] in STEP_TERMINAL - {"accepted"} for row in rows
        )
        if required_failure and task["desired_state"] != "cancel":
            # Stop other work once an indispensable step has failed; keep live external
            # attempts visible until they acknowledge cancellation or become uncertain.
            self._stop_unclaimed(task_id, "skipped")
            self.db.execute(
                "UPDATE orchestration_tasks SET desired_state='cancel',stop_reason='required_step_failed' WHERE id=?",
                (task_id,),
            )
            rows = self.db.query("SELECT * FROM orchestration_steps WHERE task_id=?", (task_id,))
            states_list = [row["state"] for row in rows]
        if "uncertain" in states_list:
            state = "uncertain"
        elif any(s in ACTIVE_ATTEMPTS for s in states_list):
            if task["desired_state"] == "cancel" or required_failure:
                state = "cancelling"
            else:
                state = "waiting_input" if "waiting_input" in states_list else "running"
        elif required_failure:
            if task["stop_reason"] == "user_cancelled":
                state = "cancelled"
            elif task["stop_reason"] == "deadline_elapsed":
                state = "expired"
            else:
                state = "failed"
        elif task["desired_state"] == "cancel":
            state = "cancelled"
        elif all(row["state"] in STEP_TERMINAL for row in rows):
            state = (
                "succeeded"
                if all(row["state"] == "accepted" for row in rows if row["required"])
                else "failed"
            )
        elif "result_reported" in states_list:
            state = "awaiting_acceptance"
        else:
            state = "queued" if task["approved_at"] is not None else "planned"
        if state != task["state"]:
            self.db.execute(
                "UPDATE orchestration_tasks SET state=?,updated_at=? WHERE id=?",
                (state, self.clock.now(), task_id),
            )
            self._event(task_id, "task.state", {"state": state})

    def reconcile(self) -> dict[str, int]:
        """Recover from restarts without converting an unobserved execution into a retry."""
        with self.db.tx():
            return self._reconcile_locked()

    def _reconcile_locked(self) -> dict[str, int]:
        counts = {
            "tasks_revoked": 0,
            "tasks_expired": 0,
            "leases_uncertain": 0,
            "wakes_uncertain": 0,
        }
        now = self.clock.now()
        for task in self.db.query(
            "SELECT * FROM orchestration_tasks WHERE state NOT IN ('succeeded','failed','cancelled','expired','revoked')"
        ):
            if not self._task_authorized(task):
                self._invalidate_task(task, "authorization_changed")
                counts["tasks_revoked"] += 1
                continue
            if task["deadline_at"] <= now:
                self._stop_unclaimed(task["id"], "expired")
                for attempt in self.db.query(
                    "SELECT * FROM orchestration_attempts WHERE task_id=? AND state IN ('claimed','running','waiting_input')",
                    (task["id"],),
                ):
                    self._uncertain(attempt, "deadline_elapsed")
                    counts["leases_uncertain"] += 1
                uncertain = self.db.scalar(
                    "SELECT count(*) FROM orchestration_steps WHERE task_id=? AND state='uncertain'",
                    (task["id"],),
                )
                state = "uncertain" if uncertain else "expired"
                self.db.execute(
                    "UPDATE orchestration_tasks SET state=?,desired_state='cancel',stop_reason=COALESCE(stop_reason,'deadline_elapsed'),updated_at=? WHERE id=?",
                    (state, now, task["id"]),
                )
                if task["state"] != state:
                    self._event(task["id"], "task.deadline_elapsed", {"state": state})
                    counts["tasks_expired"] += 1
        for attempt in self.db.query(
            "SELECT * FROM orchestration_attempts WHERE state IN ('claimed','running','waiting_input') AND lease_expires_at<=?",
            (now,),
        ):
            self._uncertain(attempt, "lease_expired")
            counts["leases_uncertain"] += 1
        for wake in self.db.query(
            "SELECT * FROM orchestration_wakes WHERE state='reserved' AND expires_at<=?", (now,)
        ):
            self.db.execute(
                "UPDATE orchestration_wakes SET state='uncertain',detail='dispatch_receipt_timeout',finished_at=? WHERE id=?",
                (now, wake["id"]),
            )
            self._event(
                wake["task_id"],
                "wake.uncertain",
                {"wake_id": wake["id"], "reason": "dispatch_receipt_timeout"},
                step_id=wake["step_id"],
            )
            counts["wakes_uncertain"] += 1
        return counts

    # ---- wake intent is committed before HTTP; a lost reply is never a retry signal ----
    def _view_wake(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["wake_id"] = row["id"]
        return result

    def _unclaimed_wake(self, agent: sqlite3.Row) -> bool:
        return (
            self.db.one(
                """SELECT id FROM orchestration_wakes WHERE agent_id=? AND agent_generation=?
               AND claimed_at IS NULL AND state IN ('reserved','sent','uncertain') LIMIT 1""",
                (agent["id"], agent["generation"]),
            )
            is not None
        )

    def wake_candidates(
        self,
        *,
        limit: int = 100,
        kind: str | None = None,
        wake_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise OrchestrationError("Invalid wake limit.")
        self.reconcile()
        result = []
        seen: set[str] = set()
        for step in self._ready_steps():
            if step["agent_id"] in seen:
                continue
            agent = self._agent(step["agent_id"])
            if (kind is not None and agent["kind"] != kind) or (
                wake_mode is not None and agent["wake_mode"] != wake_mode
            ):
                continue
            if (
                not self._agent_current(agent)
                or self._unclaimed_wake(agent)
                or self.db.one(
                    "SELECT id FROM orchestration_wakes WHERE task_id=? AND step_id=? AND agent_generation=?",
                    (step["task_id"], step["id"], agent["generation"]),
                )
                is not None
                or self.db.scalar(
                    "SELECT count(*) FROM orchestration_attempts WHERE agent_id=? AND state IN ('claimed','running','waiting_input','uncertain')",
                    (agent["id"],),
                )
            ):
                continue
            seen.add(step["agent_id"])
            result.append(
                {
                    "agent_id": agent["id"],
                    "task_id": step["task_id"],
                    "step_id": step["id"],
                    "agent": self._view_agent(agent, owner=True),
                    "deadline_at": self._task(step["task_id"])["deadline_at"],
                }
            )
            if len(result) >= limit:
                break
        return result

    def reserve_wake(
        self, agent_id: str, task_id: str, *, timeout_seconds: float = 60
    ) -> dict[str, Any] | None:
        timeout = duration(timeout_seconds, maximum=600)
        with self.db.tx():
            self._reconcile_locked()
            agent = self._agent(agent_id)
            if not self._agent_current(agent) or self._unclaimed_wake(agent):
                return None
            task = self._task(task_id)
            if not self._task_authorized(task):
                return None
            if self.db.scalar(
                "SELECT count(*) FROM orchestration_attempts WHERE agent_id=? AND state IN ('claimed','running','waiting_input','uncertain')",
                (agent_id,),
            ):
                return None
            for step in self._ready_steps(agent_id, task_id):
                if (
                    self.db.one(
                        "SELECT id FROM orchestration_wakes WHERE task_id=? AND step_id=? AND agent_generation=?",
                        (task_id, step["id"], agent["generation"]),
                    )
                    is not None
                ):
                    continue
                wake_id = _id("wake")
                now = self.clock.now()
                self.db.execute(
                    """INSERT INTO orchestration_wakes
                       (id,agent_id,agent_generation,task_id,step_id,state,reserved_at,expires_at)
                       VALUES (?,?,?,?,?,'reserved',?,?)""",
                    (
                        wake_id,
                        agent_id,
                        agent["generation"],
                        task_id,
                        step["id"],
                        now,
                        min(now + timeout, task["deadline_at"]),
                    ),
                )
                self._event(task_id, "wake.reserved", {"wake_id": wake_id}, step_id=step["id"])
                return self._view_wake(
                    self.db.row("SELECT * FROM orchestration_wakes WHERE id=?", (wake_id,))
                )
        return None

    def finish_wake(
        self,
        wake_id: str,
        *,
        outcome: str,
        detail: str = "",
        http_status: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(outcome, str) or outcome not in {"sent", "failed", "uncertain"}:
            raise OrchestrationError("Invalid wake outcome.")
        detail = bounded_text(detail, "wake detail", 1_000, empty=True)
        if http_status is not None and (
            type(http_status) is not int or not 100 <= http_status <= 599
        ):
            raise OrchestrationError("Invalid wake HTTP status.")
        with self.db.tx():
            row = self.db.one("SELECT * FROM orchestration_wakes WHERE id=?", (wake_id,))
            if row is None:
                raise OrchestrationNotFound("Wake not found.")
            if row["state"] == outcome:
                return self._view_wake(row)
            if row["state"] not in {"reserved", "uncertain"}:
                raise OrchestrationConflict("The wake already has a settled dispatch receipt.")
            # A late receipt may settle the original dispatch, but cannot create another send.
            self.db.execute(
                "UPDATE orchestration_wakes SET state=?,detail=?,http_status=?,finished_at=? WHERE id=?",
                (outcome, detail, http_status, self.clock.now(), wake_id),
            )
            self._event(
                row["task_id"],
                "wake." + outcome,
                {"wake_id": wake_id, "http_status": http_status},
                step_id=row["step_id"],
            )
            return self._view_wake(
                self.db.row("SELECT * FROM orchestration_wakes WHERE id=?", (wake_id,))
            )

    def events(
        self, task_id: str, actor_id: str, *, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise OrchestrationError("Invalid event cursor or limit.")
        self.reconcile()
        self._require_task_access(self._task(task_id), actor_id)
        rows = self.db.query(
            "SELECT * FROM orchestration_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
            (task_id, after, limit),
        )
        items = []
        for row in rows:
            item = dict(row)
            item["data"] = loads(row["data"], {})
            item.pop("event_hash", None)
            items.append(item)
        return {"events": items, "cursor": rows[-1]["id"] if rows else after}

    def owner_occupancy(self, agent_id: str, actor_id: str) -> dict[str, Any]:
        """Resource ownership reveals occupancy identities, never another requester's payload."""
        self.reconcile()
        self._require_agent_owner(self._agent(agent_id), actor_id)
        attempts = []
        for row in self.db.query(
            "SELECT * FROM orchestration_attempts WHERE agent_id=? AND state IN ('claimed','running','waiting_input','uncertain') ORDER BY claimed_at",
            (agent_id,),
        ):
            attempts.append(
                {
                    "task_id": row["task_id"],
                    "step_id": row["step_id"],
                    "attempt_id": row["id"],
                    "state": row["state"],
                    "desired_state": row["desired_state"],
                    "can_confirm_stopped": row["state"] == "uncertain"
                    or row["desired_state"] == "cancel",
                }
            )
        wakes = [
            {
                "task_id": row["task_id"],
                "wake_id": row["id"],
                "state": row["state"],
                "can_confirm_stopped": row["state"] in {"sent", "uncertain"},
            }
            for row in self.db.query(
                "SELECT * FROM orchestration_wakes WHERE agent_id=? AND claimed_at IS NULL AND state IN ('reserved','sent','uncertain') ORDER BY reserved_at",
                (agent_id,),
            )
        ]
        return {"attempts": attempts, "wakes": wakes}

    def confirm_wake_stopped(
        self,
        task_id: str,
        wake_id: str,
        actor_id: str,
        *,
        note: str,
    ) -> dict[str, Any]:
        """Release an unclaimed wake after its resource owner reconciles the provider.

        Its original HTTP receipt and this separate stop attestation remain in the event
        log. Cancellation cannot recall a webhook; the old plan is never resent.
        """
        note = bounded_text(note, "wake stop confirmation", 1_000)
        self.reconcile()
        with self.db.tx():
            task = self._task(task_id)
            wake = self.db.one(
                "SELECT * FROM orchestration_wakes WHERE id=? AND task_id=?", (wake_id, task_id)
            )
            if wake is None:
                raise OrchestrationNotFound("Wake not found.")
            self._require_agent_owner(self._agent(wake["agent_id"]), actor_id)
            if wake["state"] == "stopped":
                if wake["stop_note"] != note or wake["stopped_by"] != actor_id:
                    raise OrchestrationConflict("This wake already has another stop confirmation.")
            else:
                if wake["claimed_at"] is not None or wake["state"] not in {"sent", "uncertain"}:
                    raise OrchestrationConflict(
                        "Only a settled or uncertain unclaimed wake can be reconciled."
                    )
                self.db.execute(
                    "UPDATE orchestration_wakes SET state='stopped',stopped_by=?,stopped_at=?,stop_note=? WHERE id=?",
                    (actor_id, self.clock.now(), note, wake_id),
                )
                self._event(
                    task_id,
                    "wake.stop_confirmed",
                    {
                        "wake_id": wake_id,
                        "previous_state": wake["state"],
                        "actor_id": actor_id,
                        "note": note,
                    },
                    step_id=wake["step_id"],
                )
                if task["state"] not in TASK_TERMINAL:
                    self._stop_unclaimed(task_id, "cancelled")
                    self.db.execute(
                        "UPDATE orchestration_tasks SET desired_state='cancel',stop_reason='wake_stop_confirmed',updated_at=? WHERE id=?",
                        (self.clock.now(), task_id),
                    )
                    self._refresh_task(task_id)
            try:
                self._require_task_access(self._task(task_id), actor_id)
            except (OrchestrationAccessDenied, OrchestrationNotFound):
                return {
                    "task_id": task_id,
                    "wake_id": wake_id,
                    "state": "stopped",
                    "redacted": True,
                }
            return self._view_task(self._task(task_id), actor_id=actor_id)

    def allowed_artifact(self, attempt_id: str, agent_id: str, artifact_id: str) -> bool:
        """Worker downloads are confined to this run and its accepted direct inputs."""
        self.reconcile()
        try:
            attempt = self._worker_attempt(attempt_id, agent_id)
        except OrchestrationError:
            return False
        task = self._task(attempt["task_id"])
        if (
            not self._task_authorized(task)
            or task["desired_state"] != "run"
            or attempt["desired_state"] != "run"
            or attempt["lease_expires_at"] <= self.clock.now()
            or attempt["state"] not in ACTIVE_ATTEMPTS | {"result_reported", "accepted"}
        ):
            return False
        artifacts = list(loads(attempt["result"], {}).get("artifacts", []))
        step = self._step(attempt["task_id"], attempt["step_id"])
        for dep_id in loads(step["depends_on"], []):
            dep = self._step(attempt["task_id"], dep_id)
            if dep["state"] == "accepted":
                receipt = self.db.row(
                    "SELECT result FROM orchestration_attempts WHERE id=?",
                    (dep["current_attempt"],),
                )
                artifacts.extend(loads(receipt["result"], {}).get("artifacts", []))
        return any(item.get("id", item.get("artifact_id")) == artifact_id for item in artifacts)
