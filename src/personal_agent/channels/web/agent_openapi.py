"""A provider-independent API definition for an agent's own worker connection."""

from __future__ import annotations

from typing import Any

from personal_agent.channels.web import agent_contracts as dto
from personal_agent.channels.web.contracts import RequestModel


def worker_document(base_url: str, agent_id: str, space_id: str) -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    operations: list[tuple[str, str, str, type[RequestModel] | None]] = [
        ("/hello", "connectAgent", "post", dto.HelloInput),
        ("/claim", "claimTask", "post", dto.ClaimInput),
        ("/status", "agentStatus", "get", None),
        ("/runs/{attempt_id}/status", "taskStatus", "get", None),
        ("/runs/{attempt_id}/stop-receipt", "stopReceipt", "get", None),
        ("/runs/{attempt_id}/artifacts/{artifact_id}", "fetchArtifact", "get", None),
        ("/runs/{attempt_id}/heartbeat", "heartbeat", "post", dto.HeartbeatInput),
        ("/runs/{attempt_id}/events", "reportProgress", "post", dto.EventInput),
        ("/runs/{attempt_id}/result", "submitResult", "post", dto.ResultInput),
        ("/runs/{attempt_id}/question", "askQuestion", "post", dto.QuestionInput),
        ("/runs/{attempt_id}/uncertain", "reportUncertainty", "post", dto.UncertainInput),
    ]
    for path, name, method, model in operations:
        parameters = [
            {
                "name": "space",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "enum": [space_id], "default": space_id},
            }
        ]
        if "{attempt_id}" in path:
            parameters.append(
                {"name": "attempt_id", "in": "path", "required": True, "schema": {"type": "string"}}
            )
        if "{artifact_id}" in path:
            parameters.extend(
                [
                    {
                        "name": "artifact_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "encoding",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "enum": ["base64"], "default": "base64"},
                    },
                ]
            )
        operation: dict[str, Any] = {
            "operationId": name,
            "parameters": parameters,
            "responses": {
                "200": {
                    "description": "Recorded operation; provider reports require requester acceptance."
                },
                "401": {"description": "Credential invalid or revoked"},
                "409": {
                    "description": "Stale lease or conflicting receipt; do not repeat execution"
                },
            },
        }
        if name == "fetchArtifact":
            operation["description"] = (
                "Fetch a result file from this attempt or an accepted direct dependency. "
                "Returns JSON metadata and content_base64; at most 2 MiB decoded."
            )
            operation["responses"]["200"]["content"] = {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["id", "name", "mime", "sha256", "size", "content_base64"],
                        "properties": {
                            **{
                                key: {"type": "string"}
                                for key in ("id", "name", "mime", "sha256", "content_base64")
                            },
                            "size": {"type": "integer", "maximum": 2 * 1024 * 1024},
                        },
                    }
                }
            }
        if model is not None:
            schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
            schemas.update(schema.pop("$defs", {}))
            schemas[model.__name__] = schema
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{model.__name__}"}
                    }
                },
            }
        paths[path] = {method: operation}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Qixu worker",
            "version": "1.0.0",
            "description": "Task claiming, leases and durable results for one registered agent. A wake is not completion; results await human acceptance.",
        },
        "servers": [{"url": base_url.rstrip("/") + f"/api/v4/agents/{agent_id}/worker"}],
        "security": [{"agentToken": []}],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {"agentToken": {"type": "http", "scheme": "bearer"}},
        },
    }
