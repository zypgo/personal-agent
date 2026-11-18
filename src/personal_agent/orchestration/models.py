"""Bounded wire values and validation; provider reports are never acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import PurePosixPath
from typing import Any

AGENT_KINDS = frozenset({"grok", "muse", "personal_agent", "codex", "cursor"})
WAKE_MODES = frozenset({"manual", "grok_routine", "native_schedule", "verified_hook", "muse_task"})
AVAILABILITIES = frozenset({"unknown", "online", "offline"})
ACTIVE_ATTEMPTS = frozenset({"claimed", "running", "waiting_input"})
STEP_TERMINAL = frozenset(
    {"accepted", "rejected", "failed", "cancelled", "expired", "revoked", "skipped"}
)
TASK_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired", "revoked"})
MAX_RESULT_CHARS = 64_000
MAX_PROMPT_CHARS = 32_000
MAX_ARTIFACTS = 32
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class OrchestrationError(ValueError):
    """A safe, presentable validation failure."""


class OrchestrationConflict(OrchestrationError):
    """A stale lease, approval, authorization, or conflicting idempotent retry."""


class OrchestrationNotFound(OrchestrationError):
    """The selected space has no visible matching record."""


class OrchestrationAccessDenied(OrchestrationError):
    """The caller does not hold a current grant for this operation."""


def canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise OrchestrationError("A finite JSON value is required.") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value: Any, label: str = "identifier") -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise OrchestrationError(f"Invalid {label}.")
    return value


def bounded_text(value: Any, label: str, limit: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (not empty and not value.strip())
        or "\x00" in value
        or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise OrchestrationError(f"Invalid or oversized {label}.")
    return value


def string_list(value: Any, label: str, *, limit: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise OrchestrationError(f"Invalid {label}.")
    result = [identifier(item, label) for item in value]
    if len(set(result)) != len(result):
        raise OrchestrationError(f"Duplicate {label}.")
    return sorted(result)


def duration(value: Any, *, maximum: float = 3600) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 1 <= value <= maximum
    ):
        raise OrchestrationError("Invalid lease or timeout duration.")
    return float(value)


def secret_references(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 16:
        raise OrchestrationError("Use environment variable references for credentials.")
    result: dict[str, str] = {}
    for key, item in value.items():
        identifier(key, "credential reference name")
        if not isinstance(item, str) or not _ENVIRONMENT.fullmatch(item):
            raise OrchestrationError("Credentials must be environment variable names.")
        result[key] = item
    return result


def steps_plan(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise OrchestrationError("A plan requires 1 to 64 steps.")
    clean: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {
            "id",
            "agent_id",
            "prompt",
            "depends_on",
            "acceptance",
            "required",
        }:
            raise OrchestrationError("Invalid plan step fields.")
        step_id = identifier(item.get("id"), "step ID")
        if step_id in ids:
            raise OrchestrationError("Duplicate step ID.")
        ids.add(step_id)
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise OrchestrationError("Required must be a boolean.")
        clean.append(
            {
                "id": step_id,
                "agent_id": identifier(item.get("agent_id"), "agent ID"),
                "prompt": bounded_text(item.get("prompt"), "prompt", MAX_PROMPT_CHARS),
                "depends_on": string_list(item.get("depends_on", []), "dependency"),
                "acceptance": bounded_text(item.get("acceptance"), "acceptance criteria", 8_000),
                "required": required,
            }
        )
    graph = {step["id"]: set(step["depends_on"]) for step in clean}
    if any(not deps <= ids for deps in graph.values()):
        raise OrchestrationError("Every dependency must name a step in this plan.")
    resolved: set[str] = set()
    while len(resolved) < len(ids):
        ready = {key for key, deps in graph.items() if key not in resolved and deps <= resolved}
        if not ready:
            raise OrchestrationError("A plan must be an acyclic dependency graph.")
        resolved.update(ready)
    if not any(step["required"] for step in clean):
        raise OrchestrationError("A plan needs at least one required step.")
    return clean


def artifact_metadata(value: Any) -> list[dict[str, Any]]:
    """Accept references only. The HTTP boundary verifies and stores actual file bytes."""
    if not isinstance(value, list) or len(value) > MAX_ARTIFACTS:
        raise OrchestrationError("Invalid artifact count.")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed = {
        "id",
        "artifact_id",
        "name",
        "path",
        "size",
        "sha256",
        "media_type",
        "mime",
        "download_url",
    }
    for item in value:
        if not isinstance(item, dict) or set(item) - allowed:
            raise OrchestrationError("Artifacts must contain metadata only.")
        result: dict[str, Any] = {}
        for key, entry in item.items():
            if key == "size":
                if type(entry) is not int or not 0 <= entry <= 64 * 1024 * 1024:
                    raise OrchestrationError("Invalid artifact size.")
            elif key == "sha256":
                if not isinstance(entry, str) or not re.fullmatch(r"[0-9a-f]{64}", entry):
                    raise OrchestrationError("Invalid artifact hash.")
            else:
                bounded_text(entry, f"artifact {key}", 1_000)
            if key in {"path", "name"}:
                path = PurePosixPath(entry)
                if (
                    path.is_absolute()
                    or "\\" in entry
                    or any(p in {"", ".", ".."} for p in entry.split("/"))
                ):
                    raise OrchestrationError("Artifact names must be relative paths.")
            result[key] = entry
        ref = result.get("artifact_id", result.get("id", result.get("path", result.get("name"))))
        if not isinstance(ref, str) or ref in seen:
            raise OrchestrationError("Artifacts need unique references or names.")
        seen.add(ref)
        clean.append(result)
    return clean


def event_data(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise OrchestrationError("Event data must be an object.")
    encoded = canonical(value)
    if len(encoded) > 16_000:
        raise OrchestrationError("Event data is too large.")
    forbidden = {"content_base64", "worker_token", "lease_token", "password", "secret", "api_key"}

    def check(item: Any, depth: int) -> None:
        if depth > 6:
            raise OrchestrationError("Event data is too deeply nested.")
        if isinstance(item, dict):
            if any(not isinstance(k, str) or k.lower() in forbidden for k in item):
                raise OrchestrationError("Credentials and file bytes cannot be event data.")
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif isinstance(item, str):
            bounded_text(item, "event value", 8_000, empty=True)

    check(value, 0)
    return value
