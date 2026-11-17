"""Provider-independent execution plans. A plan never grants access by itself."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import PurePosixPath
from typing import Any

MAX_FILES = 64
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_LOG_CHARS = 64_000
MAX_LOG_SNAPSHOTS = 8
MAX_COMMAND_CHARS = 16_000
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired", "rejected"})
ACTIVE = frozenset({"submitting", "admitted", "running", "collecting", "uncertain"})
CAPABILITIES = frozenset({"isolated_exec", "persistent_receipts", "network_none"})


class ExecutionError(ValueError):
    """A rejected transition or invalid plan, safe to present to the user."""


class ExecutionConflict(ExecutionError):
    """The caller used a stale version, identity, or conflicting idempotency key."""


class ExecutionNotFound(ExecutionError):
    """No such record exists inside the selected space."""


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500 or "\\" in value:
        raise ExecutionError("Use a relative workspace file path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ExecutionError("A file path must stay inside its workspace.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ExecutionError("Control characters are not allowed in paths.")
    if any(
        p in {".git", ".ssh", ".aws", ".gnupg", ".pa-jobs"} or p == ".env" or p.startswith(".env.")
        for p in path.parts
    ):
        raise ExecutionError("Credential and control directories cannot be transferred.")
    return str(path)


def validate_manifest(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > MAX_FILES:
        raise ExecutionError("The file count limit was exceeded.")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for item in items:
        if not isinstance(item, dict):
            raise ExecutionError("Invalid file manifest.")
        name = relative_path(item.get("path", ""))
        if name in seen:
            raise ExecutionError("Duplicate paths in file manifest.")
        seen.add(name)
        encoded = item.get("content_base64")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise ExecutionError("File data exceeds the transfer limit.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ExecutionError("Invalid file encoding.") from exc
        if type(item.get("size")) is not int or item["size"] != len(data):
            raise ExecutionError("File size does not match its manifest.")
        if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise ExecutionError("File hash or size failed validation.")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ExecutionError("Total file transfer limit was exceeded.")
        clean.append(
            {"path": name, "size": len(data), "sha256": item["sha256"], "content_base64": encoded}
        )
    return sorted(clean, key=lambda item: str(item["path"]))


def make_plan(
    command: str,
    *,
    timeout_s: float = 900,
    input_manifest: list[dict[str, Any]] | None = None,
    output_paths: list[str] | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > MAX_COMMAND_CHARS
        or "\x00" in command
    ):
        raise ExecutionError("Give a command of 1 to 16000 characters.")
    if (
        not isinstance(timeout_s, int | float)
        or isinstance(timeout_s, bool)
        or not math.isfinite(timeout_s)
        or not 1 <= timeout_s <= 3600
    ):
        raise ExecutionError("Execution timeout must be between 1 and 3600 seconds.")
    inputs = validate_manifest([] if input_manifest is None else input_manifest)
    if output_paths is not None and (
        not isinstance(output_paths, list) or len(output_paths) > MAX_FILES
    ):
        raise ExecutionError("Invalid output file list.")
    outputs = [relative_path(p) for p in output_paths or []]
    if len(set(outputs)) != len(outputs):
        raise ExecutionError("Invalid output file list.")
    revision = digest([{k: v for k, v in item.items() if k != "content_base64"} for item in inputs])
    return {
        "version": 1,
        "command": command,
        "timeout_s": float(timeout_s),
        "network": "none",
        "input_manifest": inputs,
        "output_paths": outputs,
        "workspace_revision": revision,
        "required_capabilities": sorted(CAPABILITIES),
    }
