"""The checks between a person's task and a node that runs it, one at a time.

A node is somebody's computer and the control plane cannot see what happens on it, so every step a
node takes is checked against what the owner approved: the plan, the moment, the node, the exit
status, the files. Each test breaks exactly one of those rules and expects the store to hold.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest

from personal_agent.core.artifacts import ArtifactStore
from personal_agent.execution import ExecutionConflict, ExecutionError
from personal_agent.execution.models import CAPABILITIES
from personal_agent.execution.schema import migrate
from personal_agent.execution.store import ExecutionStore

WEEK = 7 * 86400


@pytest.fixture
def jobs(db, clock):
    if db.one("SELECT 1 FROM sqlite_master WHERE name='execution_tasks'") is None:
        with db.tx():
            migrate(db._conn)
    return ExecutionStore(db, clock)


def submit(jobs, request_id="r1", actor="alice", **kw):
    return jobs.create_task(
        title="Check input", command="printf hello", actor_id=actor, request_id=request_id, **kw
    )


def approve(jobs, task):
    job = task["jobs"][-1]
    jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    return job


def start(jobs, task=None, node="node-a", **claim):
    task = task or submit(jobs)
    approve(jobs, task)
    envelope = jobs.claim(node, CAPABILITIES, **claim)
    jobs.admit(
        envelope["attempt_id"],
        envelope["generation"],
        plan_hash=envelope["plan_hash"],
        node_id=node,
        resource_id="container-" + envelope["attempt_id"],
    )
    return task, envelope


def finish(jobs, env, **kw):
    kw.setdefault("status", "succeeded")
    kw.setdefault("exit_code", 0)
    return jobs.complete(env["attempt_id"], env["generation"], node_id="node-a", **kw)


def file_item(path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }


# ---- approving: the plan, the person, the moment ----


def test_a_job_is_not_approved_after_its_deadline(jobs, clock):
    job = submit(jobs)["jobs"][0]
    clock.advance(WEEK + 1)
    with pytest.raises(ExecutionConflict, match="expired"):
        jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    assert jobs.get_job(job["id"])["state"] == "awaiting_approval"


def test_work_saved_under_an_authorization_is_not_run_without_one(jobs):
    jobs.authorization_snapshot = lambda _actor: {"membership": 1, "principal": 1}
    job = submit(jobs)["jobs"][0]
    # A store that no longer knows who may do what must not read that as "everybody may".
    jobs.authorization_snapshot = None
    with pytest.raises(ExecutionConflict, match="authorization"):
        jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    assert jobs.get_job(job["id"])["state"] == "awaiting_approval"


# ---- claiming: what a node is given, and when it is given nothing ----


def test_a_job_is_not_given_to_a_node_after_its_deadline(jobs, clock):
    approve(jobs, submit(jobs))
    clock.advance(WEEK + 1)
    assert jobs.claim("node-a", CAPABILITIES) is None


def test_a_changed_grant_of_the_node_stops_the_work_it_is_running(jobs):
    _task, env = start(jobs)
    assert jobs.claim("node-a", CAPABILITIES, node_version=1)["attempt_id"] == env["attempt_id"]
    assert jobs.claim("node-a", CAPABILITIES, node_version=2) is None
    assert jobs.get_attempt(env["attempt_id"])["desired_state"] == "cancel"


def test_a_node_whose_attempt_is_uncertain_is_not_given_other_work(jobs, clock):
    _task, env = start(jobs)
    second = submit(jobs, request_id="second")
    other = approve(jobs, second)
    clock.advance(100)
    assert jobs.reconcile()["uncertain"] == 1
    # The first attempt may still be running there: the node hears about that one again, and
    # nothing else starts on it until the owner has settled it.
    assert jobs.claim("node-a", CAPABILITIES)["attempt_id"] == env["attempt_id"]
    assert jobs.get_job(other["id"])["state"] == "queued"


# ---- reporting: what a node may say about its attempt ----


def test_admission_has_to_name_the_approved_plan(jobs):
    approve(jobs, submit(jobs))
    env = jobs.claim("node-a", CAPABILITIES)
    with pytest.raises(ExecutionConflict, match="plan hash"):
        jobs.admit(
            env["attempt_id"], 1, plan_hash="0" * 64, node_id="node-a", resource_id="container"
        )
    assert not jobs.get_attempt(env["attempt_id"])["resource_id"]


def test_output_is_not_accepted_before_the_node_admitted_its_resource(jobs):
    approve(jobs, submit(jobs))
    env = jobs.claim("node-a", CAPABILITIES)
    with pytest.raises(ExecutionConflict, match="admit"):
        jobs.observe(
            env["attempt_id"], 1, node_id="node-a", state="running", sequence=1, stdout="hello"
        )
    assert jobs.logs(env["attempt_id"]) == []
    # Saying it does not know where things stand is always allowed.
    jobs.observe(env["attempt_id"], 1, node_id="node-a", state="uncertain")


def test_a_late_heartbeat_does_not_move_collecting_back_to_running(jobs):
    _task, env = start(jobs)
    jobs.observe(env["attempt_id"], 1, node_id="node-a", state="collecting")
    late = jobs.observe(env["attempt_id"], 1, node_id="node-a", state="running")
    assert late["state"] == "collecting"
    assert jobs.get_job(env["job_id"])["state"] == "collecting"


def test_an_uncertain_attempt_is_running_again_once_the_node_says_so(jobs):
    _task, env = start(jobs)
    assert jobs.observe(env["attempt_id"], 1, node_id="node-a", state="uncertain")["state"] == (
        "uncertain"
    )
    assert jobs.observe(env["attempt_id"], 1, node_id="node-a", state="running")["state"] == (
        "running"
    )


@pytest.mark.parametrize("exit_code", [None, 1, -1, 255])
def test_success_needs_a_confirmed_zero_exit(jobs, exit_code):
    _task, env = start(jobs)
    with pytest.raises(ExecutionError, match="zero exit"):
        finish(jobs, env, status="succeeded", exit_code=exit_code)
    assert jobs.get_attempt(env["attempt_id"])["state"] == "admitted"


def test_a_file_the_approved_plan_never_named_is_not_collected(jobs, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jobs.workspace = workspace
    jobs.artifacts = ArtifactStore(jobs.db, tmp_path / "artifacts", workspace, clock=jobs.clock)
    _task, env = start(jobs, submit(jobs, output_paths=["result.txt"]))
    with pytest.raises(ExecutionError, match="approved plan"):
        finish(jobs, env, artifacts=[file_item("other.txt", b"secret")])
    assert jobs.get_attempt(env["attempt_id"])["state"] == "admitted"
    assert jobs.db.scalar("SELECT COUNT(*) FROM artifacts") == 0
    assert not (workspace / ".pa-jobs").exists()


@pytest.mark.parametrize("reason", ["revised", "revoked"])
def test_a_file_that_nobody_may_receive_any_more_is_not_queued_for_sending(jobs, tmp_path, reason):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jobs.workspace = workspace
    jobs.artifacts = ArtifactStore(jobs.db, tmp_path / "artifacts", workspace, clock=jobs.clock)
    task = submit(jobs, channel="telegram", recipient="chat-1", output_paths=["result.txt"])
    _task, env = start(jobs, task)
    if reason == "revised":
        jobs.revise_task(task["id"], actor_id="alice", expected_revision=1, title="New goal")
        finish(jobs, env, artifacts=[file_item("result.txt", b"result")])
    else:
        finish(jobs, env, deliver=False, artifacts=[file_item("result.txt", b"result")])

    # The file is kept for the web page of the owner; nothing is addressed to the chat, and the
    # web notice is cancelled too, so no channel announces a result nobody asked for any more.
    [row] = jobs.db.query("SELECT channel, recipient, status FROM outbox WHERE kind='artifact'")
    assert (row["channel"], row["recipient"], row["status"]) == ("web", None, "cancelled")
    assert jobs.db.scalar("SELECT COUNT(*) FROM outbox WHERE status='pending'") == 0


# ---- cancelling and revising ----


def test_only_the_requester_or_an_authorized_owner_cancels_a_task(jobs):
    task = submit(jobs)
    with pytest.raises(ExecutionError, match="requester"):
        jobs.cancel_task(task["id"], actor_id="mallory")
    assert jobs.get_task(task["id"])["state"] == "awaiting_approval"
    assert jobs.cancel_task(task["id"], actor_id="alice")["state"] == "cancelled"
    other = submit(jobs, request_id="other")
    assert jobs.cancel_task(other["id"], actor_id="owner", authorized=True)["state"] == "cancelled"


def test_a_task_is_revised_only_from_the_revision_the_editor_saw(jobs):
    task = submit(jobs)
    jobs.revise_task(task["id"], actor_id="alice", expected_revision=1, title="Second goal")
    with pytest.raises(ExecutionConflict, match="reload"):
        jobs.revise_task(task["id"], actor_id="alice", expected_revision=1, title="Third goal")
    current = jobs.get_task(task["id"])
    assert (current["title"], current["revision"]) == ("Second goal", 2)
