"""Encrypted storage for verified whole-instance snapshots, using the restic CLI.

This module never snapshots a live agent home. The existing snapshot/restore code remains
responsible for consistency, checksums and revoking authority restored from an old backup.
Only local and SFTP repositories are accepted; credentials travel through a minimal child
environment, never command arguments or diagnostic output.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from personal_agent.collaboration.backup import (
    MANIFEST,
    SnapshotError,
    restore_snapshot,
    verify_snapshot,
)
from personal_agent.security.sandbox import run_process

TAG_PREFIX = "personal-agent-v1:"
SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")


class ResticError(SnapshotError):
    pass


@dataclass(frozen=True)
class ArchivedSnapshot:
    id: str
    time: str


def _repository(value: str) -> tuple[str, Path | None]:
    if not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ResticError("Set a local or SFTP restic repository.")
    if value.startswith("sftp://"):
        try:
            url = urlsplit(value)
            host = url.hostname or ""
            port = url.port
            valid_host = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host))
            if ":" in host:
                ipaddress.IPv6Address(host)
                valid_host = True
            if (
                not valid_host
                or url.password is not None
                or (
                    url.username is not None
                    and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", url.username)
                )
                or url.query
                or url.fragment
                or not url.path.startswith("/")
                or url.path == "/"
                or (port is not None and not 1 <= port <= 65535)
                or any(char.isspace() for char in value)
            ):
                raise ValueError
        except ValueError:
            raise ResticError(
                "Use an SFTP URL without passwords or options: sftp://user@host/path."
            ) from None
        return value, None
    if ":" in value or value.startswith("-"):
        raise ResticError("Use a local path or an SFTP URL: sftp://user@host/path.")
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise ResticError("The repository must not be a symbolic link.")
    path = path.parent.resolve() / path.name
    return str(path), path


class ResticArchive:
    def __init__(
        self,
        repository: str | None = None,
        *,
        namespace: str = "personal-agent",
        password_file: Path | None = None,
        timeout_s: float = 300,
        executable: str | None = None,
    ) -> None:
        self.repository, self.local_repository = _repository(
            repository if repository is not None else os.environ.get("RESTIC_REPOSITORY", "")
        )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", namespace):
            raise ResticError(
                "The backup namespace must be a short name with letters, digits, dots or dashes."
            )
        if not 1 <= timeout_s <= 3600:
            raise ResticError("The backup timeout must be between 1 and 3600 seconds.")
        self.tag = TAG_PREFIX + namespace
        self.timeout_s = timeout_s
        self.executable = executable or shutil.which("restic")
        if self.executable is None:
            raise ResticError("restic is not installed. Install the official restic binary first.")
        self._env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "SSH_AUTH_SOCK", "TMPDIR"}
        }
        self._env.update({"RESTIC_REPOSITORY": self.repository, "LANG": "C.UTF-8", "NO_COLOR": "1"})
        supplied = password_file or (
            Path(os.environ["RESTIC_PASSWORD_FILE"])
            if os.environ.get("RESTIC_PASSWORD_FILE")
            else None
        )
        if supplied is not None:
            path = supplied.expanduser().absolute()
            try:
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or not 0 < info.st_size <= 65536
                ):
                    raise ValueError
            except (OSError, ValueError):
                raise ResticError(
                    "Use a private regular password file (mode 0600, no links)."
                ) from None
            self._env["RESTIC_PASSWORD_FILE"] = str(path)
        elif os.environ.get("RESTIC_PASSWORD"):
            self._env["RESTIC_PASSWORD"] = os.environ["RESTIC_PASSWORD"]
        else:
            raise ResticError(
                "Set RESTIC_PASSWORD or a private RESTIC_PASSWORD_FILE before using restic."
            )

    async def _run(self, args: list[str], *, cwd: Path) -> str:
        assert self.executable is not None
        result = await run_process(
            [self.executable, "--quiet", "--json", "--no-cache", *args],
            env=self._env,
            cwd=cwd,
            timeout_s=self.timeout_s,
        )
        if result.timed_out:
            raise ResticError(
                "restic timed out; inspect stored snapshots before retrying a backup."
            )
        if result.error is not None:
            raise ResticError("restic could not be started.")
        if result.exit_code != 0:
            raise ResticError(
                f"restic did not complete (exit {result.exit_code}); check repository access, password and storage."
            )
        if result.truncated:
            raise ResticError(
                "restic returned too much output; inspect the repository with the restic CLI."
            )
        return result.stdout

    def initialize(self) -> None:
        """Explicitly create a new encrypted repository; restic refuses an existing one."""
        asyncio.run(self._run(["init"], cwd=Path.cwd()))

    def backup(self, snapshot: Path) -> str:
        snapshot = snapshot.expanduser().absolute()
        if snapshot.is_symlink():
            raise ResticError("The snapshot must not be a symbolic link.")
        snapshot = snapshot.resolve()
        manifest = verify_snapshot(snapshot)
        if self.local_repository is not None and (
            self.local_repository.is_relative_to(snapshot)
            or snapshot.is_relative_to(self.local_repository)
        ):
            raise ResticError("Keep the restic repository outside the snapshot directory.")
        # Only manifest-listed files are uploaded: arbitrary extra files in a snapshot
        # directory must not silently become part of the encrypted backup.
        with tempfile.TemporaryDirectory(prefix="pa-restic-input-") as temp:
            listing = Path(temp) / "files"
            listing.write_bytes(
                b"\0".join(
                    name.encode()
                    for name in [MANIFEST, *(item["path"] for item in manifest["files"])]
                )
                + b"\0"
            )
            listing.chmod(0o600)
            output = asyncio.run(
                self._run(
                    [
                        "backup",
                        "--tag",
                        self.tag,
                        "--host",
                        "personal-agent",
                        "--files-from-raw",
                        str(listing),
                    ],
                    cwd=snapshot,
                )
            )
        # A caller must leave this offline snapshot untouched during upload. Report a
        # changed source as unconfirmed even if restic managed to store a snapshot ID.
        if verify_snapshot(snapshot) != manifest:
            raise ResticError("The snapshot changed during backup; inspect the repository.")
        try:
            summaries = [
                item
                for line in output.splitlines()
                if isinstance((item := json.loads(line)), dict)
                and item.get("message_type") == "summary"
            ]
            identity = summaries[-1]["snapshot_id"]
            if not isinstance(identity, str) or not SNAPSHOT_ID.fullmatch(identity):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise ResticError(
                "restic did not confirm a snapshot ID; inspect the repository before retrying."
            ) from None
        return identity

    async def _snapshots(self, identity: str | None = None) -> list[ArchivedSnapshot]:
        args = ["snapshots", "--tag", self.tag]
        if identity is not None:
            if not SNAPSHOT_ID.fullmatch(identity):
                raise ResticError("Use the complete 64-character restic snapshot ID.")
            args.append(identity)
        else:
            args.extend(["--latest", "20"])
        output = await self._run(args, cwd=Path.cwd())
        try:
            rows = json.loads(output)
            if not isinstance(rows, list):
                raise ValueError
            result = []
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("id"), str)
                    or not SNAPSHOT_ID.fullmatch(row["id"])
                    or not isinstance(row.get("time"), str)
                    or len(row["time"]) > 64
                    or not row["time"].isascii()
                    or any(ord(char) < 32 for char in row["time"])
                    or not isinstance(row.get("tags"), list)
                    or self.tag not in row.get("tags", [])
                ):
                    raise ValueError
                if identity is not None and row["id"] != identity:
                    raise ValueError
                if datetime.fromisoformat(row["time"]).tzinfo is None:
                    raise ValueError
                result.append(ArchivedSnapshot(row["id"], row["time"]))
            return result[:20]
        except (ValueError, TypeError):
            raise ResticError("restic returned invalid snapshot metadata.") from None

    def snapshots(self) -> list[ArchivedSnapshot]:
        return asyncio.run(self._snapshots())

    def restore(self, identity: str, target: Path) -> dict[str, Any]:
        target = target.expanduser().absolute()
        target = target.parent.resolve() / target.name
        if target.exists() or target.is_symlink() or not target.parent.is_dir():
            raise ResticError("Restore into a new directory with an existing parent.")
        if self.local_repository is not None and (
            target.is_relative_to(self.local_repository)
            or self.local_repository.is_relative_to(target)
        ):
            raise ResticError("Restore outside the restic repository.")

        async def download(directory: Path) -> None:
            if len(await self._snapshots(identity)) != 1:
                raise ResticError("Snapshot not found in this backup namespace.")
            await self._run(
                ["restore", identity, "--target", str(directory), "--verify"], cwd=directory.parent
            )

        with tempfile.TemporaryDirectory(prefix="pa-restic-restore-", dir=target.parent) as temp:
            downloaded = Path(temp) / "snapshot"
            downloaded.mkdir(mode=0o700)
            asyncio.run(download(downloaded))
            verify_snapshot(downloaded)
            # Never start the restored files directly: this path revokes old sessions,
            # invitations/nodes/channel bindings and holds old timers/side effects.
            return restore_snapshot(downloaded, target)

    def check(self) -> None:
        asyncio.run(self._run(["check", "--read-data"], cwd=Path.cwd()))

    def forget(self, *, keep_last: int, apply: bool = False) -> int:
        """Preview by default. Explicit application never runs unscoped prune or forget."""
        if type(keep_last) is not int or not 1 <= keep_last <= 10000:
            raise ResticError("Keep between 1 and 10000 snapshots.")
        args = ["forget", "--tag", self.tag, "--group-by", "tags", "--keep-last", str(keep_last)]
        if not apply:
            args.append("--dry-run")
        output = asyncio.run(self._run(args, cwd=Path.cwd()))
        # restic emits no JSON groups when this namespace contains no snapshots.
        if not output.strip():
            return 0
        try:
            rows = json.loads(output)
            if not isinstance(rows, list):
                raise ValueError
            count = 0
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError
                removed = row.get("remove")
                if removed is None:
                    continue
                if not isinstance(removed, list):
                    raise ValueError
                count += len(removed)
            return count
        except (ValueError, TypeError):
            raise ResticError(
                "restic did not return a retention summary; inspect the repository."
            ) from None
