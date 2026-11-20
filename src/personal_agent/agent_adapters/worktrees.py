"""Detached, retained Git workspaces and bounded immutable change receipts.

This separates working files; it is not an OS sandbox. Nothing here commits, stashes,
merges, pushes, removes a worktree, or writes changes back to the source checkout.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import tarfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from personal_agent.agent_adapters.bridge import BridgeError
from personal_agent.tools import safefs

MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_NEW_BYTES = 1024 * 1024
MAX_NEW_FILES = 64
MAX_MANIFEST_BYTES = 128 * 1024
RESERVED_INPUTS = ".pa-inputs"
_ATTEMPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


class WorktreeError(BridgeError):
    """The workspace cannot safely be prepared or completely represented."""


def _directory(path: Path, *, private: bool = False) -> Path:
    absolute = path.expanduser().absolute()
    if any(ord(c) < 32 or ord(c) == 127 for c in str(absolute)):
        raise WorktreeError("Workspace paths cannot contain control characters.")
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise WorktreeError("Workspace paths must not contain symbolic links.")
    resolved = absolute.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise WorktreeError("Workspace and host profile paths must be directories.")
    if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise WorktreeError("The host profile must be private and owned by this user.")
    return resolved


def _git(
    cwd: Path,
    args: Sequence[str],
    *,
    max_bytes: int = MAX_MANIFEST_BYTES,
    allow_failure: bool = False,
    extra_config: Sequence[str] = (),
    timeout_s: float = 30,
) -> tuple[int, bytes]:
    env = {
        key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TZ"}
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
    ]
    for setting in extra_config:
        command.extend(["-c", setting])
    command.extend(["-C", str(cwd), *args])
    output = bytearray()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise WorktreeError("Git could not be started on this host.") from exc
    assert process.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout_s
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorktreeError("Git exceeded its workspace operation deadline.")
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output) + len(chunk) > max_bytes:
                        raise WorktreeError(
                            "The change receipt exceeds its bounded size; no truncated result was published."
                        )
                    output.extend(chunk)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except BaseException:
        if process.poll() is None:
            # macOS answers EPERM rather than ESRCH once nothing but zombies is left in the group.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise
    finally:
        process.stdout.close()
    if code != 0 and not allow_failure:
        raise WorktreeError(
            "Git refused the workspace operation; the source checkout was not changed."
        )
    return code, bytes(output)


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise WorktreeError("Workspace paths must use valid UTF-8.") from exc


def _head(cwd: Path) -> str:
    value = _text(_git(cwd, ["rev-parse", "--verify", "HEAD^{commit}"], max_bytes=128)[1])
    if not _COMMIT.fullmatch(value):
        raise WorktreeError("The source needs a committed Git base revision.")
    return value


def _common_dir(cwd: Path) -> str:
    value = _text(_git(cwd, ["rev-parse", "--git-common-dir"], max_bytes=16_000)[1])
    path = Path(value)
    return str((cwd / path).resolve() if not path.is_absolute() else path.resolve())


def _reserved(cwd: Path) -> None:
    candidate = cwd / RESERVED_INPUTS
    if candidate.exists() or candidate.is_symlink():
        raise WorktreeError("The source already contains the reserved .pa-inputs path.")


def validate_source(source: Path, *, allow_directory: bool = False) -> dict[str, Any]:
    """Call before claiming work; prepare repeats the check before native startup."""
    source = _directory(source)
    _reserved(source)
    code, inside = _git(
        source, ["rev-parse", "--is-inside-work-tree"], max_bytes=128, allow_failure=True
    )
    if code != 0:
        markers = [(parent / ".git") for parent in (source, *source.parents)]
        if any(marker.exists() or marker.is_symlink() for marker in markers):
            raise WorktreeError("The source has Git metadata that could not be validated.")
        if not allow_directory:
            raise WorktreeError(
                "This is not a Git workspace; explicitly allow direct-directory execution to use it."
            )
        return {
            "mode": "directory",
            "source": str(source),
            "source_root": str(source),
            "source_relative": "",
            "base_commit": None,
            "git_common_dir": None,
        }
    if inside.strip() != b"true":
        raise WorktreeError(
            "Use a Git working checkout, not a bare repository or metadata directory."
        )
    root = Path(_text(_git(source, ["rev-parse", "--show-toplevel"], max_bytes=16_000)[1])).resolve(
        strict=True
    )
    if not source.is_relative_to(root):
        raise WorktreeError("The working directory escaped its Git root.")
    status = _git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=none"],
    )[1]
    if status:
        raise WorktreeError(
            "The source Git checkout is dirty. Commit or move your changes yourself; the host will not stash or commit them."
        )
    return {
        "mode": "git_worktree",
        "source": str(source),
        "source_root": str(root),
        "source_relative": str(source.relative_to(root)) if source != root else "",
        "base_commit": _head(root),
        "git_common_dir": _common_dir(root),
    }


def _read_json(path: Path, maximum: int = MAX_MANIFEST_BYTES) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise WorktreeError("Invalid workspace receipt file.")
        value = json.loads(stream.read(maximum + 1))
    if not isinstance(value, dict):
        raise WorktreeError("Invalid workspace receipt object.")
    return value


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()


def _no_filters(root: Path) -> list[str]:
    _, data = _git(
        root,
        [
            "config",
            "--local",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(smudge|process|required)$",
        ],
        allow_failure=True,
    )
    drivers = set()
    for key in _text(data).splitlines():
        if key.startswith("filter.") and "." in key[7:]:
            drivers.add(key.rsplit(".", 1)[0])
    return [
        setting
        for driver in sorted(drivers)
        for setting in (f"{driver}.smudge=", f"{driver}.process=", f"{driver}.required=false")
    ]


def prepare(
    source: Path,
    profile_dir: Path,
    attempt_id: str,
    *,
    allow_directory: bool = False,
    expected_base_commit: str | None = None,
) -> dict[str, Any]:
    if not _ATTEMPT.fullmatch(attempt_id):
        raise WorktreeError("Invalid workspace attempt identity.")
    profile = _directory(profile_dir, private=True)
    source_info = validate_source(source, allow_directory=allow_directory)
    if expected_base_commit is not None and source_info["base_commit"] != expected_base_commit:
        raise WorktreeError(
            "The source revision changed after preflight; no native run was started."
        )
    root = Path(source_info["source_root"])
    if profile.is_relative_to(root):
        raise WorktreeError("The host profile must be outside the source workspace.")
    workspaces = profile / "worktrees"
    workspaces.mkdir(mode=0o700, exist_ok=True)
    _directory(workspaces, private=True)
    descriptor = workspaces / f"{attempt_id}.json"
    if descriptor.exists() or descriptor.is_symlink():
        saved = _read_json(descriptor)
        if (
            saved.get("source") != source_info["source"]
            or saved.get("attempt_id") != attempt_id
            or saved.get("base_commit") != source_info["base_commit"]
        ):
            raise WorktreeError("The attempt already belongs to another workspace snapshot.")
        _validate_snapshot(saved)
        return saved
    if source_info["mode"] == "git_worktree":
        target = workspaces / attempt_id
        if target.exists() or target.is_symlink():
            raise WorktreeError(
                "An unrecorded worktree already exists; preserve it for reconciliation."
            )
        _git(
            root,
            ["worktree", "add", "--detach", str(target), source_info["base_commit"]],
            extra_config=_no_filters(root),
            timeout_s=60,
        )
        cwd = target / source_info["source_relative"]
        _directory(cwd)
        current = validate_source(source, allow_directory=allow_directory)
        if current["base_commit"] != source_info["base_commit"]:
            raise WorktreeError(
                "The source changed while preparing its worktree; the new worktree was retained."
            )
    else:
        target = Path(source_info["source"])
        cwd = target
    snapshot = {
        **source_info,
        "attempt_id": attempt_id,
        "profile_dir": str(profile),
        "worktree_root": str(target),
        "cwd": str(cwd),
        "descriptor_path": str(descriptor),
        "input_dir": str(cwd / RESERVED_INPUTS / attempt_id),
    }
    _write(descriptor, _json_bytes(snapshot))
    return snapshot


def prepare_worktree(
    cwd: Path, attempt_id: str, profile_dir: Path, **kwargs: Any
) -> dict[str, Any]:
    return prepare(cwd, profile_dir, attempt_id, **kwargs)


def _validate_snapshot(snapshot: Mapping[str, Any]) -> None:
    if not _ATTEMPT.fullmatch(str(snapshot.get("attempt_id", ""))):
        raise WorktreeError("Invalid workspace snapshot.")
    profile = _directory(Path(snapshot["profile_dir"]), private=True)
    root = _directory(Path(snapshot["worktree_root"]))
    cwd = _directory(Path(snapshot["cwd"]))
    if not cwd.is_relative_to(root):
        raise WorktreeError("The execution directory escaped its recorded worktree.")
    if snapshot["mode"] == "git_worktree":
        if root != profile / "worktrees" / snapshot["attempt_id"]:
            raise WorktreeError("The worktree does not belong to this attempt.")
        if (
            not _COMMIT.fullmatch(str(snapshot["base_commit"]))
            or _common_dir(root) != snapshot["git_common_dir"]
        ):
            raise WorktreeError(
                "Git metadata changed; the result cannot be attributed to its source."
            )
    elif snapshot["mode"] != "directory" or str(root) != snapshot["source"]:
        raise WorktreeError("Invalid direct-directory workspace binding.")


def _relative_name(raw: bytes) -> str:
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorktreeError("Output names must use valid UTF-8.") from exc
    path = PurePosixPath(name)
    if (
        not name
        or len(name.encode()) > 1000
        or path.is_absolute()
        or "\\" in name
        or any(part in {"", ".", "..", ".git"} for part in name.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise WorktreeError("An output has an unsupported or unsafe relative filename.")
    return name


def _file(root: Path, name: str) -> tuple[bytes, int]:
    try:
        fd, before = safefs.open_regular(root, tuple(PurePosixPath(name).parts))
        with os.fdopen(fd, "rb") as stream:
            if before.st_size > MAX_NEW_BYTES:
                raise WorktreeError("A new output file exceeds its bounded size.")
            data = stream.read(MAX_NEW_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_NEW_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_size,
        ) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
            raise WorktreeError("An output changed while its immutable receipt was captured.")
        return data, 0o755 if before.st_mode & 0o111 else 0o644
    except OSError as exc:
        raise WorktreeError(
            "New outputs must be ordinary files, with no symlink traversal."
        ) from exc


def _artifact(path: Path, data: bytes) -> dict[str, Any]:
    _write(path, data)
    return {"name": path.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def collect(snapshot: Mapping[str, Any]) -> list[Path]:
    """Call after a confirmed native stop. Retain the checkout and all unaccepted work."""
    _validate_snapshot(snapshot)
    profile = Path(snapshot["profile_dir"])
    descriptor = profile / "worktrees" / f"{snapshot['attempt_id']}.json"
    if _read_json(descriptor) != dict(snapshot):
        raise WorktreeError("The workspace snapshot differs from its durable descriptor.")
    results = profile / "results"
    results.mkdir(mode=0o700, exist_ok=True)
    _directory(results, private=True)
    folder = results / str(snapshot["attempt_id"])
    folder.mkdir(mode=0o700, exist_ok=True)
    _directory(folder, private=True)
    seal = folder / "receipt.json"
    if seal.exists():
        sealed_records = _read_json(seal).get("files", [])
        if not isinstance(sealed_records, list) or not 1 <= len(sealed_records) <= 3:
            raise WorktreeError("Invalid sealed artifact inventory.")
        paths = []
        for item in sealed_records:
            name = _relative_name(str(item["name"]).encode())
            if name not in {"changes.patch", "new-files.tar", "changes.json"}:
                raise WorktreeError("Invalid sealed result path.")
            data, _ = _file_bounded(folder, name, MAX_DIFF_BYTES)
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise WorktreeError("A sealed result artifact changed.")
            paths.append(folder / name)
        return paths
    if any(folder.iterdir()):
        raise WorktreeError("An incomplete result capture exists; preserve it for reconciliation.")
    manifest: dict[str, Any] = {
        "schema": 1,
        "mode": snapshot["mode"],
        "base_commit": snapshot["base_commit"],
        "source_relative": snapshot["source_relative"],
        "new_files": [],
        "untracked_policy": "non-ignored ordinary files; reserved inputs excluded",
        "reserved_inputs": str(PurePosixPath(snapshot["source_relative"]) / RESERVED_INPUTS),
    }
    records: list[dict[str, Any]] = []
    if snapshot["mode"] == "git_worktree":
        root = Path(snapshot["worktree_root"])
        reserved = manifest["reserved_inputs"]
        patch = _git(
            root,
            [
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                str(snapshot["base_commit"]),
                "--",
                ".",
                f":(exclude,literal){reserved}",
            ],
            max_bytes=MAX_DIFF_BYTES,
        )[1]
        manifest["head_commit"] = _head(root)
        raw_names = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"])[1]
        new_files: list[tuple[str, bytes, int]] = []
        total = 0
        for raw in raw_names.split(b"\x00"):
            if not raw:
                continue
            name = _relative_name(raw)
            if name == reserved or name.startswith(reserved + "/"):
                continue
            data, mode = _file(root, name)
            total += len(data)
            if total > MAX_NEW_BYTES or len(new_files) >= MAX_NEW_FILES:
                raise WorktreeError("New output files exceed the bounded artifact budget.")
            new_files.append((name, data, mode))
        if patch:
            manifest["patch"] = _artifact(folder / "changes.patch", patch)
            records.append(manifest["patch"])
        if new_files:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as bundle:
                for name, data, mode in new_files:
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = len(data), mode, 0
                    info.uid = info.gid = 0
                    bundle.addfile(info, io.BytesIO(data))
                    manifest["new_files"].append(
                        {
                            "path": name,
                            "size": len(data),
                            "mode": oct(mode),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                    )
            records.append(_artifact(folder / "new-files.tar", archive.getvalue()))
        manifest["isolated_working_files"] = True
        manifest["os_sandbox"] = False
    else:
        manifest["isolated_working_files"] = False
        manifest["os_sandbox"] = False
        manifest["notice"] = (
            "Explicit direct-directory mode: no Git snapshot or automatic change capture is available."
        )
    document = _json_bytes(manifest)
    if len(document) > MAX_MANIFEST_BYTES:
        raise WorktreeError("The change manifest exceeds its bounded size.")
    records.append(_artifact(folder / "changes.json", document))
    _write(seal, _json_bytes({"files": records}))
    return [folder / item["name"] for item in records]


def _file_bounded(root: Path, name: str, maximum: int) -> tuple[bytes, int]:
    fd, info = safefs.open_regular(root, (name,))
    with os.fdopen(fd, "rb") as stream:
        if info.st_size > maximum:
            raise WorktreeError("A sealed result exceeds its size bound.")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise WorktreeError("A sealed result exceeds its size bound.")
    return data, info.st_mode


finish_run = collect
