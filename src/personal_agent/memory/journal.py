"""SQLite is the source of truth; the editable memory files are a copy of it (a "projection").

If the process crashes, the copy can be rebuilt. Every change stores the complete new content of
the file, not an instruction like "append", so replaying a change after a crash cannot append
twice. Reads inside a transaction see the changes made so far; the files themselves change only
after COMMIT. If the owner edits a file that has no pending change, that edit is imported. If the
owner edits a file while a committed change to it is still waiting to be written, the owner's
content wins and is saved as a new version instead of being overwritten. A file that still matches
either the old or the new content is safe to write again, even after a crash that came after the
file was replaced but before the change was marked as done.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from personal_agent.clock import Clock
from personal_agent.db import Database

Snapshot = tuple[str, bytes | None]
MISSING: Snapshot = ("missing", None)


def _text(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class MemoryJournal:
    def __init__(self, root: Path, db: Database, clock: Clock) -> None:
        self.root = root.resolve()
        self.db = db
        self.clock = clock

    def key(self, path: Path) -> str:
        relative = path.relative_to(self.root).as_posix()
        return "/memories" if relative == "." else "/memories/" + relative

    def path(self, key: str) -> Path:
        if not key.startswith("/memories/"):
            raise ValueError("invalid memory projection path")
        relative = Path(key.removeprefix("/memories/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid memory projection path")
        target = self.root / relative
        # Never replay through an owner-created symlink out of the memory tree.
        resolved = target.resolve()
        if self.root not in resolved.parents:
            raise OSError("memory projection resolves outside its root")
        return target

    @staticmethod
    def disk(path: Path) -> Snapshot:
        if path.is_symlink():
            raise OSError("memory projections cannot replace symbolic links")
        if path.is_dir():
            return "directory", None
        try:
            if path.exists() and not path.is_file():
                raise OSError("memory projections require regular files")
            return "file", path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return MISSING

    def _import(self, key: str, current: Snapshot, row: Any = None) -> None:
        revision = int(row["revision"]) + 1 if row is not None else 1
        kind, data = current
        with self.db.tx():
            self.db.execute(
                """INSERT INTO memory_documents(path, kind, content, revision, projected_revision,
                                                base_kind, base_content, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, content=excluded.content,
                       revision=excluded.revision, projected_revision=excluded.projected_revision,
                       base_kind=excluded.base_kind, base_content=excluded.base_content,
                       updated_at=excluded.updated_at""",
                (key, kind, data, revision, revision, kind, data, self.clock.now()),
            )
            if row is not None and (row["kind"] == "file" or kind == "file"):
                self.db.execute(
                    """INSERT INTO memory_versions(path, action, old_content, new_content, actor, created_at)
                       VALUES (?, ?, ?, ?, 'user', ?)""",
                    (
                        key,
                        "delete"
                        if kind == "missing"
                        else "create"
                        if row["kind"] == "missing"
                        else "update",
                        _text(row["content"]),
                        _text(data),
                        self.clock.now(),
                    ),
                )

    def get(self, path: Path) -> Snapshot:
        key = self.key(path)
        row = self.db.one("SELECT * FROM memory_documents WHERE path = ?", (key,))
        if row is not None and row["revision"] != row["projected_revision"]:
            return row["kind"], row["content"]
        current = self.disk(path)
        if (row is None and current == MISSING) or (
            row is not None and current == (row["kind"], row["content"])
        ):
            return current  # the ordinary read path takes no SQLite write lock
        with self.db.tx():
            latest = self.db.one("SELECT * FROM memory_documents WHERE path = ?", (key,))
            if latest is not None and (
                latest["revision"] != latest["projected_revision"]
                or row is None
                or latest["revision"] != row["revision"]
            ):
                return latest["kind"], latest["content"]
            self._import(key, current, latest)
            return current

    def stage(self, path: Path, snapshot: Snapshot) -> None:
        key = self.key(path)
        with self.db.tx():
            before = self.get(path)
            row = self.db.one("SELECT * FROM memory_documents WHERE path = ?", (key,))
            revision = int(row["revision"]) + 1 if row else 1
            projected = int(row["projected_revision"]) if row else 0
            base = (row["base_kind"], row["base_content"]) if row else before
            self.db.execute(
                """INSERT INTO memory_documents(path, kind, content, revision, projected_revision,
                                                base_kind, base_content, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, content=excluded.content,
                       revision=excluded.revision, updated_at=excluded.updated_at""",
                (key, *snapshot, revision, projected, *base, self.clock.now()),
            )
            self.db.after_commit(self.recover)

    def entries(
        self, directory: Path, *, recursive: bool = True, ignore_errors: bool = False
    ) -> dict[Path, Snapshot]:
        """Visible descendants, including staged creations/deletions in an outer transaction."""
        paths: set[Path] = set()
        if directory.is_dir():
            for parent, dirs, files in os.walk(directory, followlinks=False):
                dirs[:] = [
                    name
                    for name in dirs
                    if not name.startswith(".") and not (Path(parent) / name).is_symlink()
                ]
                paths.update(
                    Path(parent) / name
                    for name in [*dirs, *files]
                    if not name.startswith(".") and not (Path(parent) / name).is_symlink()
                )
                if not recursive:
                    break
        prefix = self.key(directory).rstrip("/") + "/"
        for row in self.db.query(
            "SELECT path FROM memory_documents WHERE substr(path, 1, ?) = ?",
            (len(prefix), prefix),
        ):
            try:
                path = self.path(row["path"])
            except OSError:
                if ignore_errors:
                    continue
                raise
            if recursive or path.parent == directory:
                paths.add(path)
        entries: dict[Path, Snapshot] = {}
        for path in paths:
            try:
                value = self.get(path)
            except OSError:
                if ignore_errors:
                    continue
                raise
            if value != MISSING:
                entries[path] = value
        return entries

    def modified_at(self, path: Path) -> float:
        """Pending revisions use their commit time; projected files retain owner mtime edits."""
        row = self.db.one(
            "SELECT revision, projected_revision, updated_at FROM memory_documents WHERE path = ?",
            (self.key(path),),
        )
        if row is not None and row["revision"] != row["projected_revision"]:
            return float(row["updated_at"])
        return path.stat().st_mtime

    @staticmethod
    def _replace(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def recover(self) -> None:
        """Idempotently project committed bytes. Called at startup and after every commit."""
        rows = self.db.query(
            "SELECT path, kind FROM memory_documents WHERE revision != projected_revision"
        )
        # Parents before children when creating, children before parents when removing.
        rows.sort(
            key=lambda row: (
                row["kind"] == "missing",
                -len(row["path"]) if row["kind"] == "missing" else len(row["path"]),
            )
        )
        for candidate in rows:
            with self.db.tx():
                row = self.db.one(
                    "SELECT * FROM memory_documents WHERE path = ?", (candidate["path"],)
                )
                if row is None or row["revision"] == row["projected_revision"]:
                    continue
                path = self.path(row["path"])
                actual = self.disk(path)
                desired = row["kind"], row["content"]
                base = row["base_kind"], row["base_content"]
                if actual not in (desired, base):
                    self._import(row["path"], actual, row)  # preserve a concurrent owner edit
                    continue
                if actual != desired:
                    if desired[0] == "file":
                        self._replace(path, desired[1] or b"")
                    elif desired[0] == "directory":
                        path.mkdir(parents=True, exist_ok=True)
                    elif path.is_dir():
                        path.rmdir()  # never recursively delete a newly added owner file
                    else:
                        path.unlink(missing_ok=True)
                self.db.execute(
                    """UPDATE memory_documents SET projected_revision=revision,
                           base_kind=kind, base_content=content WHERE path = ?""",
                    (row["path"],),
                )
