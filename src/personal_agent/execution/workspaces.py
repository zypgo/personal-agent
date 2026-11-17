"""Bounded, no-link file snapshots shared by file tools and remote execution."""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from personal_agent.execution.models import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    ExecutionError,
    relative_path,
    validate_manifest,
)

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def snapshot(root: Path, paths: list[str]) -> list[dict[str, Any]]:
    if not isinstance(paths, list) or len(paths) > MAX_FILES:
        raise ExecutionError("The file count limit was exceeded.")
    files: list[dict[str, Any]] = []
    total = 0
    for raw in paths:
        name = relative_path(raw)
        parent = os.open(root, _DIRECTORY)
        try:
            parts = name.split("/")
            for part in parts[:-1]:
                child = os.open(part, _DIRECTORY, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except OSError as exc:
            raise ExecutionError(
                "Cannot snapshot a workspace file without following links."
            ) from exc
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > MAX_FILE_BYTES
            ):
                raise ExecutionError("Only bounded regular files without hard links may be sent.")
            data = source.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ExecutionError("Total file transfer limit was exceeded.")
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ExecutionError("Workspace changed during snapshot; submit again.")
        files.append(
            {
                "path": name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_base64": base64.b64encode(data).decode(),
            }
        )
    return validate_manifest(files)


def stage_result(root: Path, job_id: str, item: dict[str, Any]) -> str:
    """Write under a generated job directory; never overwrite the live workspace."""
    name = relative_path(str(item["path"]))
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ExecutionError("Invalid job identity.")
    parts = [".pa-jobs", job_id, *name.split("/")]
    parent = os.open(root, _DIRECTORY)
    data = base64.b64decode(item["content_base64"], validate=True)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(part, _DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        try:
            fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as existing:
                info = os.fstat(existing.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or existing.read(MAX_FILE_BYTES + 1) != data
                ):
                    raise ExecutionError(
                        "A previously staged result differs; refusing overwrite."
                    ) from None
        else:
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.fsync(parent)
    except OSError as exc:
        raise ExecutionError("Cannot stage results safely.") from exc
    finally:
        os.close(parent)
    return "/".join(parts)


def remove_staged_result(root: Path, job_id: str, path: str) -> None:
    """Release a recorded staging copy after durable publication, without following links."""
    name = relative_path(path)
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ExecutionError("Invalid job identity.")
    directories = [".pa-jobs", job_id, *name.split("/")[:-1]]
    fds: list[int] = []
    try:
        fds.append(os.open(root, _DIRECTORY))
        for part in directories:
            fds.append(os.open(part, _DIRECTORY, dir_fd=fds[-1]))
        try:
            os.unlink(name.split("/")[-1], dir_fd=fds[-1])
        except FileNotFoundError:
            pass
        for index in range(len(directories) - 1, -1, -1):
            os.close(fds.pop())
            try:
                os.rmdir(directories[index], dir_fd=fds[-1])
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                    raise
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ExecutionError("Cannot release the recorded result staging file safely.") from exc
    finally:
        for fd in fds:
            os.close(fd)
