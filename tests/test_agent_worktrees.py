"""Real Git worktrees preserve source changes and produce bounded, reviewable receipts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from personal_agent.agent_adapters import worktrees
from personal_agent.agent_adapters.worktrees import WorktreeError, collect, prepare, validate_source


def git(cwd: Path, *args: str) -> str:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(cwd), *args],
        env=environment,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode().strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path]:
    source, profile = tmp_path / "source", tmp_path / "profile"
    source.mkdir()
    profile.mkdir(mode=0o700)
    git(source, "init", "-q")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "tracked.txt").write_text("Original baseline.\n")
    (source / "binary.dat").write_bytes(b"\x00\x01original\x00")
    (source / ".gitignore").write_text(".env\nignored-output\n")
    (source / "sub[1]").mkdir()
    (source / "sub[1]" / "nested.txt").write_text("Nested baseline.\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Synthetic baseline")
    return source, profile


@pytest.mark.parametrize("dirty", ["tracked", "staged", "untracked"])
def test_dirty_source_is_rejected_without_committing_or_stashing(
    repository: tuple[Path, Path], dirty: str
) -> None:
    source, profile = repository
    before = git(source, "rev-parse", "HEAD")
    if dirty == "untracked":
        (source / "untracked.txt").write_text("Uncommitted local work.")
    else:
        (source / "tracked.txt").write_text("Uncommitted local work.")
        if dirty == "staged":
            git(source, "add", "tracked.txt")
    status = git(source, "status", "--porcelain")
    with pytest.raises(WorktreeError, match="dirty"):
        prepare(source, profile, "attempt-1")
    assert git(source, "status", "--porcelain") == status
    assert git(source, "rev-parse", "HEAD") == before
    assert git(source, "stash", "list") == ""
    assert not (profile / "worktrees").exists()


def test_prepared_worktree_is_detached_and_does_not_copy_ignored_credentials(
    repository: tuple[Path, Path],
) -> None:
    source, profile = repository
    (source / ".env").write_text("SYNTHETIC_ONLY=value-never-copied\n")
    observed = validate_source(source)
    snapshot = prepare(source, profile, "attempt-1", expected_base_commit=observed["base_commit"])
    cwd = Path(snapshot["cwd"])
    assert cwd != source and snapshot["mode"] == "git_worktree"
    assert git(cwd, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git(cwd, "rev-parse", "HEAD") == observed["base_commit"]
    assert not (cwd / ".env").exists()
    (cwd / "tracked.txt").write_text("Agent changes its own working file.\n")
    assert (source / "tracked.txt").read_text() == "Original baseline.\n"
    assert git(source, "status", "--porcelain") == ""
    assert prepare(source, profile, "attempt-1") == snapshot


def test_source_revision_is_frozen_between_preflight_and_admission(
    repository: tuple[Path, Path],
) -> None:
    source, profile = repository
    observed = validate_source(source)
    (source / "tracked.txt").write_text("New baseline.\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-qm", "Synthetic new baseline")
    with pytest.raises(WorktreeError, match="revision changed"):
        prepare(source, profile, "attempt-1", expected_base_commit=observed["base_commit"])
    assert not (profile / "worktrees").exists()


def test_collect_preserves_binary_patch_and_new_files_without_applying_them(
    repository: tuple[Path, Path],
) -> None:
    source, profile = repository
    snapshot = prepare(source, profile, "attempt-1")
    cwd = Path(snapshot["cwd"])
    (cwd / "tracked.txt").write_text("Reviewed change.\n")
    (cwd / "binary.dat").write_bytes(b"\x00new binary\xff\x00")
    (cwd / "new.txt").write_text("New output.\n")
    (cwd / "ignored-output").write_text("Ignored local output is not exported.")
    paths = collect(snapshot)
    assert {path.name for path in paths} == {"changes.patch", "new-files.tar", "changes.json"}
    patch = next(path for path in paths if path.suffix == ".patch")
    assert b"GIT binary patch" in patch.read_bytes()
    git(source, "apply", "--check", str(patch))
    manifest = json.loads(next(path for path in paths if path.name == "changes.json").read_text())
    assert manifest["base_commit"] == snapshot["base_commit"]
    assert manifest["isolated_working_files"] and not manifest["os_sandbox"]
    assert [item["path"] for item in manifest["new_files"]] == ["new.txt"]
    assert manifest["new_files"][0]["sha256"] == hashlib.sha256(b"New output.\n").hexdigest()
    assert str(source) not in json.dumps(manifest) and str(profile) not in json.dumps(manifest)
    with tarfile.open(next(path for path in paths if path.suffix == ".tar")) as archive:
        assert archive.getnames() == ["new.txt"]
        stream = archive.extractfile("new.txt")
        assert stream is not None and stream.read() == b"New output.\n"
    assert (source / "tracked.txt").read_text() == "Original baseline.\n"
    assert not (source / "new.txt").exists()
    assert cwd.is_dir()  # results are retained until a separate explicit review/cleanup


def test_reserved_inputs_are_excluded_even_if_native_agent_staged_them(
    repository: tuple[Path, Path],
) -> None:
    source, profile = repository
    snapshot = prepare(source / "sub[1]", profile, "attempt-1")
    cwd = Path(snapshot["cwd"])
    inputs = Path(snapshot["input_dir"])
    inputs.mkdir(parents=True)
    (inputs / "accepted.txt").write_text("PEER_INPUT_MUST_NOT_BE_EXPORTED")
    (cwd / "output.txt").write_text("New result.")
    git(cwd, "add", ".pa-inputs")
    paths = collect(snapshot)
    for path in paths:
        if path.suffix in {".patch", ".json"}:
            assert b"PEER_INPUT_MUST_NOT_BE_EXPORTED" not in path.read_bytes()
    archive_path = next(path for path in paths if path.suffix == ".tar")
    with tarfile.open(archive_path) as archive:
        assert archive.getnames() == ["sub[1]/output.txt"]
    assert Path(snapshot["cwd"]).relative_to(Path(snapshot["worktree_root"])).as_posix() == "sub[1]"


def test_source_reserved_input_path_and_profile_inside_source_are_rejected(
    repository: tuple[Path, Path],
) -> None:
    source, _profile = repository
    (source / ".pa-inputs").mkdir()
    with pytest.raises(WorktreeError, match="reserved"):
        validate_source(source)
    (source / ".pa-inputs").rmdir()
    nested_profile = source / "ignored-profile"
    nested_profile.mkdir(mode=0o700)
    # Marking a host profile ignored cannot turn it into a safe external state directory.
    with (source / ".git" / "info" / "exclude").open("a") as stream:
        stream.write("\nignored-profile\n")
    with pytest.raises(WorktreeError, match="outside"):
        prepare(source, nested_profile, "attempt-1")


def test_new_symlink_outputs_are_not_followed_or_archived(
    repository: tuple[Path, Path], tmp_path: Path
) -> None:
    source, profile = repository
    snapshot = prepare(source, profile, "attempt-1")
    outside = tmp_path / "outside-data"
    outside.write_text("Outside private fixture.")
    (Path(snapshot["cwd"]) / "link.txt").symlink_to(outside)
    with pytest.raises(WorktreeError, match="ordinary files"):
        collect(snapshot)
    assert outside.read_text() == "Outside private fixture."
    assert Path(snapshot["cwd"]).is_dir()


def test_patch_and_new_file_limits_refuse_truncation(
    repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, profile = repository
    snapshot = prepare(source, profile, "attempt-1")
    (Path(snapshot["cwd"]) / "tracked.txt").write_text("A" * 1000)
    monkeypatch.setattr(worktrees, "MAX_DIFF_BYTES", 100)
    with pytest.raises(WorktreeError, match="bounded"):
        collect(snapshot)
    assert not (profile / "results" / "attempt-1" / "receipt.json").exists()
    second = prepare(source, profile, "attempt-2")
    (Path(second["cwd"]) / "large.txt").write_bytes(b"a" * 21)
    monkeypatch.setattr(worktrees, "MAX_NEW_BYTES", 20)
    with pytest.raises(WorktreeError, match="bounded"):
        collect(second)


def test_sealed_collection_is_reused_and_tampering_never_recaptures(
    repository: tuple[Path, Path],
) -> None:
    source, profile = repository
    snapshot = prepare(source, profile, "attempt-1")
    cwd = Path(snapshot["cwd"])
    (cwd / "tracked.txt").write_text("First final change.\n")
    paths = collect(snapshot)
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    (cwd / "tracked.txt").write_text("Later mutation outside the receipt.\n")
    assert collect(snapshot) == paths
    assert hashes == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    next(path for path in paths if path.suffix == ".patch").write_text("tampered")
    with pytest.raises(WorktreeError, match="changed"):
        collect(snapshot)
    assert cwd.is_dir()


def test_non_git_mode_is_explicit_and_does_not_claim_change_isolation(tmp_path: Path) -> None:
    source, profile = tmp_path / "directory", tmp_path / "profile"
    source.mkdir()
    profile.mkdir(mode=0o700)
    with pytest.raises(WorktreeError, match="explicitly allow"):
        prepare(source, profile, "attempt-1")
    snapshot = prepare(source, profile, "attempt-1", allow_directory=True)
    assert snapshot["mode"] == "directory" and snapshot["cwd"] == str(source)
    paths = collect(snapshot)
    assert len(paths) == 1
    manifest = json.loads(paths[0].read_text())
    assert not manifest["isolated_working_files"] and manifest["base_commit"] is None


def test_checkout_hooks_and_smudge_commands_do_not_run(
    repository: tuple[Path, Path], tmp_path: Path
) -> None:
    source, profile = repository
    marker = tmp_path / "hook-or-filter-ran"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.write_text(f'#!/bin/sh\nprintf unsafe > "{marker}"\n')
    hook.chmod(0o755)
    (source / ".gitattributes").write_text("*.dat filter=sneaky\n")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-qm", "Synthetic attribute configuration")
    git(source, "config", "filter.sneaky.smudge", f"sh -c 'printf unsafe > \"{marker}\"; cat'")
    snapshot = prepare(source, profile, "attempt-1")
    assert not marker.exists()
    assert (Path(snapshot["cwd"]) / "binary.dat").read_bytes() == (
        source / "binary.dat"
    ).read_bytes()


def test_snapshot_descriptor_cannot_be_rebound_to_another_checkout(
    repository: tuple[Path, Path],
) -> None:
    source, profile = repository
    snapshot = prepare(source, profile, "attempt-1")
    modified: dict[str, Any] = {**snapshot, "worktree_root": str(source), "cwd": str(source)}
    with pytest.raises(WorktreeError, match="does not belong"):
        collect(modified)
