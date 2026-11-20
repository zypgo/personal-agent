"""Build V3 routes and their public schema from the same static contracts.

Generating the schema never constructs an App, opens a database, or reads a credential.
The live schema endpoint is authenticated by V3Api like other account-scoped endpoints.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from fastapi import APIRouter
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from personal_agent import __version__
from personal_agent.channels.web.contracts import ROUTES, ErrorResponse, RouteContract

Endpoint = Callable[[Request], Awaitable[Response]]
Resolver = Callable[[str], Endpoint]


def _extra(contract: RouteContract) -> dict[str, Any]:
    parameters: list[dict[str, Any]] = []
    for name, converter in re.findall(r"\{([a-z_]+)(?::([a-z]+))?\}", contract.path):
        schema: dict[str, Any] = {"type": "integer" if converter == "int" else "string"}
        if name == "action":
            schema["enum"] = ["status", "admit", "observe", "complete", "cleanup"]
        parameters.append({"name": name, "in": "path", "required": True, "schema": schema})
    if contract.authority == "space":
        parameters.append(
            {
                "name": "space",
                "in": "query",
                "required": False,
                "description": "An accessible space ID. Defaults to the caller's personal space.",
                "schema": {"type": "string"},
            }
        )
    if contract.handler == "logs":
        parameters.append(
            {
                "name": "after",
                "in": "query",
                "schema": {"type": "integer", "minimum": -1, "maximum": 2**63 - 1, "default": -1},
            }
        )
    if contract.handler == "knowledge" and contract.method == "GET":
        parameters.append(
            {"name": "q", "in": "query", "schema": {"type": "string", "maxLength": 200}}
        )
    if contract.handler == "knowledge_history":
        parameters.append(
            {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}}
        )
    if contract.method == "POST" and contract.authority != "node":
        parameters.append(
            {
                "name": "X-PA",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "const": "1"},
            }
        )
    extra: dict[str, Any] = {
        "parameters": parameters,
        "security": []
        if contract.authority == "public"
        else [{"nodeBearer" if contract.authority == "node" else "browserSession": []}],
    }
    if contract.request is not None:
        extra["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{contract.request.__name__}"}
                }
            },
        }
    return extra


def router(resolve: Resolver) -> APIRouter:
    result = APIRouter(prefix="/api/v3")
    for contract in ROUTES:
        result.add_api_route(
            contract.path,
            resolve(contract.handler),
            methods=[contract.method],
            operation_id=f"v3_{contract.handler}_{contract.method.lower()}",
            response_class=JSONResponse,
            response_model=contract.response,
            status_code=contract.status,
            responses={
                code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 413, 429, 500)
            },
            openapi_extra=_extra(contract),
        )
        if contract.method == "GET":
            # Starlette's original GET routes also accepted HEAD. Keep that wire behavior
            # without advertising a second operation or generating duplicate operation IDs.
            result.add_api_route(
                contract.path,
                resolve(contract.handler),
                methods=["HEAD"],
                response_model=contract.response,
                include_in_schema=False,
            )
    return result


async def _schema_only_endpoint(request: Request) -> Response:
    raise RuntimeError("Schema-only endpoints must never serve requests.")


@lru_cache(maxsize=1)
def document() -> dict[str, Any]:
    """Exportable OpenAPI 3.1, with no instance-specific data or live side effects."""
    schema = get_openapi(
        title="Personal Agent V3 API",
        version=__version__,
        description=(
            "Authenticated collaboration and durable execution. Browser writes also require "
            "an allowed Origin and X-PA: 1. Bodies are strictly validated only after authentication; "
            "the session and membership are checked again after the body is read. "
            "All responses are private and must not be cached persistently."
        ),
        routes=router(lambda _name: _schema_only_endpoint).routes,
    )
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    models: set[type[BaseModel]] = {c.request for c in ROUTES if c.request is not None}
    for model in sorted(models, key=lambda m: m.__name__):
        definition = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(definition.pop("$defs", {}))
        schemas[model.__name__] = definition
    components["securitySchemes"] = {
        "browserSession": {"type": "apiKey", "in": "cookie", "name": "pa_session"},
        "nodeBearer": {
            "type": "http",
            "scheme": "bearer",
            "description": "A registered node credential; it is not a browser or administrator session.",
        },
    }
    return schema
