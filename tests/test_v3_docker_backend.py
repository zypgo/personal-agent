"""The container a node starts for an attempt, and the proof that a container is the attempt's own.

A node is somebody's own computer. What runs there is arbitrary code from a task, so the container
is closed in every way Docker allows without breaking a plain command, and the node only ever acts
on a container whose labels say it was created for this very attempt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from personal_agent.nodes.backend import BackendError, DockerBackend, Observation
from personal_agent.nodes.store import NodeStore, Receipt


def accepted(tmp_path: Path) -> Receipt:
    payload = {
        "attempt_id": "attempt_1",
        "job_id": "job_1",
        "task_id": "task_1",
        "space_id": "space_1",
        "generation": 3,
        "plan_hash": "a" * 64,
        "command": "true",
        "timeout_s": 60,
        "network": "none",
        "deadline_at": 1060.0,
        "input_manifest": [],
        "output_paths": [],
    }
    with NodeStore(tmp_path) as store:
        return store.accept(payload, now=1000)


def values(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == name]


def test_the_container_has_no_network_no_privileges_and_hard_resource_limits(tmp_path):
    receipt = accepted(tmp_path)
    workspace = tmp_path / "workspace"
    args = DockerBackend(memory_mb=256, cpus=0.5).create_argv(receipt, workspace)

    assert args[0] == "create"
    assert values(args, "--network") == ["none"]
    assert "--read-only" in args
    assert values(args, "--cap-drop") == ["ALL"] and "--cap-add" not in args
    assert values(args, "--security-opt") == ["no-new-privileges"]
    assert "--privileged" not in args
    assert values(args, "--pids-limit") == ["128"]
    assert values(args, "--memory") == ["256m"]
    # Equal to the memory limit means the container has no swap to hide behind.
    assert values(args, "--memory-swap") == ["256m"]
    assert values(args, "--cpus") == ["0.5"]
    assert sorted(values(args, "--ulimit")) == ["fsize=67108864:67108864", "nofile=256:256"]
    assert "--init" in args
    assert "--pull=never" in args
    assert values(args, "--user")[0] not in {"0", "0:0"}
    assert values(args, "--tmpfs") == ["/tmp:rw,nosuid,size=128m"]
    assert values(args, "--mount") == [f"type=bind,src={workspace.resolve()},dst=/workspace"]
    assert values(args, "-w") == ["/workspace"]


def test_the_container_carries_the_labels_that_prove_whose_it_is(tmp_path):
    receipt = accepted(tmp_path)
    args = DockerBackend().create_argv(receipt, tmp_path / "workspace")
    assert values(args, "--label") == [
        f"pa.attempt={receipt.attempt_id}",
        f"pa.plan={receipt.plan_hash}",
        f"pa.generation={receipt.generation}",
    ]
    assert values(args, "--name") == [receipt.resource_id]


def test_the_limits_come_from_the_node_owner_and_stay_in_range():
    with pytest.raises(ValueError):
        DockerBackend(memory_mb=64)
    with pytest.raises(ValueError):
        DockerBackend(memory_mb=8192)
    with pytest.raises(ValueError):
        DockerBackend(cpus=0)
    with pytest.raises(ValueError):
        DockerBackend(cpus=16)
    with pytest.raises(ValueError):
        DockerBackend(image="--privileged")
    with pytest.raises(ValueError):
        DockerBackend(image="")


def docker_says(monkeypatch: pytest.MonkeyPatch, receipt: Receipt, **wrong: str) -> list[list[str]]:
    """Make `docker inspect` answer with this attempt's labels, except those given."""
    labels = {
        "pa.attempt": receipt.attempt_id,
        "pa.plan": receipt.plan_hash,
        "pa.generation": str(receipt.generation),
        **wrong,
    }
    asked: list[list[str]] = []

    async def run(self: DockerBackend, args: list[str], **_: Any) -> tuple[int, str, str]:
        asked.append(args)
        if args[0] != "inspect":
            return 0, "", ""
        item = {
            "Config": {"Labels": labels},
            "State": {"Status": "exited", "ExitCode": 0},
        }
        return 0, json.dumps(item), ""

    monkeypatch.setattr(DockerBackend, "_run", run)
    return asked


async def test_a_container_that_is_the_attempts_own_is_adopted(tmp_path, monkeypatch):
    receipt = accepted(tmp_path)
    docker_says(monkeypatch, receipt)
    assert await DockerBackend().inspect(receipt) == Observation("exited", 0)


@pytest.mark.parametrize(
    "wrong",
    [
        {"pa.attempt": "attempt_2"},
        {"pa.plan": "b" * 64},
        {"pa.generation": "4"},
        {"pa.attempt": ""},
    ],
)
async def test_a_container_of_another_attempt_plan_or_generation_is_never_touched(
    wrong, tmp_path, monkeypatch
):
    receipt = accepted(tmp_path)
    asked = docker_says(monkeypatch, receipt, **wrong)
    backend = DockerBackend()
    for action in (
        backend.inspect,
        backend.start,
        backend.stop,
        backend.logs,
        backend.cleanup,
        lambda r: backend.create(r, tmp_path / "workspace"),
    ):
        with pytest.raises(BackendError, match="ownership"):
            await action(receipt)
    # Nothing but looking was ever asked of Docker.
    assert {args[0] for args in asked} == {"inspect"}
