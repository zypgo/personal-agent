"""Bounded file manifests, never tar extraction or implicit host paths."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILES = 64
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TRANSFER_BYTES = 8 * 1024 * 1024
MAX_COMMAND_CHARS = 16000
MAX_LOG_BYTES = 16384
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid execution identifier")
    return value


def relative_path(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("invalid file path")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("file path must stay within the task workspace")
    if any(
        part in {".git", ".ssh", ".aws", ".gnupg", ".pa-jobs"}
        or part == ".env"
        or part.startswith(".env.")
        for part in path.parts
    ):
        raise ValueError("credential and control paths cannot be transferred")
    return path.parts


def validate_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("attempt_id", "job_id", "space_id"):
        identifier(payload.get(key))
    if not isinstance(payload.get("generation"), int) or isinstance(payload["generation"], bool):
        raise ValueError("invalid execution generation")
    if payload["generation"] < 1:
        raise ValueError("invalid execution generation")
    if not isinstance(payload.get("plan_hash"), str) or not SHA256.fullmatch(payload["plan_hash"]):
        raise ValueError("invalid execution plan hash")
    command = payload.get("command")
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > MAX_COMMAND_CHARS
        or "\x00" in command
    ):
        raise ValueError("invalid execution command")
    if payload.get("network", "none") != "none":
        raise ValueError("this node only supports network-isolated execution")
    timeout = payload.get("timeout_s", 900)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 3600
    ):
        raise ValueError("invalid execution deadline")
    paths = payload.get("output_paths", [])
    if not isinstance(paths, list) or len(paths) > MAX_FILES:
        raise ValueError("too many output paths")
    for value in paths:
        relative_path(value)
    _decode_manifest(payload.get("input_manifest", []))
    return payload


def _decode_manifest(entries: Any) -> list[tuple[tuple[str, ...], bytes]]:
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise ValueError("too many input files")
    decoded: list[tuple[tuple[str, ...], bytes]] = []
    seen: set[tuple[str, ...]] = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid file manifest")
        parts = relative_path(entry.get("path"))
        if parts in seen:
            raise ValueError("duplicate input file")
        seen.add(parts)
        size = entry.get("size")
        data = entry.get("content_base64")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_FILE_BYTES
            or not isinstance(data, str)
            or len(data) > 4 * ((MAX_FILE_BYTES + 2) // 3)
        ):
            raise ValueError("input file exceeds the transfer quota")
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid file encoding") from exc
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise ValueError("input file does not match its manifest")
        total += len(raw)
        if total > MAX_TRANSFER_BYTES:
            raise ValueError("input transfer exceeds the quota")
        decoded.append((parts, raw))
    return decoded


def materialize(attempt_dir: Path, payload: dict[str, Any]) -> Path:
    """Publish once before execution. A durable marker lives outside the writable mount."""
    files = _decode_manifest(payload.get("input_manifest", []))
    attempt_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if attempt_dir.is_symlink():
        raise ValueError("attempt directory must not be a symlink")
    workspace = attempt_dir / "workspace"
    marker = attempt_dir / "input.json"
    if workspace.exists():
        if workspace.is_symlink() or not marker.is_file() or marker.is_symlink():
            raise ValueError("workspace publication could not be verified")
        if json.loads(marker.read_text()).get("plan_hash") != payload["plan_hash"]:
            raise ValueError("workspace belongs to a different plan")
        return workspace
    staging = Path(tempfile.mkdtemp(prefix="input-", dir=attempt_dir))
    try:
        for parts, raw in files:
            dest = staging.joinpath(*parts)
            dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        marker_tmp = attempt_dir / "input.pending"
        fd = os.open(marker_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"plan_hash": payload["plan_hash"]}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, marker)
        os.replace(staging, workspace)
        return workspace
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _read_regular(workspace: Path, parts: tuple[str, ...]) -> bytes:
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > MAX_FILE_BYTES
            ):
                raise ValueError("output is not a bounded regular file")
            raw = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (
                len(raw) > MAX_FILE_BYTES
                or len(raw) != before.st_size
                or after.st_nlink != 1
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ValueError("output changed while being collected")
            return raw
    finally:
        os.close(directory)


def collect_outputs(workspace: Path, paths: list[str]) -> list[dict[str, Any]]:
    if len(paths) > MAX_FILES:
        raise ValueError("too many output files")
    result: list[dict[str, Any]] = []
    total = 0
    for path in dict.fromkeys(paths):
        raw = _read_regular(workspace, relative_path(path))
        total += len(raw)
        if total > MAX_TRANSFER_BYTES:
            raise ValueError("output transfer exceeds the quota")
        result.append(
            {
                "path": path,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content_base64": base64.b64encode(raw).decode(),
            }
        )
    return result
