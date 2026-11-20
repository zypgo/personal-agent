"""Conversation cards on the existing browser/space authentication boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from personal_agent.assistant import (
    AssistantConflict,
    AssistantError,
    action_cards,
    begin_action,
    capability_catalog,
    dismiss_action,
    finish_action,
)
from personal_agent.collaboration.models import AccessDenied
from personal_agent.execution.models import ExecutionError
from personal_agent.orchestration.models import OrchestrationError

if TYPE_CHECKING:
    from personal_agent.channels.web.api import WebApi


class AssistantApi:
    def __init__(self, api: WebApi) -> None:
        self.api = api
        self.app = api.app

    def error(self, exc: Exception) -> Response:
        status = (
            403
            if isinstance(exc, AccessDenied)
            else 409
            if isinstance(exc, AssistantConflict)
            else 400
        )
        return self.api._json({"error": str(exc)}, status)

    async def cards(self, request: Request) -> Response:
        session = self.api._need(request)
        try:
            assert self.app.identities is not None
            selected = self.app.identities.space(self.app.space_id)
            return self.api._json(
                action_cards(
                    self.app,
                    session.principal_id,
                    include_cross_space_reviews=selected.kind == "personal"
                    and selected.owner_id == session.principal_id,
                )
            )
        except (AssistantError, AccessDenied, ExecutionError, OrchestrationError) as exc:
            return self.error(exc)

    async def capabilities(self, request: Request) -> Response:
        session = self.api._need(request)
        try:
            return self.api._json(capability_catalog(self.app, session.principal_id))
        except (AssistantError, AccessDenied) as exc:
            return self.error(exc)

    async def mutate(self, request: Request) -> Response:
        # Take an authority snapshot before the await, and recheck the exact session and
        # generations afterwards. Cookie presence or a valid old proposal is not authority.
        session = self.api._need(request)
        authority = self.app.actor_snapshot(session.principal_id)
        try:
            body = await request.json()
        except (ValueError, RecursionError) as exc:
            raise HTTPException(400, "Use a JSON object.") from exc
        current = self.api._need(request)
        if current.id != session.id or current.principal_id != session.principal_id:
            raise HTTPException(401, "The browser session changed.")
        try:
            self.app.require_actor(
                session.principal_id,
                version=authority["membership_version"],
                principal_version=authority["principal_version"],
            )
            check = request.scope.get("state", {}).get("v3_revalidate")
            if callable(check):
                check()
            if not isinstance(body, dict):
                raise AssistantError("Use a JSON object.")
            card_id = request.path_params["card_id"]
            operation = request.path_params["operation"]
            result: dict[str, Any]
            if operation == "begin":
                if set(body) != {"payload"} or not isinstance(body["payload"], dict):
                    raise AssistantError("A begin request contains only the final payload object.")
                result = begin_action(self.app, session.principal_id, card_id, body["payload"])
            elif operation == "complete":
                if set(body) != {"execution_id", "outcome"} or not all(
                    isinstance(value, str) for value in body.values()
                ):
                    raise AssistantError(
                        "A completion contains only execution_id and the fixed outcome; no response data."
                    )
                result = finish_action(
                    self.app,
                    session.principal_id,
                    card_id,
                    execution_id=body["execution_id"],
                    outcome=body["outcome"],
                )
            elif operation == "dismiss":
                if body:
                    raise AssistantError("Dismiss does not accept form or response data.")
                result = dismiss_action(self.app, session.principal_id, card_id)
            else:
                raise HTTPException(404, "No such action operation.")
            return self.api._json(result)
        except (AssistantError, AccessDenied, ExecutionError, OrchestrationError) as exc:
            return self.error(exc)


def assistant_routes(api: WebApi) -> list[Route]:
    assistant = AssistantApi(api)
    return [
        Route("/api/assistant/cards", assistant.cards),
        Route("/api/assistant/capabilities", assistant.capabilities),
        Route("/api/assistant/cards/{card_id}/{operation}", assistant.mutate, methods=["POST"]),
    ]
