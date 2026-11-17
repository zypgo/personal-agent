"""What a plan and its files must satisfy before a node is given them, and before it runs them.

The control plane refuses a bad plan early, and the node does not trust what it is sent: both ends
check the same rules, so each rule below is pinned on both. A node can be somebody else's computer,
so these are the limits between a task's files and everything else on it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from personal_agent.execution import ExecutionError
from personal_agent.execution.models import make_plan, validate_manifest
from personal_agent.nodes.transfers import collect_outputs, materialize, validate_envelope

MIB = 1024 * 1024


def entry(path: str, content: bytes = b"hello") -> dict[str, Any]:
    return {
        "path": path,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }


def envelope(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "attempt_id": "attempt_1",
        "job_id": "job_1",
        "space_id": "space_1",
        "generation": 1,
        "plan_hash": "a" * 64,
        "command": "true",
        "timeout_s": 60,
        "network": "none",
        "input_manifest": [],
        "output_paths": [],
    }
    value.update(changes)
    return value


UNSAFE_PATHS = [
    "../escape",
    "a/../b",
    "/absolute",
    "a//b",
    "./a",
    "a/",
    "a\\b",
    "a\x00b",
    "a\nb",
    "a\tb",
    "a\x7fb",
    "x" * 501,
    ".git/config",
    ".ssh/id_ed25519",
    ".aws/credentials",
    ".gnupg/private-keys",
    ".pa-jobs/attempt/input.json",
    ".env",
    ".env.production",
    "project/.env.local",
    "deep/.ssh/id_rsa",
]
ORDINARY_PATHS = ["notes.txt", "data/2026/report.csv", ".gitignore", ".envrc", "my.env", "env"]


@pytest.mark.parametrize("path", UNSAFE_PATHS)
def test_the_control_plane_refuses_a_path_that_leaves_the_task_or_names_a_credential(path):
    with pytest.raises(ExecutionError):
        make_plan("true", input_manifest=[entry(path)])
    with pytest.raises(ExecutionError):
        make_plan("true", output_paths=[path])


@pytest.mark.parametrize("path", UNSAFE_PATHS)
def test_the_node_refuses_the_same_paths(path, tmp_path):
    with pytest.raises(ValueError):
        validate_envelope(envelope(input_manifest=[entry(path)]))
    with pytest.raises(ValueError):
        validate_envelope(envelope(output_paths=[path]))
    with pytest.raises(ValueError):
        materialize(tmp_path / "attempt", envelope(input_manifest=[entry(path)]))
    assert not (tmp_path / "attempt" / "workspace").exists()


@pytest.mark.parametrize("path", ORDINARY_PATHS)
def test_names_that_only_look_like_a_credential_are_not_refused(path, tmp_path):
    plan = make_plan("true", input_manifest=[entry(path)], output_paths=[path])
    assert plan["output_paths"] == [path]
    validate_envelope(envelope(input_manifest=[entry(path)], output_paths=[path]))
    assert (materialize(tmp_path / "attempt", envelope(input_manifest=[entry(path)]))).is_dir()


def test_a_manifest_names_at_most_64_files_and_no_file_twice(tmp_path):
    many = [entry(f"f{i}", bytes([i])) for i in range(65)]
    assert len(validate_manifest(many[:64])) == 64
    with pytest.raises(ExecutionError):
        validate_manifest(many)
    with pytest.raises(ExecutionError):
        validate_manifest([entry("a"), entry("a")])
    with pytest.raises(ExecutionError):
        make_plan("true", output_paths=["a", "a"])

    validate_envelope(envelope(input_manifest=many[:64]))
    with pytest.raises(ValueError, match="too many"):
        validate_envelope(envelope(input_manifest=many))
    with pytest.raises(ValueError, match="too many"):
        validate_envelope(envelope(output_paths=[f"o{i}" for i in range(65)]))
    with pytest.raises(ValueError, match="too many"):
        collect_outputs(tmp_path, [f"o{i}" for i in range(65)])


def test_a_file_must_be_what_its_manifest_says_it_is(tmp_path):
    wrong_size = entry("a")
    wrong_size["size"] = 4
    wrong_hash = entry("a")
    wrong_hash["sha256"] = "0" * 64
    too_big = entry("a", b"x" * (2 * MIB + 1))
    not_base64 = {**entry("a"), "content_base64": "not base64!"}
    for bad in (wrong_size, wrong_hash, too_big, not_base64):
        with pytest.raises(ExecutionError):
            validate_manifest([bad])
        with pytest.raises(ValueError):
            validate_envelope(envelope(input_manifest=[bad]))
    assert validate_manifest([entry("a", b"x" * (2 * MIB))])[0]["size"] == 2 * MIB


def test_all_the_files_of_a_task_are_limited_to_8_mib_each_way(tmp_path):
    files = [entry(f"f{i}", bytes([i + 1]) * (2 * MIB)) for i in range(5)]
    assert len(validate_manifest(files[:4])) == 4
    with pytest.raises(ExecutionError):
        validate_manifest(files)
    validate_envelope(envelope(input_manifest=files[:4]))
    with pytest.raises(ValueError, match="quota"):
        validate_envelope(envelope(input_manifest=files))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for i in range(5):
        (workspace / f"o{i}").write_bytes(bytes([i + 1]) * (2 * MIB))
    assert len(collect_outputs(workspace, [f"o{i}" for i in range(4)])) == 4
    with pytest.raises(ValueError, match="quota"):
        collect_outputs(workspace, [f"o{i}" for i in range(5)])


@pytest.mark.parametrize("timeout", [0, 0.5, 3601, 10**9, True, float("inf"), float("nan"), "60"])
def test_a_task_runs_for_at_most_an_hour(timeout):
    with pytest.raises(ExecutionError):
        make_plan("true", timeout_s=timeout)
    with pytest.raises(ValueError):
        validate_envelope(envelope(timeout_s=timeout))


def test_the_longest_and_shortest_allowed_deadlines_are_accepted():
    assert make_plan("true", timeout_s=3600)["timeout_s"] == 3600.0
    assert make_plan("true", timeout_s=1)["timeout_s"] == 1.0
    validate_envelope(envelope(timeout_s=3600))
    validate_envelope(envelope(timeout_s=1))


def test_a_plan_never_asks_for_the_network():
    plan = make_plan("true")
    assert plan["network"] == "none"
    assert "network_none" in plan["required_capabilities"]
    with pytest.raises(ValueError, match="network"):
        validate_envelope(envelope(network="host"))


def test_a_workspace_is_reused_only_for_the_plan_that_made_it(tmp_path):
    attempt = tmp_path / "attempt"
    first = envelope(input_manifest=[entry("a.txt")])
    workspace = materialize(attempt, first)
    assert materialize(attempt, first) == workspace

    with pytest.raises(ValueError, match="different plan"):
        materialize(attempt, envelope(plan_hash="b" * 64, input_manifest=[entry("a.txt")]))

    # The marker is what proves the publication finished: without it nothing is reused.
    (attempt / "input.json").unlink()
    with pytest.raises(ValueError, match="could not be verified"):
        materialize(attempt, first)


def test_a_workspace_that_is_a_link_is_not_reused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "workspace").symlink_to(elsewhere)
    (attempt / "input.json").write_text(json.dumps({"plan_hash": "a" * 64}))
    with pytest.raises(ValueError, match="could not be verified"):
        materialize(attempt, envelope())


def test_a_marker_that_is_a_link_proves_nothing(tmp_path):
    attempt = tmp_path / "attempt"
    materialize(attempt, envelope())
    elsewhere = tmp_path / "elsewhere.json"
    (attempt / "input.json").replace(elsewhere)
    (attempt / "input.json").symlink_to(elsewhere)
    assert json.loads(elsewhere.read_text()) == {"plan_hash": "a" * 64}
    with pytest.raises(ValueError, match="could not be verified"):
        materialize(attempt, envelope())


def test_outputs_are_regular_bounded_files_reached_without_following_links(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "big").write_bytes(b"x" * (2 * MIB + 1))
    with pytest.raises(ValueError, match="bounded regular file"):
        collect_outputs(workspace, ["big"])
    (workspace / "limit").write_bytes(b"x" * (2 * MIB))
    assert collect_outputs(workspace, ["limit"])[0]["size"] == 2 * MIB

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (workspace / "folder").symlink_to(outside)
    with pytest.raises(OSError):
        collect_outputs(workspace, ["folder/secret"])

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a").write_text("a")
    (tmp_path / "workspace-link").symlink_to(elsewhere)
    with pytest.raises(OSError):
        collect_outputs(tmp_path / "workspace-link", ["a"])


def test_an_output_that_changes_while_it_is_read_is_not_collected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "growing.txt"
    target.write_bytes(b"stable")
    real_fdopen = os.fdopen

    class Touched:
        """Reads the file, then touches it: the same bytes, a new modification time."""

        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> Touched:
            self.stream.__enter__()
            return self

        def __exit__(self, *exc: Any) -> Any:
            return self.stream.__exit__(*exc)

        def fileno(self) -> int:
            return int(self.stream.fileno())

        def read(self, size: int = -1) -> bytes:
            data = bytes(self.stream.read(size))
            later = Path(target).stat().st_mtime_ns + 5_000_000_000
            os.utime(target, ns=(later, later))
            return data

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", lambda fd, mode="r": Touched(real_fdopen(fd, mode)))
        with pytest.raises(ValueError, match="changed while being collected"):
            collect_outputs(workspace, ["growing.txt"])
    assert collect_outputs(workspace, ["growing.txt"])[0]["size"] == 6
