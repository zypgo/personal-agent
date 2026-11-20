"""V3 browser and runner endpoints, all behind the existing HTTP security Guard."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, TypeVar, cast

from fastapi import FastAPI
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from personal_agent.channels.ingress import Ingress
from personal_agent.channels.web import contracts as dto
from personal_agent.channels.web.openapi import Endpoint, document, router
from personal_agent.collaboration.models import AccessDenied, IdentityError
from personal_agent.execution.models import ExecutionConflict, ExecutionError, ExecutionNotFound
from personal_agent.execution.workspaces import snapshot
from personal_agent.memory.store import Actor

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.channels.web.api import WebApi
    from personal_agent.channels.web.auth import WebSession
    from personal_agent.nodes.registry import Node

COOKIE = "pa_session"


def answer(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "private, no-store"})


async def error_response(_request: Request, exc: Exception) -> Response:
    if isinstance(exc, HTTPException):
        return answer({"error": exc.detail}, exc.status_code)
    if isinstance(exc, AccessDenied):
        return answer({"error": str(exc)}, 403)
    if isinstance(exc, ExecutionNotFound):
        return answer({"error": str(exc)}, 404)
    if isinstance(exc, ExecutionConflict):
        return answer({"error": str(exc)}, 409)
    if isinstance(exc, IdentityError | ExecutionError | ValueError):
        return answer({"error": str(exc)}, 400)
    if isinstance(exc, OSError):
        return answer({"error": "The requested file is unavailable."}, 404)
    return answer({"error": "The operation could not be completed."}, 500)


BodyT = TypeVar("BodyT", bound=dto.RequestModel)


async def body(request: Request, model: type[BodyT]) -> BodyT:
    """Read only after authentication; fence authority before validating or doing work."""
    try:
        value = await request.json()
    except (ValueError, RecursionError):
        raise HTTPException(400, "Send a JSON object.") from None
    revalidate = request.scope.get("state", {}).get("v3_revalidate")
    if callable(revalidate):
        revalidate()
    if not isinstance(value, dict):
        raise HTTPException(400, "Send a JSON object.")
    try:
        return model.model_validate(value, strict=True)
    except ValidationError as exc:
        # Neither the input nor Pydantic's context can enter an HTTP error or exception chain.
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        location = errors[0]["loc"] if errors else ()
        field = str(location[0]) if location else "request"
        if issubclass(model, dto.NodeAttempt) and field == "generation":
            raise ExecutionConflict("Stale attempt generation.") from None
        if field not in model.model_fields and field not in model.errors:
            field = "request"
        raise HTTPException(400, model.errors.get(field, f"Invalid {field}.")) from None


class V3Api:
    def __init__(self, root: App, *, secure: bool) -> None:
        self.root = root
        self.secure = secure
        assert (
            root.identities is not None
            and root.collaboration is not None
            and root.nodes is not None
        )
        self.identities = root.identities
        self.spaces = root.collaboration
        self.nodes = root.nodes
        # Shared across all unauthenticated enrollment/recovery/node-pair routes.
        from personal_agent.channels.web.api import Strikes

        self.strikes = Strikes(10, 60)

    def session(self, request: Request) -> WebSession:
        token = request.cookies.get(COOKIE, "")
        session = self.root.web_auth.lookup(token)
        if session is None:
            raise HTTPException(401, "Not signed in.")

        def revalidate() -> None:
            current = self.root.web_auth.lookup(token)
            if current is None or current.id != session.id:
                raise HTTPException(401, "Not signed in.")

        request.scope.setdefault("state", {})["v3_revalidate"] = revalidate
        return session

    def selected(
        self, request: Request, *, write: bool = False, manage: bool = False
    ) -> tuple[App, WebSession]:
        session = self.session(request)
        space_id = (
            request.query_params.get("space")
            or self.identities.personal_space(session.principal_id).id
        )
        membership = self.identities.require_access(
            session.principal_id, space_id, write=write, manage=manage
        )
        session_check = request.scope["state"]["v3_revalidate"]

        def revalidate() -> None:
            session_check()
            self.identities.require_access(
                session.principal_id,
                space_id,
                write=write,
                manage=manage,
                version=membership.version,
            )

        request.scope["state"]["v3_revalidate"] = revalidate
        return self.spaces.get(space_id), session

    def signed(self, principal_id: str, request: Request, data: dict[str, Any]) -> Response:
        signed = self.root.web_auth.create_session(
            principal_id, agent=request.headers.get("user-agent", "")
        )
        response = answer(data)
        response.set_cookie(
            COOKIE,
            signed.token,
            max_age=int(self.root.web_auth.session_s),
            httponly=True,
            secure=self.secure,
            samesite="strict",
            path="/",
        )
        return response

    def throttle(self, request: Request) -> None:
        source = request.client.host if request.client else ""
        now = self.root.clock.now()
        if self.strikes.wait(source, now) is not None:
            raise HTTPException(429, "Too many attempts; try again later.")
        self.strikes.fail(source, now)

    async def me(self, request: Request) -> Response:
        app, session = self.selected(request)
        principal = self.identities.principal(session.principal_id)
        membership = self.identities.require_access(principal.id, app.space_id)
        return answer(
            {
                "principal": asdict(principal),
                "spaces": [asdict(s) for s in self.identities.spaces_for(principal.id)],
                "space_id": app.space_id,
                "role": membership.role,
            }
        )

    async def create_space(self, request: Request) -> Response:
        session = self.session(request)
        data = await body(request, dto.CreateSpace)
        space = self.identities.create_project(session.principal_id, data.name)
        self.spaces.get(space.id)
        return answer({"space": asdict(space)}, 201)

    async def members(self, request: Request) -> Response:
        app, session = self.selected(request, manage=request.method == "POST")
        if request.method == "POST":
            data = await body(request, dto.ChangeMember)
            target = data.principal_id
            if data.revoke:
                self.identities.revoke_member(session.principal_id, app.space_id, target)
            else:
                assert data.role is not None
                self.identities.set_role(session.principal_id, app.space_id, target, data.role)
            if app.jobs is not None:
                app.jobs.revoke_actor(target)
            app.live.publish({"type": "authorization_changed"})
            if data.revoke and target == session.principal_id:
                return answer({"members": []})
        return answer({"members": self.identities.members(session.principal_id, app.space_id)})

    async def invite(self, request: Request) -> Response:
        app, session = self.selected(request, manage=True)
        data = await body(request, dto.InviteMember)
        invitation = self.identities.create_member_invite(
            session.principal_id,
            app.space_id,
            data.role,
            data.label,
        )
        return answer(asdict(invitation), 201)

    async def enroll(self, request: Request) -> Response:
        self.throttle(request)
        token = request.cookies.get(COOKIE, "")
        initial = self.root.web_auth.lookup(token)
        data = await body(request, dto.Enroll)

        def existing_principal_id() -> str | None:
            current = self.root.web_auth.lookup(token)
            if data.require_existing_session and (
                initial is None
                or current is None
                or (current.id, current.principal_id, current.authz_version)
                != (initial.id, initial.principal_id, initial.authz_version)
            ):
                raise HTTPException(401, "Sign in again before joining this project.")
            return current.principal_id if current else None

        existing = existing_principal_id()
        with self.root.db.tx():
            if data.require_existing_session:
                # Recheck under the same lock as invitation redemption and session
                # creation: losing the current identity must never register a new one.
                existing = existing_principal_id()
            enrolled = self.identities.redeem_member(
                data.code,
                existing_principal_id=existing,
                label=data.display_name,
            )
            return self.signed(
                enrolled.principal.id,
                request,
                {"ok": True, "recovery_code": enrolled.recovery_code},
            )

    async def recover(self, request: Request) -> Response:
        self.throttle(request)
        data = await body(request, dto.RedeemCode)
        with self.root.db.tx():
            principal_id = self.identities.recover(data.code)
            return self.signed(principal_id, request, {"ok": True})

    async def recovery(self, request: Request) -> Response:
        session = self.session(request)
        return answer({"code": self.identities.issue_recovery(session.principal_id)})

    async def devices(self, request: Request) -> Response:
        session = self.session(request)
        return answer(
            {
                "devices": [
                    asdict(x)
                    for x in self.root.web_auth.sessions(principal_id=session.principal_id)
                ]
            }
        )

    async def device_pair(self, request: Request) -> Response:
        session = self.session(request)
        data = await body(request, dto.PairDevice)
        return answer(
            asdict(self.identities.create_device_invite(session.principal_id, data.label))
        )

    async def device_revoke(self, request: Request) -> Response:
        session = self.session(request)
        changed = self.root.web_auth.revoke(
            int(request.path_params["device_id"]), principal_id=session.principal_id
        )
        if not changed:
            raise HTTPException(404, "Device is unavailable.")
        return answer({"ok": True})

    async def channel_pair(self, request: Request) -> Response:
        app, session = self.selected(request, write=True)
        return answer(
            asdict(self.identities.create_channel_binding(session.principal_id, app.space_id))
        )

    async def channel_bindings(self, request: Request) -> Response:
        app, session = self.selected(request)
        return answer(
            {"bindings": self.identities.channel_bindings(session.principal_id, app.space_id)}
        )

    async def channel_revoke(self, request: Request) -> Response:
        app, session = self.selected(request)
        data = await body(request, dto.RevokeChannel)
        changed = self.identities.revoke_channel(
            session.principal_id,
            data.channel,
            data.account_id,
            data.sender_id,
            space_id=app.space_id,
        )
        if not changed:
            raise HTTPException(404, "Channel binding is unavailable.")
        return answer({"ok": True})

    async def tasks(self, request: Request) -> Response:
        app, session = self.selected(request, write=request.method == "POST")
        assert app.jobs is not None
        if request.method == "POST":
            data = await body(request, dto.CreateTask)
            inputs = snapshot(app.paths.workspace_dir, data.input_paths)
            task = app.jobs.create_task(
                title=data.title,
                command=data.command,
                actor_id=session.principal_id,
                request_id=data.client_request_id,
                timeout_s=data.timeout_s,
                input_manifest=inputs,
                output_paths=data.output_paths,
                channel="web",
            )
            return answer({"task": task}, 201)
        return answer({"tasks": app.jobs.list_tasks(summaries=True)})

    async def task(self, request: Request) -> Response:
        app, _ = self.selected(request)
        assert app.jobs is not None
        task = app.jobs.get_task(request.path_params["task_id"])
        return answer({"task": task, "jobs": task.get("jobs", [])})

    async def cancel(self, request: Request) -> Response:
        app, session = self.selected(request, write=True)
        assert app.jobs is not None
        membership = self.identities.require_access(session.principal_id, app.space_id)
        return answer(
            {
                "task": app.jobs.cancel_task(
                    request.path_params["task_id"],
                    actor_id=session.principal_id,
                    authorized=membership.can_manage,
                )
            }
        )

    async def revise(self, request: Request) -> Response:
        app, session = self.selected(request, write=True)
        assert app.jobs is not None
        data = await body(request, dto.ReviseTask)
        member = self.identities.require_access(session.principal_id, app.space_id)
        return answer(
            {
                "task": app.jobs.revise_task(
                    request.path_params["task_id"],
                    actor_id=session.principal_id,
                    expected_revision=data.revision,
                    title=data.title,
                    authorized=member.can_manage,
                )
            }
        )

    async def step(self, request: Request) -> Response:
        app, session = self.selected(request, write=True)
        assert app.jobs is not None
        data = await body(request, dto.AddStep)
        revision = data.expected_revision
        member = self.identities.require_access(session.principal_id, app.space_id)
        inputs = snapshot(app.paths.workspace_dir, data.input_paths)
        return answer(
            {
                "job": app.jobs.add_step(
                    request.path_params["task_id"],
                    actor_id=session.principal_id,
                    expected_revision=revision,
                    step_id=data.step_id,
                    command=data.command,
                    timeout_s=data.timeout_s,
                    input_manifest=inputs,
                    output_paths=data.output_paths,
                    authorized=member.can_manage,
                )
            },
            201,
        )

    async def question(self, request: Request) -> Response:
        app, session = self.selected(request, write=True)
        assert app.jobs is not None
        data = await body(request, dto.AskQuestion)
        revision = data.expected_revision
        member = self.identities.require_access(session.principal_id, app.space_id)
        return answer(
            {
                "question": app.jobs.request_input(
                    request.path_params["task_id"],
                    actor_id=session.principal_id,
                    expected_revision=revision,
                    question=data.question,
                    choices=data.choices,
                    request_id=data.request_id,
                    authorized=member.can_manage,
                )
            },
            201,
        )

    async def answer_question(self, request: Request) -> Response:
        app, session = self.selected(request, write=True)
        assert app.jobs is not None
        data = await body(request, dto.AnswerQuestion)
        revision = data.expected_revision
        return answer(
            {
                "question": app.jobs.answer_input(
                    request.path_params["task_id"],
                    request.path_params["question_id"],
                    actor_id=session.principal_id,
                    expected_revision=revision,
                    answer=data.answer,
                    authorized=True,
                )
            }
        )

    async def approve(self, request: Request) -> Response:
        app, session = self.selected(request, write=True)
        self.identities.require_admin(session.principal_id)
        assert app.jobs is not None
        data = await body(request, dto.ApproveJob)
        job = app.jobs.approve_job(
            request.path_params["job_id"],
            actor_id=session.principal_id,
            plan_hash=data.plan_hash,
            allowed=data.allowed,
        )
        return answer({"job": job})

    async def review(self, request: Request) -> Response:
        """Resource owner reviews execution plans, without access to private space history."""
        session = self.session(request)
        self.identities.require_admin(session.principal_id)
        if request.method == "POST":
            data = await body(request, dto.ReviewJob)
            app = self.spaces.get(data.space_id)
            assert app.jobs is not None
            return answer(
                {
                    "job": app.jobs.approve_job(
                        data.job_id,
                        actor_id=session.principal_id,
                        plan_hash=data.plan_hash,
                        allowed=data.allowed,
                    )
                }
            )
        pending = []
        for app in self.spaces.all_apps():
            assert app.jobs is not None
            for task in app.jobs.list_tasks():
                for job in task.get("jobs", []):
                    if (
                        job["state"] == "awaiting_approval"
                        and job["approver_id"] == session.principal_id
                    ):
                        pending.append(
                            {"space_id": app.space_id, "title": task["title"], "job": job}
                        )
        return answer({"pending": pending})

    async def logs(self, request: Request) -> Response:
        app, _ = self.selected(request)
        assert app.jobs is not None
        try:
            after = int(request.query_params.get("after", "-1"))
        except ValueError:
            raise HTTPException(400, "Invalid log cursor.") from None
        if after < -1 or after > 2**63 - 1:
            raise HTTPException(400, "Invalid log cursor.")
        return answer(
            {
                "logs": app.jobs.logs(request.path_params["attempt_id"], after=after),
                "snapshots": True,
            }
        )

    async def diagnostics(self, request: Request) -> Response:
        app, _ = self.selected(request)
        spent = sum(space.budget.spent_today() for space in self.spaces.all_apps())
        limit = self.root.budget.daily_limit()
        nodes = [
            {
                "id": node.id,
                "label": node.label,
                "last_seen_at": node.last_seen_at,
                "online": node.last_seen_at is not None
                and self.root.clock.now() - node.last_seen_at < 60,
            }
            for node in self.nodes.list()
            if self.nodes.authorize(node.id, app.space_id)
        ]
        counts = {
            row["state"]: row["count"]
            for row in app.db.query(
                "SELECT state,COUNT(*) AS count FROM execution_tasks GROUP BY state"
            )
        }
        pending = app.db.scalar(
            "SELECT COUNT(*) FROM execution_attempts WHERE cleanup_state != 'released' AND state IN ('succeeded','failed','cancelled','expired')",
            default=0,
        )
        return answer(
            {
                "budget": {
                    "space_spent_usd": app.budget.spent_today(),
                    "instance_remaining_usd": max(0, limit - spent),
                    "instance_limit_usd": limit,
                },
                "tasks": counts,
                "outbox": app.outbox.counts(),
                "nodes": nodes,
                "cleanup_pending": pending,
            }
        )

    async def knowledge_history(self, request: Request) -> Response:
        import re

        app, _ = self.selected(request)
        name = request.query_params.get("path", "")
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,90}\.md", name):
            raise HTTPException(400, "Use a simple Markdown filename.")
        versions = []
        for version in app.memory.history("/memories/state/knowledge/" + name, limit=20):
            value = version.get("new_content") or ""
            versions.append(
                {
                    "id": version["id"],
                    "action": version["action"],
                    "actor": version["actor"],
                    "created_at": version["created_at"],
                    "text": bytes(value).decode() if isinstance(value, bytes) else str(value),
                }
            )
        return answer({"versions": versions})

    async def knowledge(self, request: Request) -> Response:
        app, _ = self.selected(request, write=request.method == "POST")
        prefix = "/memories/state/knowledge/"
        if request.method == "POST":
            data = await body(request, dto.WriteKnowledge)
            name = data.path
            import re

            if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,90}\.md", name):
                raise HTTPException(400, "Use a simple Markdown filename.")
            path = prefix + name
            content = data.text
            with app.db.tx():
                row = app.db.one(
                    "SELECT revision,content FROM memory_documents WHERE path=?", (path,)
                )
                revision = int(row["revision"]) if row else 0
                if data.revision != revision:
                    raise ExecutionConflict("Document changed; reload before editing.")
                from personal_agent.memory.store import PathError

                try:
                    app.memory.write_text(path, content, actor=Actor.USER)
                except PathError as exc:
                    raise HTTPException(400, str(exc)) from exc
        documents = []
        for row in app.db.query(
            "SELECT path,revision,content FROM memory_documents WHERE path LIKE ? AND content IS NOT NULL ORDER BY path",
            (prefix + "%",),
        ):
            value = row["content"]
            documents.append(
                {
                    "path": str(row["path"])[len(prefix) :],
                    "revision": row["revision"],
                    "text": bytes(value).decode() if isinstance(value, bytes) else str(value),
                }
            )
        query = request.query_params.get("q", "")[:200].casefold()
        if query:
            documents = [
                doc
                for doc in documents
                if query in doc["path"].casefold() or query in doc["text"].casefold()
            ]
        return answer({"documents": documents})

    async def node_list(self, request: Request) -> Response:
        app, _ = self.selected(request)
        return answer(
            {
                "nodes": [
                    n.to_dict()
                    for n in self.nodes.list()
                    if self.nodes.authorize(n.id, app.space_id)
                ]
            }
        )

    async def node_invite(self, request: Request) -> Response:
        session = self.session(request)
        self.identities.require_admin(session.principal_id)
        data = await body(request, dto.InviteNode)
        space_ids = data.space_ids
        for key in space_ids:
            self.identities.space(key)
        invitation = self.nodes.create_invite(session.principal_id, space_ids, label=data.label)
        return answer(asdict(invitation), 201)

    async def node_revoke(self, request: Request) -> Response:
        session = self.session(request)
        self.identities.require_admin(session.principal_id)
        return answer(
            {"ok": self.nodes.revoke(request.path_params["node_id"], session.principal_id)}
        )

    def node(self, request: Request) -> Node:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "Node authentication required.")
        node = self.nodes.authenticate(auth[7:])
        if node is None:
            raise HTTPException(401, "Node credential is invalid or revoked.")
        self.identities.principal(node.owner_id)

        def revalidate() -> None:
            current = self.nodes.authenticate(auth[7:])
            if current is None or current.id != node.id:
                raise HTTPException(401, "Node credential is invalid or revoked.")
            self.identities.principal(current.owner_id)

        request.scope.setdefault("state", {})["v3_revalidate"] = revalidate
        return node

    async def node_pair(self, request: Request) -> Response:
        self.throttle(request)
        data = await body(request, dto.PairNode)
        registered = self.nodes.redeem(data.code, data.capabilities)
        if registered is None:
            raise HTTPException(403, "Node pairing code expired or was used.")
        return answer({"node": registered.node.to_dict(), "token": registered.token})

    async def node_claim(self, request: Request) -> Response:
        node = self.node(request)
        data = await body(request, dto.ClaimNode)
        refreshed = self.nodes.heartbeat(
            node.id, data.capabilities if "capabilities" in data.model_fields_set else None
        )
        if refreshed is None:
            raise HTTPException(401, "Node credential is invalid or revoked.")
        node = refreshed
        approvers = {node.owner_id, "owner"}
        apps = self.spaces.all_apps()
        # One node runs one attempt across ALL spaces, including after a lost response.
        for app in apps:
            assert app.jobs is not None
            active = [
                a
                for a in app.jobs.attempts_for_node(node.id)
                if a["state"] in ("submitting", "admitted", "running", "collecting", "uncertain")
            ]
            if active:
                attempt_record = active[0]
                if not self.node_authorized(node, app, attempt_record):
                    task = app.jobs.get_task(attempt_record["task_id"])
                    app.jobs.cancel_task(task["id"], actor_id=task["actor_id"])
                    # Existing node receipts poll /status and receive cancellation. A lost
                    # receipt is uncertain, never permission to start a replacement workload.
                    return answer({"attempt": None})
                attempt = app.jobs.claim(
                    node.id,
                    node.effective_capabilities,
                    allowed_approver_ids=approvers,
                    node_version=node.credential_version,
                )
                if attempt is not None:
                    attempt["space_id"] = app.space_id
                    return answer({"attempt": attempt})
                return answer({"attempt": None})
        for app in apps:
            if not self.nodes.authorize(node.id, app.space_id):
                continue
            assert app.jobs is not None
            actors = [
                m["principal_id"]
                for m in self.root.db.query(
                    "SELECT m.principal_id FROM memberships m JOIN principals p ON p.id=m.principal_id WHERE m.space_id=? AND m.revoked_at IS NULL AND m.role!='viewer' AND p.disabled_at IS NULL",
                    (app.space_id,),
                )
            ]
            attempt = app.jobs.claim(
                node.id,
                node.effective_capabilities,
                allowed_actor_ids=actors,
                allowed_approver_ids=approvers,
                node_version=node.credential_version,
            )
            if attempt is not None:
                attempt["space_id"] = app.space_id
                return answer({"attempt": attempt})
        return answer({"attempt": None})

    def node_authorized(self, node: Node, app: App, attempt: dict[str, Any]) -> bool:
        assert app.jobs is not None
        job = app.jobs.get_job(attempt["job_id"])
        task = app.jobs.get_task(attempt["task_id"])
        return (
            self.nodes.authorize(node.id, app.space_id)
            and attempt["node_version"] == node.credential_version
            and set(job["plan"]["required_capabilities"]).issubset(node.effective_capabilities)
            and job["approved_by"] in {node.owner_id, "owner"}
            and self.identities.authorized(task["actor_id"], app.space_id, write=True)
            and app.jobs.authorized(task["id"])
        )

    async def node_attempt(self, request: Request) -> Response:
        node = self.node(request)
        method = request.path_params["action"]
        data = await body(request, dto.NODE_ACTIONS.get(method, dto.NodeAttempt))
        node = self.node(request)  # Current grants may have changed while the body was arriving.
        app = self.spaces.get(data.space_id)
        assert app.jobs is not None
        attempt_id = request.path_params["attempt_id"]
        attempt = app.jobs.get_attempt(attempt_id)
        if attempt["node_id"] != node.id:
            raise HTTPException(403, "Attempt belongs to another node.")
        generation = data.generation
        if type(generation) is not int or generation != attempt["generation"]:
            raise ExecutionConflict("Stale attempt generation.")
        if data.plan_hash != attempt["plan_hash"]:
            raise ExecutionConflict("Attempt plan hash differs from the approved plan.")
        task = app.jobs.get_task(attempt["task_id"])
        allowed = self.node_authorized(node, app, attempt)
        if not allowed:
            app.jobs.cancel_task(task["id"], actor_id=task["actor_id"])
        self.nodes.heartbeat(node.id)
        if method == "status":
            return answer(
                {
                    "authorized": allowed,
                    "desired_state": attempt.get("desired_state", "run") if allowed else "cancel",
                    "state": attempt["state"],
                    "generation": generation,
                }
            )
        if method == "admit":
            if not allowed:
                raise HTTPException(403, "Task authorization revoked.")
            admitted = cast(dto.AdmitAttempt, data)
            result = app.jobs.admit(
                attempt_id,
                generation,
                node_id=node.id,
                plan_hash=data.plan_hash,
                resource_id=admitted.resource_id,
            )
        elif method == "observe":
            observed = cast(dto.ObserveAttempt, data)
            result = app.jobs.observe(
                attempt_id,
                generation,
                node_id=node.id,
                state=observed.state,
                sequence=observed.sequence,
                stdout=observed.stdout,
                stderr=observed.stderr,
            )
        elif method == "complete":
            completed = cast(dto.CompleteAttempt, data)
            result = app.jobs.complete(
                attempt_id,
                generation,
                node_id=node.id,
                status=completed.status,
                exit_code=completed.exit_code,
                stdout=completed.stdout,
                stderr=completed.stderr,
                artifacts=[item.model_dump() for item in completed.artifacts or []],
                deliver=allowed,
            )
            return answer({"accepted": True, "attempt": result})
        elif method == "cleanup":
            cleaned = cast(dto.CleanupAttempt, data)
            result = app.jobs.cleanup(
                attempt_id, generation, node_id=node.id, released=cleaned.released
            )
        else:
            raise HTTPException(404, "Unknown node operation.")
        return answer({"accepted": True, "attempt": result})

    async def openapi(self, request: Request) -> Response:
        self.session(request)
        return answer(document())

    def routes(self) -> list[BaseRoute]:
        routes = router(lambda name: cast(Endpoint, getattr(self, name))).routes
        return [*routes, Route("/api/v3/openapi.json", self.openapi)]


class SpaceRouter:
    def __init__(self, fallback: ASGIApp, root: App, api: WebApi) -> None:
        from personal_agent.channels.web.agents import AgentApi

        self.fallback, self.root, self.api = fallback, root, api
        self.agents_asgi = AgentApi(root, secure=api.secure).asgi()
        self.v3 = V3Api(root, secure=api.secure)
        self.v3_asgi = FastAPI(
            routes=self.v3.routes(),
            openapi_url=None,
            docs_url=None,
            redoc_url=None,
            exception_handlers={
                Exception: error_response,
                HTTPException: error_response,
                IdentityError: error_response,
                ExecutionError: error_response,
                ValueError: error_response,
            },
        )
        self.children: dict[str, ASGIApp] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.fallback(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith("/api/v4/"):
            await self.agents_asgi(scope, receive, send)
            return
        if path.startswith("/api/v3/"):
            await self.v3_asgi(scope, receive, send)
            return
        if path == "/workspace":
            response = self.api._asset(Request(scope), "workbench.html")
            await response(scope, receive, send)
            return
        if not path.startswith("/api/") or path in ("/api/pair", "/api/health", "/api/logout"):
            await self.fallback(scope, receive, send)
            return
        request = Request(scope)
        try:
            app, session = self.v3.selected(
                request,
                write=scope["method"] not in ("GET", "HEAD", "OPTIONS"),
            )
            if path.startswith("/api/approvals/"):
                app.require_approval_actor(session.principal_id)
            scope.setdefault("state", {})["principal_id"] = session.principal_id
            if app is self.root:
                await self.fallback(scope, receive, send)
                return
            if app.space_id not in self.children:
                from personal_agent.channels.web.api import WebApi, _error

                def wake() -> None:
                    self.v3.spaces.wake(app.space_id)

                api = WebApi(
                    app,
                    Ingress(app, wake=wake),
                    stop=self.api.stop,
                    secure=self.api.secure,
                    wake=wake,
                )
                self.children[app.space_id] = Starlette(
                    routes=api.routes(),
                    exception_handlers={HTTPException: _error, Exception: _error},
                )
            await self.children[app.space_id](scope, receive, send)
        except (HTTPException, IdentityError) as exc:
            response = await error_response(request, exc)
            await response(scope, receive, send)
