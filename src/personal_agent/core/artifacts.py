"""Immutable workspace file snapshots, atomically registered with their Outbox delivery.

The database is the publication boundary. A private blob is fully written and fsynced before
its metadata and notification commit together. A crash before commit leaves only an unlisted
blob, removed by the next sweep. The source is opened descriptor by descriptor without links;
the sandbox cannot redirect a check-then-open into the owner's private files.
"""

from __future__ import annotations

import contextlib
import hashlib
import mimetypes
import os
import re
import sqlite3
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.core.artifact_config import ArtifactConfig
from personal_agent.core.outbox import Outbox
from personal_agent.db import Database
from personal_agent.security.redact import known_secret_values, redact
from personal_agent.security.sanitize import scrub_surrogates, strip_invisible

ARTIFACT_ID = re.compile(r"[a-f0-9]{32}")
MAX_DESCRIPTION_CHARS = 500
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class ArtifactError(Exception):
    """A file cannot safely be published or delivered."""


class ArtifactExpired(ArtifactError):
    """The snapshot's retention period has ended."""


@dataclass(frozen=True)
class Artifact:
    id: str
    session_key: str
    work_id: str
    source_path: str
    name: str
    mime: str
    size: int
    sha256: str
    run_id: str | None
    created_at: float
    expires_at: float
    outbox_id: int | None

    def to_json(self) -> dict[str, Any]:
        """Public metadata: no local path, recipient, work key or credentials."""
        return {
            "id": self.id,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class Publication:
    artifact: Artifact
    reused: bool


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(**dict(row))


def _name(value: str) -> str:
    if (
        not value
        or value in (".", "..")
        or len(value.encode("utf-8", errors="replace")) > 240
        or any(c in value for c in ("/", "\\"))
        or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value)
    ):
        raise ArtifactError("Give a short filename without folders or control characters.")
    return value


def artifact_source_parts(workspace: Path, raw: str) -> tuple[str, ...]:
    """Canonical workspace-relative names, purely lexical: no stat, resolve or file read.

    Policy and publication share this representation. Opening the file remains a separate,
    descriptor-based operation after authorization; normalization never follows a symlink.
    """
    if not raw or "\0" in raw or raw.startswith("~"):
        raise ArtifactError("Give a path inside the workspace.")
    path = Path(raw)
    if ".." in path.parts:
        raise ArtifactError("Parent traversal is not allowed in an artifact path.")
    if path.is_absolute():
        try:
            path = path.relative_to(workspace)
        except ValueError as exc:
            raise ArtifactError("Artifacts must come from the workspace.") from exc
    if not path.parts:
        raise ArtifactError("Name a file inside the workspace, not the workspace folder.")
    return path.parts


def _visible(value: str) -> str:
    return scrub_surrogates(strip_invisible(value, formatting=True))


def validate_artifact_metadata(name: str, description: str) -> str:
    """Return a display-safe caption or reject credentials before any snapshot or delivery.

    Only outgoing labels/captions are checked, not the file's bytes. Compare both raw and
    display-normalized metadata/secrets so invisible characters cannot hide a credential.
    """
    _name(name)
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ArtifactError(f"Keep the description under {MAX_DESCRIPTION_CHARS} characters.")
    known = known_secret_values(os.environ)
    visible_known = [_visible(value) for value in known]
    for value in (name, description):
        visible = _visible(value)
        if redact(value, known) != value or redact(visible, visible_known) != visible:
            raise ArtifactError("Artifact filenames and descriptions cannot contain credentials.")
    return _visible(description).strip()


def _open_regular(root: Path, names: tuple[str, ...]) -> tuple[int, os.stat_result]:
    """Pin every directory, including the root; never follow a source or storage symlink."""
    parent = os.open(root, _DIRECTORY)
    try:
        for name in names[:-1]:
            child = os.open(name, _DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ArtifactError("Only ordinary files with no symbolic or hard links can be used.")
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


class ArtifactStore:
    def __init__(
        self,
        db: Database,
        directory: Path,
        workspace: Path,
        config: ArtifactConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.db = db
        self.directory = directory
        self.workspace = workspace
        self.config = config or ArtifactConfig()
        self.clock = clock or SystemClock()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _source(self, raw: str) -> tuple[str, ...]:
        return artifact_source_parts(self.workspace, raw)

    def _snapshot(self, names: tuple[str, ...]) -> bytes:
        try:
            fd, before = _open_regular(self.workspace, names)
            with os.fdopen(fd, "rb") as source:
                if before.st_size > self.config.max_file_bytes:
                    raise ArtifactError(f"The file exceeds {self.config.max_file_bytes} bytes.")
                data = source.read(self.config.max_file_bytes + 1)
                after = os.fstat(source.fileno())
                if len(data) > self.config.max_file_bytes:
                    raise ArtifactError(f"The file exceeds {self.config.max_file_bytes} bytes.")
                if _signature(before) != _signature(after) or len(data) != after.st_size:
                    raise ArtifactError(
                        "The file changed during publication; finish writing it first."
                    )
                return data
        except OSError as exc:
            raise ArtifactError(
                "Cannot open an ordinary workspace file without following links."
            ) from exc

    def get(self, artifact_id: str) -> Artifact | None:
        if not ARTIFACT_ID.fullmatch(artifact_id):
            return None
        row = self.db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        return _artifact(row) if row is not None else None

    def publish(
        self,
        path: str,
        *,
        session_key: str,
        work_id: str,
        run_id: str | None,
        channel: str,
        recipient: str | None = None,
        name: str | None = None,
        description: str = "",
        web_enabled: bool = False,
        interactive: bool = False,
        authority: dict[str, Any] | None = None,
    ) -> Publication:
        """Publish once per work + source path; retries return the original immutable snapshot.

        A corrected file in the same task must use another path. A later task has a new work key
        and may publish a new revision. Delivery remains the dispatcher's responsibility.
        """
        if not self.config.enabled:
            raise ArtifactError("Artifact publication is disabled in configuration.")
        if not work_id or not session_key:
            raise ArtifactError("Publication requires a session and a durable work identifier.")
        names = self._source(path)
        source_path = "/".join(names)
        filename = _name(name if name is not None else names[-1])
        description = validate_artifact_metadata(filename, description)
        with self.db.tx():
            existing = self.db.one(
                "SELECT * FROM artifacts WHERE session_key=? AND work_id=? AND source_path=?",
                (session_key, work_id, source_path),
            )
            if existing is not None:
                return Publication(_artifact(existing), reused=True)
            self._sweep_locked()
            data = self._snapshot(names)
            now = self.clock.now()
            used, count = self.db.row(
                "SELECT COALESCE(SUM(size),0),COUNT(*) FROM artifacts WHERE expires_at>?", (now,)
            )
            if used + len(data) > self.config.max_total_bytes:
                raise ArtifactError(
                    "Artifact storage quota is full; wait for expiry or raise the quota."
                )
            if count >= self.config.max_active_files:
                raise ArtifactError("The active artifact count limit has been reached.")
            identifier = uuid.uuid4().hex
            blob = f"{identifier}.blob"
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            artifact = Artifact(
                identifier,
                session_key,
                work_id,
                source_path,
                filename,
                mime,
                len(data),
                hashlib.sha256(data).hexdigest(),
                run_id,
                now,
                now + self.config.retention_days * 86400,
                None,
            )
            directory = os.open(self.directory, _DIRECTORY)
            created = False
            try:
                fd = os.open(
                    blob,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                created = True
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(directory)
                text = description.strip() or f"File ready: {filename} ({len(data)} bytes)."
                if channel not in ("web", "telegram"):
                    text += (
                        " Open your signed-in Personal Agent Web conversation to download the file."
                        if web_enabled
                        else " The file is saved locally; enable Personal Agent Web and sign in to download it."
                    )
                outbox_id = Outbox(self.db, self.clock).enqueue(
                    session_key=session_key,
                    channel=channel,
                    recipient=recipient,
                    kind="artifact",
                    text=text,
                    run_id=run_id,
                    dedupe_key=f"artifact:{identifier}",
                    extra={
                        **(authority or {}),
                        "artifacts": [artifact.to_json()],
                        "interactive": interactive,
                    },
                )
                self.db.execute(
                    """INSERT INTO artifacts
                       (id,session_key,work_id,source_path,name,mime,size,sha256,run_id,
                        created_at,expires_at,outbox_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        artifact.id,
                        session_key,
                        work_id,
                        source_path,
                        filename,
                        mime,
                        artifact.size,
                        artifact.sha256,
                        run_id,
                        now,
                        artifact.expires_at,
                        outbox_id,
                    ),
                )
            except BaseException:
                if created:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(blob, dir_fd=directory)
                raise
            finally:
                os.close(directory)
            return Publication(
                _artifact(self.db.row("SELECT * FROM artifacts WHERE id=?", (identifier,))),
                reused=False,
            )

    def read(self, artifact_id: str) -> tuple[Artifact, bytes]:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise ArtifactError("Artifact not found.")
        if artifact.expires_at <= self.clock.now():
            raise ArtifactExpired("This artifact has expired; ask for a new publication.")
        try:
            fd, info = _open_regular(self.directory, (f"{artifact.id}.blob",))
            with os.fdopen(fd, "rb") as source:
                if info.st_size != artifact.size:
                    raise ArtifactError("Artifact integrity check failed.")
                data = source.read(artifact.size + 1)
                if (
                    len(data) != artifact.size
                    or hashlib.sha256(data).hexdigest() != artifact.sha256
                ):
                    raise ArtifactError("Artifact integrity check failed.")
        except OSError as exc:
            raise ArtifactError("Artifact snapshot is unavailable.") from exc
        return artifact, data

    def _sweep_locked(self) -> int:
        live = {
            f"{row['id']}.blob"
            for row in self.db.query(
                "SELECT id FROM artifacts WHERE expires_at>?", (self.clock.now(),)
            )
        }
        removed = 0
        directory = os.open(self.directory, _DIRECTORY)
        try:
            for name in os.listdir(directory):
                if name.endswith(".blob") and ARTIFACT_ID.fullmatch(name[:-5]) and name not in live:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(name, dir_fd=directory)
                        removed += 1
        finally:
            os.close(directory)
        return removed

    def sweep(self) -> int:
        """Reclaim expired and uncommitted blobs; retain metadata for history and deduplication."""
        with self.db.tx():
            return self._sweep_locked()
