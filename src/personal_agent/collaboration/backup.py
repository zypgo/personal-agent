"""Offline whole-instance snapshots: directory + checked manifest, no unsafe archive extraction."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from personal_agent.presence import LOCK_NAME, EngineLock

if TYPE_CHECKING:
    from personal_agent.app import App

MANIFEST = "snapshot.json"


class SnapshotError(ValueError):
    pass


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _relative(path: Any) -> str:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise SnapshotError("Invalid snapshot entry.")
    parts = path.split("/")
    if path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise SnapshotError("Invalid snapshot entry.")
    return path


def _plain(root: Path, name: str) -> Path:
    current = root
    for part in _relative(name).split("/"):
        current = current / part
        if current.is_symlink():
            raise SnapshotError("Snapshot entries must not be symbolic links.")
    if not current.is_file() or current.stat().st_nlink != 1:
        raise SnapshotError("Snapshot entries must be regular unlinked files.")
    return current


def _check_database(path: Path) -> None:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SnapshotError("Database integrity check failed.")
    finally:
        conn.close()


def create_snapshot(root: App, target: Path) -> dict[str, Any]:
    """All engines must be stopped. Secrets are retained privately, never printed."""
    source, target = root.paths.home.resolve(), target.expanduser().absolute()
    target = target.parent.resolve() / target.name
    if target.exists() or target.is_symlink():
        raise SnapshotError("Choose a new snapshot directory; nothing is overwritten.")
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise SnapshotError("Place the snapshot outside the agent home.")
    if not target.parent.is_dir():
        raise SnapshotError("The snapshot parent directory must exist.")
    apps = root.collaboration.all_apps() if root.collaboration else [root]
    with ExitStack() as locks:
        for app in apps:
            lock = EngineLock(app.paths.run_dir / LOCK_NAME)
            if not lock.acquire():
                raise SnapshotError("Stop all agent engines before taking a complete snapshot.")
            locks.callback(lock.release)
        for app in apps:
            app.memory.recover()
        target.mkdir(mode=0o700)
        try:
            db_names = {app.paths.db_file.relative_to(source).as_posix() for app in apps}
            omitted = {"run", "logs", "backups"}
            for directory, children, files in os.walk(source, followlinks=False):
                base = Path(directory)
                relative = base.relative_to(source)
                # Omit operational output only at a space's data root, never inside workspaces.
                is_home = base == source or any(base == app.paths.home for app in apps)
                children[:] = sorted(
                    child for child in children if not (is_home and child in omitted)
                )
                for child in children:
                    if (base / child).is_symlink():
                        raise SnapshotError(
                            "Remove symbolic links from the home before snapshotting."
                        )
                destination = target / relative
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                for name in sorted(files):
                    src = base / name
                    rel = src.relative_to(source).as_posix()
                    if rel in db_names or any(
                        rel == item + suffix
                        for item in db_names
                        for suffix in ("-wal", "-shm", "-journal")
                    ):
                        continue
                    if is_home and name == MANIFEST:
                        continue
                    src = _plain(source, rel)
                    before = src.stat()
                    shutil.copyfile(src, destination / name)
                    after = src.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ino,
                    ):
                        raise SnapshotError("A file changed during backup; stop writers and retry.")
                    (destination / name).chmod(stat.S_IMODE(before.st_mode) & 0o700)
            for app in apps:
                dest = target / app.paths.db_file.relative_to(source)
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                app.db.backup(dest)
                _check_database(dest)
            entries = []
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    entries.append(
                        {
                            "path": path.relative_to(target).as_posix(),
                            "bytes": path.stat().st_size,
                            "sha256": _hash(path),
                            "mode": stat.S_IMODE(path.stat().st_mode) & 0o700,
                        }
                    )
            manifest = {
                "format": 1,
                "created_at": root.clock.now(),
                "databases": sorted(db_names),
                "files": entries,
            }
            with (target / MANIFEST).open("x") as stream:
                os.chmod(target / MANIFEST, 0o600)
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            verify_snapshot(target)
            return manifest
        except BaseException:
            shutil.rmtree(target)
            raise


def verify_snapshot(source: Path) -> dict[str, Any]:
    source = source.expanduser().absolute()
    if source.is_symlink() or not source.is_dir():
        raise SnapshotError("A snapshot must be a real directory.")
    try:
        manifest = json.loads(_plain(source, MANIFEST).read_text())
    except (OSError, ValueError) as exc:
        raise SnapshotError("The snapshot manifest cannot be read.") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != 1
        or not isinstance(manifest.get("files"), list)
    ):
        raise SnapshotError("Unsupported snapshot format.")
    seen = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise SnapshotError("Invalid snapshot entry.")
        name = _relative(entry.get("path"))
        if name in seen or name == MANIFEST:
            raise SnapshotError("Duplicate snapshot entry.")
        seen.add(name)
        path = _plain(source, name)
        if path.stat().st_size != entry.get("bytes") or _hash(path) != entry.get("sha256"):
            raise SnapshotError("Snapshot checksum mismatch.")
        if type(entry.get("mode")) is not int or entry["mode"] & ~0o700:
            raise SnapshotError("Invalid snapshot file mode.")
    databases = manifest.get("databases")
    if not isinstance(databases, list) or "state.db" not in databases:
        raise SnapshotError("The snapshot has no root database.")
    for name in databases:
        if _relative(name) not in seen:
            raise SnapshotError("A database is missing from the snapshot.")
        _check_database(_plain(source, name))
    return manifest


def restore_snapshot(source: Path, target: Path) -> dict[str, Any]:
    """Verify first, restore only to a new home; the source is never altered."""
    source, target = source.expanduser().absolute(), target.expanduser().absolute()
    # Resolve parent aliases without accepting a symlink as the snapshot itself.
    source = source.parent.resolve() / source.name
    target = target.parent.resolve() / target.name
    manifest = verify_snapshot(source)
    if target.exists() or target.is_symlink() or target.is_relative_to(source):
        raise SnapshotError("Restore into a new directory outside the snapshot.")
    if not target.parent.is_dir():
        raise SnapshotError("The restore parent directory must exist.")
    target.mkdir(mode=0o700)
    try:
        for entry in manifest["files"]:
            src = _plain(source, entry["path"])
            dest = target / entry["path"]
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            dest.chmod(entry["mode"])
            if _hash(dest) != entry["sha256"]:
                raise SnapshotError("The snapshot changed while restoring.")
        _fence_restored_authority(target, manifest["databases"])
        for name in manifest["databases"]:
            _check_database(target / name)
    except BaseException:
        shutil.rmtree(target)
        raise
    return manifest


def _fence_restored_authority(home: Path, databases: list[str]) -> None:
    """An older snapshot must never resurrect a revoked device or authorize a past action."""
    now = time.time()
    root = sqlite3.connect(home / "state.db")
    try:
        with root:
            root.execute("UPDATE principals SET authz_version=authz_version+1")
            root.execute("UPDATE web_sessions SET revoked_at=COALESCE(revoked_at,?)", (now,))
            root.execute("UPDATE web_invites SET used_at=COALESCE(used_at,?)", (now,))
            root.execute("UPDATE collaboration_invites SET used_at=COALESCE(used_at,?)", (now,))
            root.execute(
                "UPDATE nodes SET revoked_at=COALESCE(revoked_at,?), credential_version=credential_version+1",
                (now,),
            )
            root.execute("UPDATE node_pairings SET used_at=COALESCE(used_at,?)", (now,))
            root.execute("DELETE FROM legacy_channel_owners")
            root.execute(
                "UPDATE channel_identities SET revoked_at=COALESCE(revoked_at,?), version=version+1",
                (now,),
            )
    finally:
        root.close()
    for name in databases:
        connection = sqlite3.connect(home / name)
        try:
            with connection:
                connection.execute("UPDATE timers SET status='paused' WHERE status='active'")
                connection.execute(
                    "UPDATE events SET status='dead', last_error='restored snapshot: review and resubmit',lease_owner=NULL,lease_expires_at=NULL WHERE status IN ('pending','leased')"
                )
                connection.execute(
                    "UPDATE outbox SET status='cancelled', last_error='restored snapshot: review before delivery' WHERE status IN ('pending','sending')"
                )
        finally:
            connection.close()
