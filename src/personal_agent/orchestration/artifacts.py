"""Bounded immutable files. Authorization belongs to the associated task/attempt."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

from personal_agent.db import Database

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
SPACE_QUOTA_BYTES = 128 * 1024 * 1024


def store_files(
    db: Database,
    task_id: str,
    attempt_id: str,
    items: list[dict[str, Any]],
    *,
    now: float,
    secrets: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Call inside the same transaction as report_result; a rejected result leaves no file."""
    if len(items) > 8:
        raise ValueError("At most eight result files are allowed.")
    result: list[dict[str, Any]] = []
    total = 0
    seen: set[str] = set()
    for item in items:
        name = item["name"]
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or len(name.encode()) > 240
            or any(char in name for char in "/\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or name in seen
        ):
            raise ValueError("Use unique result filenames without directories.")
        if any(secret and secret in name for secret in secrets):
            raise ValueError("Result filename contains a connection credential.")
        seen.add(name)
        encoded = item["content_base64"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise ValueError("Result file is too large.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid result file encoding.") from exc
        total += len(content)
        if len(content) > MAX_FILE_BYTES or total > MAX_RESULT_BYTES:
            raise ValueError("Result files exceed the transfer limit.")
        if any(secret and secret.encode() in content for secret in secrets):
            raise ValueError("Result file contains a connection credential.")
        digest = hashlib.sha256(content).hexdigest()
        if digest != item["sha256"]:
            raise ValueError("Result file hash does not match.")
        mime = item.get("mime", "application/octet-stream")
        if not isinstance(mime, str) or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", mime):
            raise ValueError("Invalid result file type.")
        file_id = hashlib.sha256(f"{attempt_id}:{name}:{digest}".encode()).hexdigest()[:32]
        old = db.one(
            "SELECT id,sha256,mime FROM orchestration_artifacts WHERE attempt_id=? AND name=?",
            (attempt_id, name),
        )
        if old is not None and (old["sha256"] != digest or old["mime"] != mime):
            raise ValueError("A result filename already refers to different content.")
        if old is None:
            used = db.scalar("SELECT COALESCE(SUM(size),0) FROM orchestration_artifacts", default=0)
            if int(used) + len(content) > SPACE_QUOTA_BYTES:
                raise ValueError("Agent result storage quota reached.")
            db.execute(
                "INSERT INTO orchestration_artifacts(id,task_id,attempt_id,name,mime,sha256,size,content,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (file_id, task_id, attempt_id, name, mime, digest, len(content), content, now),
            )
        result.append(
            {"id": file_id, "name": name, "mime": mime, "sha256": digest, "size": len(content)}
        )
    return result
