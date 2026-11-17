"""Real database transitions for interrupted node execution and collaboration scope."""

from __future__ import annotations

import base64
import hashlib

import pytest

from personal_agent.core.artifacts import ArtifactStore
from personal_agent.db import open_database
from personal_agent.execution import ExecutionConflict, ExecutionError
from personal_agent.execution.models import CAPABILITIES, validate_manifest
from personal_agent.execution.schema import migrate
from personal_agent.execution.store import ExecutionStore
from personal_agent.execution.workspaces import remove_staged_result, snapshot, stage_result


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


def start(jobs, task=None, node="node-a"):
    task = task or submit(jobs)
    job = task["jobs"][-1]
    jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    envelope = jobs.claim(node, CAPABILITIES)
    jobs.admit(
        envelope["attempt_id"],
        envelope["generation"],
        plan_hash=envelope["plan_hash"],
        node_id=node,
        resource_id="container-" + envelope["attempt_id"],
    )
    return task, envelope


def finish(jobs, env, **kw):
    return jobs.complete(
        env["attempt_id"],
        env["generation"],
        node_id="node-a",
        status="succeeded",
        exit_code=0,
        **kw,
    )


def test_plan_is_persisted_before_approval_and_request_is_bound(jobs):
    task = submit(jobs)
    assert task["jobs"][0]["state"] == "awaiting_approval"
    assert jobs.claim("node-a", CAPABILITIES) is None
    assert submit(jobs)["id"] == task["id"]
    with pytest.raises(ExecutionConflict):
        jobs.create_task(title="Different", command="printf bye", actor_id="alice", request_id="r1")
    job = task["jobs"][0]
    with pytest.raises(ExecutionError, match="approver"):
        jobs.approve_job(job["id"], actor_id="alice", plan_hash=job["plan_hash"])
    with pytest.raises(ExecutionConflict):
        jobs.approve_job(job["id"], actor_id="owner", plan_hash="changed")


def test_lost_claim_and_admission_ack_reuses_one_attempt(jobs):
    _task, env = start(jobs)
    assert jobs.claim("node-a", CAPABILITIES)["attempt_id"] == env["attempt_id"]
    assert jobs.claim("node-b", CAPABILITIES) is None
    jobs.admit(
        env["attempt_id"],
        1,
        plan_hash=env["plan_hash"],
        node_id="node-a",
        resource_id="container-" + env["attempt_id"],
    )
    assert jobs.db.scalar("SELECT COUNT(*) FROM execution_attempts") == 1
    with pytest.raises(ExecutionConflict):
        jobs.admit(
            env["attempt_id"],
            1,
            plan_hash=env["plan_hash"],
            node_id="node-a",
            resource_id="new-container",
        )


def test_restart_does_not_recreate_remote_attempt(tmp_path, clock):
    path = tmp_path / "jobs.db"
    db = open_database(path)
    try:
        if db.one("SELECT 1 FROM sqlite_master WHERE name='execution_tasks'") is None:
            with db.tx():
                migrate(db._conn)
        jobs = ExecutionStore(db, clock)
        _, env = start(jobs)
    finally:
        db.close()
    db = open_database(path)
    try:
        resumed = ExecutionStore(db, clock)
        assert resumed.claim("node-a", CAPABILITIES)["attempt_id"] == env["attempt_id"]
        assert resumed.db.scalar("SELECT COUNT(*) FROM execution_attempts") == 1
    finally:
        db.close()


def test_uncertain_and_cancel_need_remote_confirmation(jobs, clock):
    task, env = start(jobs)
    clock.advance(100)
    assert jobs.reconcile()["uncertain"] == 1
    assert jobs.get_job(env["job_id"])["state"] == "uncertain"
    assert jobs.claim("node-b", CAPABILITIES) is None
    assert jobs.cancel_task(task["id"], actor_id="alice")["state"] == "cancel_requested"
    assert jobs.get_attempt(env["attempt_id"])["desired_state"] == "cancel"
    jobs.complete(env["attempt_id"], 1, node_id="node-a", status="cancelled")
    assert jobs.get_task(task["id"])["state"] == "cancelled"


def test_complete_receipt_event_and_outbox_are_atomic_and_deduplicated(jobs):
    _task, env = start(jobs)
    finish(jobs, env, stdout="hello")
    finish(jobs, env, stdout="hello")
    assert jobs.db.scalar("SELECT COUNT(*) FROM events WHERE source='execution'") == 1
    assert jobs.db.scalar("SELECT COUNT(*) FROM outbox") == 1
    assert jobs.db.row("SELECT * FROM events")["status"] == "done"
    with pytest.raises(ExecutionConflict):
        finish(jobs, env, stdout="changed")


def test_old_generation_or_other_node_cannot_finish_or_cleanup(jobs):
    _, env = start(jobs)
    with pytest.raises(ExecutionConflict):
        jobs.complete(env["attempt_id"], 2, node_id="node-a", status="succeeded", exit_code=0)
    with pytest.raises(ExecutionConflict):
        jobs.complete(env["attempt_id"], 1, node_id="node-b", status="succeeded", exit_code=0)
    with pytest.raises(ExecutionConflict):
        jobs.cleanup(env["attempt_id"], 1, node_id="node-a", released=True)
    finish(jobs, env)
    assert (
        jobs.cleanup(env["attempt_id"], 1, node_id="node-a", released=False)["cleanup_state"]
        == "cleanup_pending"
    )
    assert (
        jobs.cleanup(env["attempt_id"], 1, node_id="node-a", released=True)["cleanup_state"]
        == "released"
    )


def test_late_result_after_revision_is_history_not_new_task_progress(jobs):
    task, env = start(jobs)
    revised = jobs.revise_task(task["id"], actor_id="alice", expected_revision=1, title="New goal")
    assert revised["revision"] == 2
    finish(jobs, env)
    assert jobs.get_task(task["id"])["state"] == "waiting_input"
    assert jobs.get_job(env["job_id"])["result"]["stale_revision"]
    assert jobs.db.scalar("SELECT COUNT(*) FROM outbox") == 0


def test_revoke_does_not_replay_or_deliver_result(jobs):
    _task, env = start(jobs)
    assert jobs.revoke_actor("alice") == 1
    finish(jobs, env, deliver=False)
    assert jobs.db.scalar("SELECT COUNT(*) FROM outbox") == 0
    assert jobs.get_job(env["job_id"])["desired_state"] == "cancel"


def test_fixed_delivery_target_and_capabilities(jobs):
    with pytest.raises(ExecutionError, match="delivery"):
        submit(jobs, channel="telegram")
    task = submit(jobs, channel="telegram", recipient="fixed-chat")
    job = task["jobs"][0]
    jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    assert jobs.claim("node-a", {"shell"}) is None
    assert jobs.claim("node-a", CAPABILITIES, allowed_actor_ids={"bob"}) is None
    env = jobs.claim("node-a", CAPABILITIES, allowed_actor_ids={"alice"})
    jobs.admit(
        env["attempt_id"], 1, plan_hash=env["plan_hash"], node_id="node-a", resource_id="one"
    )
    finish(jobs, env)
    assert jobs.db.row("SELECT recipient FROM outbox")["recipient"] == "fixed-chat"


def test_workflow_step_id_is_stable_and_dependency_blocks_claim(jobs):
    task = submit(jobs)
    second = jobs.add_step(
        task["id"], actor_id="alice", expected_revision=1, command="printf second", step_id="2"
    )
    same = jobs.add_step(
        task["id"], actor_id="alice", expected_revision=1, command="printf second", step_id="2"
    )
    assert same["id"] == second["id"]
    jobs.approve_job(second["id"], actor_id="owner", plan_hash=second["plan_hash"])
    assert jobs.claim("node-a", CAPABILITIES) is None
    _, first = start(jobs, task)
    finish(jobs, first)
    next_env = jobs.claim("node-a", CAPABILITIES)
    assert next_env["job_id"] == second["id"]


def test_logs_are_cumulative_redacted_snapshots(jobs, monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "very-private-credential")
    _, env = start(jobs)
    jobs.observe(
        env["attempt_id"],
        1,
        node_id="node-a",
        state="running",
        sequence=1,
        stdout="begin very-private",
    )
    first = jobs.logs(env["attempt_id"])[0]["text"]
    assert "very-private" not in first
    jobs.observe(
        env["attempt_id"],
        1,
        node_id="node-a",
        state="running",
        sequence=2,
        stdout="begin very-private-credential done",
    )
    assert "very-private-credential" not in jobs.logs(env["attempt_id"])[1]["text"]
    with pytest.raises(ExecutionConflict):
        jobs.observe(
            env["attempt_id"], 1, node_id="node-a", state="running", sequence=2, stdout="different"
        )


def test_manifest_rejects_wrong_hash_escape_and_credential_paths():
    item = {
        "path": "result.txt",
        "size": 2,
        "sha256": hashlib.sha256(b"ok").hexdigest(),
        "content_base64": base64.b64encode(b"ok").decode(),
    }
    assert validate_manifest([item])[0]["path"] == "result.txt"
    for path in ["../result", "/tmp/result", ".env", ".git/config", "a\\b"]:
        with pytest.raises(ExecutionError):
            validate_manifest([{**item, "path": path}])
    with pytest.raises(ExecutionError):
        validate_manifest([{**item, "sha256": "wrong"}])


def test_snapshot_and_result_never_follow_links(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "input.txt").write_text("input")
    files = snapshot(root, ["input.txt"])
    assert files[0]["size"] == 5
    (root / "link").symlink_to(root / "input.txt")
    with pytest.raises(ExecutionError):
        snapshot(root, ["link"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".pa-jobs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExecutionError):
        stage_result(root, "a" * 32, files[0])
    assert list(outside.iterdir()) == []


def test_quota_does_not_disable_inspection_or_cancel(jobs):
    jobs.max_pending = 1
    task = submit(jobs)
    with pytest.raises(ExecutionError, match="quota"):
        submit(jobs, request_id="r2")
    assert jobs.get_task(task["id"])
    assert jobs.cancel_task(task["id"], actor_id="alice")["state"] == "cancelled"


def test_unapproved_artifact_or_unadmitted_success_is_rejected(jobs):
    task = submit(jobs)
    job = task["jobs"][0]
    jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    env = jobs.claim("node-a", CAPABILITIES)
    with pytest.raises(ExecutionConflict):
        finish(jobs, env)
    jobs.admit(
        env["attempt_id"], 1, plan_hash=env["plan_hash"], node_id="node-a", resource_id="fixed"
    )
    item = {
        "path": "not-approved.txt",
        "size": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "content_base64": "",
    }
    with pytest.raises(ExecutionError):
        finish(jobs, env, artifacts=[item])


def test_membership_rejoin_cannot_revive_approved_or_running_work(jobs):
    authorization = {"membership": 1, "principal": 1}
    jobs.authorization_snapshot = lambda _actor: dict(authorization)
    task, env = start(jobs)
    second = submit(jobs, request_id="second")
    job = second["jobs"][0]
    jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    authorization["membership"] = 3  # revoked, then rejoined under a new membership version
    assert not jobs.authorized(task["id"])
    assert jobs.claim("node-a", CAPABILITIES) is None
    assert jobs.get_attempt(env["attempt_id"])["desired_state"] == "cancel"
    assert jobs.claim("node-b", CAPABILITIES) is None
    with pytest.raises(ExecutionConflict, match="authorization"):
        jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    finish(jobs, env)
    assert jobs.db.scalar("SELECT COUNT(*) FROM outbox") == 0
    assert jobs.db.row("SELECT status FROM events WHERE source='execution'")["status"] == "done"
    jobs.reconcile()
    assert jobs.get_task(second["id"])["state"] == "cancelled"


def test_resource_owner_filter_applies_to_new_and_existing_attempts(jobs):
    task = submit(jobs, approver_id="bob")
    job = task["jobs"][0]
    jobs.approve_job(job["id"], actor_id="bob", plan_hash=job["plan_hash"])
    assert jobs.claim("node-a", CAPABILITIES, allowed_approver_ids={"alice", "owner"}) is None
    env = jobs.claim("node-b", CAPABILITIES, allowed_approver_ids={"bob", "owner"})
    assert env["job_id"] == job["id"]
    assert jobs.claim("node-b", CAPABILITIES, allowed_actor_ids={"charlie"}) is None
    assert jobs.get_attempt(env["attempt_id"])["desired_state"] == "cancel"


def file_item(path="input.txt", content=b"hello"):
    return {
        "path": path,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }


def test_input_storage_quota_is_released_only_after_confirmed_cleanup(jobs):
    jobs.max_input_bytes = 5
    task = submit(jobs, input_manifest=[file_item()])
    original = task["jobs"][0]["plan_hash"]
    assert "content_base64" not in task["jobs"][0]["plan"]["input_manifest"][0]
    with pytest.raises(ExecutionError, match="storage quota"):
        submit(jobs, request_id="second", input_manifest=[file_item()])
    assert jobs.db.scalar("SELECT COUNT(*) FROM execution_tasks") == 1
    _, env = start(jobs, task)
    assert env["input_manifest"][0]["content_base64"] == file_item()["content_base64"]
    finish(jobs, env)
    with pytest.raises(ExecutionError, match="storage quota"):
        submit(jobs, request_id="second", input_manifest=[file_item()])
    jobs.cleanup(env["attempt_id"], 1, node_id="node-a", released=True)
    submit(jobs, request_id="second", input_manifest=[file_item()])
    assert jobs.get_job(env["job_id"])["plan_hash"] == original


def test_failed_step_cancels_dependents_and_expires_without_storage_leak(jobs, clock):
    task = submit(jobs)
    second = jobs.add_step(
        task["id"],
        actor_id="alice",
        expected_revision=1,
        step_id="2",
        command="printf never",
        input_manifest=[file_item()],
    )
    jobs.approve_job(second["id"], actor_id="owner", plan_hash=second["plan_hash"])
    _, env = start(jobs, task)
    jobs.complete(env["attempt_id"], 1, node_id="node-a", status="failed", exit_code=1)
    assert jobs.get_task(task["id"])["state"] == "failed"
    assert jobs.get_job(second["id"])["state"] == "cancelled"
    assert jobs.claim("node-a", CAPABILITIES) is None
    assert jobs.db.scalar("SELECT COUNT(*) FROM execution_inputs") == 0
    waiting = submit(jobs, request_id="expire", input_manifest=[file_item()])
    clock.advance(7 * 86400 + 1)
    assert jobs.reconcile()["expired"] == 1
    assert jobs.get_task(waiting["id"])["state"] == "expired"
    assert jobs.db.scalar("SELECT COUNT(*) FROM execution_inputs") == 0


def test_log_ring_keeps_recent_cumulative_output_and_cursor_resume(jobs):
    _, env = start(jobs)
    for sequence in range(20):
        jobs.observe(
            env["attempt_id"],
            1,
            node_id="node-a",
            state="running",
            sequence=sequence,
            stdout=f"snapshot {sequence}",
        )
    logs = jobs.logs(env["attempt_id"])
    assert len(logs) == 8
    assert logs[-1]["sequence"] == 19
    assert jobs.logs(env["attempt_id"], after=18)[0]["text"] == "snapshot 19"
    # A stale reconnect cannot insert an old snapshot or regress the durable sequence.
    jobs.observe(env["attempt_id"], 1, node_id="node-a", state="running", sequence=1, stdout="old")
    assert jobs.get_attempt(env["attempt_id"])["log_sequence"] == 19


def test_artifact_collection_is_atomic_with_receipt_and_dedupes(jobs, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jobs.workspace = workspace
    jobs.artifacts = ArtifactStore(jobs.db, tmp_path / "artifacts", workspace, clock=jobs.clock)
    task = submit(jobs, output_paths=["result.txt"])
    _, env = start(jobs, task)
    original_enqueue = jobs.outbox.enqueue

    def interrupted(**_kwargs):
        raise RuntimeError("simulated interrupted completion transaction")

    monkeypatch.setattr(jobs.outbox, "enqueue", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        finish(jobs, env, artifacts=[file_item("result.txt", b"result")])
    assert jobs.db.scalar("SELECT COUNT(*) FROM artifacts") == 0
    assert jobs.db.scalar("SELECT COUNT(*) FROM events WHERE source='execution'") == 0
    assert jobs.get_attempt(env["attempt_id"])["state"] == "admitted"
    monkeypatch.setattr(jobs.outbox, "enqueue", original_enqueue)
    finish(jobs, env, artifacts=[file_item("result.txt", b"result")])
    finish(jobs, env, artifacts=[file_item("result.txt", b"result")])
    assert jobs.db.scalar("SELECT COUNT(*) FROM artifacts") == 1
    assert (
        jobs.db.scalar("SELECT COUNT(*) FROM outbox") == 2
    )  # immutable file and completion notice
    assert (
        jobs.get_job(env["job_id"])["result"]["artifacts"][0]["sha256"]
        == hashlib.sha256(b"result").hexdigest()
    )
    staged = workspace / ".pa-jobs" / env["job_id"] / "result.txt"
    assert staged.is_file()
    jobs.cleanup(env["attempt_id"], 1, node_id="node-a", released=True)
    assert not staged.exists()
    assert jobs.db.scalar("SELECT COUNT(*) FROM artifacts") == 1


def test_staging_cleanup_never_follows_a_replaced_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.txt").write_text("keep")
    (workspace / ".pa-jobs").mkdir()
    (workspace / ".pa-jobs" / ("a" * 32)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExecutionError):
        remove_staged_result(workspace, "a" * 32, "result.txt")
    assert (outside / "result.txt").read_text() == "keep"


def ask(jobs, task, **kwargs):
    return jobs.request_input(
        task["id"],
        actor_id="alice",
        expected_revision=task["revision"],
        question="Which report format?",
        choices=["text", "csv"],
        request_id="format",
        **kwargs,
    )


def test_structured_input_pauses_dispatch_and_answer_never_changes_approved_plan(jobs):
    task = submit(jobs)
    job = task["jobs"][0]
    jobs.approve_job(job["id"], actor_id="owner", plan_hash=job["plan_hash"])
    question = ask(jobs, task)
    assert ask(jobs, task)["id"] == question["id"]
    assert jobs.get_task(task["id"])["state"] == "waiting_input"
    assert jobs.claim("node-a", CAPABILITIES) is None
    with pytest.raises(ExecutionConflict):
        jobs.request_input(
            task["id"],
            actor_id="alice",
            expected_revision=1,
            question="Changed?",
            choices=[],
            request_id="format",
        )
    with pytest.raises(ExecutionError, match="choices"):
        jobs.answer_input(
            task["id"], question["id"], actor_id="alice", expected_revision=1, answer="pdf"
        )
    answered = jobs.answer_input(
        task["id"], question["id"], actor_id="alice", expected_revision=1, answer="csv"
    )
    assert answered["state"] == "answered" and answered["answered_by"] == "alice"
    assert (
        jobs.answer_input(
            task["id"], question["id"], actor_id="alice", expected_revision=1, answer="csv"
        )
        == answered
    )
    with pytest.raises(ExecutionConflict, match="different answer"):
        jobs.answer_input(
            task["id"], question["id"], actor_id="alice", expected_revision=1, answer="text"
        )
    env = jobs.claim("node-a", CAPABILITIES)
    assert env["plan_hash"] == job["plan_hash"] and env["command"] == "printf hello"
    assert jobs.db.scalar("SELECT COUNT(*) FROM events") == 0  # answering cannot start a model loop


def test_input_cannot_pause_active_execution_or_resume_cancelled_or_revised_task(jobs):
    task, env = start(jobs)
    with pytest.raises(ExecutionConflict, match="active execution"):
        ask(jobs, task)
    finish(jobs, env)
    question = ask(jobs, task)
    jobs.revise_task(task["id"], actor_id="alice", expected_revision=1, title="Next revision")
    with pytest.raises(ExecutionConflict, match="revision"):
        jobs.answer_input(
            task["id"], question["id"], actor_id="alice", expected_revision=1, answer="csv"
        )
    second = submit(jobs, request_id="second")
    pending = ask(jobs, second)
    jobs.cancel_task(second["id"], actor_id="alice")
    with pytest.raises(ExecutionConflict, match="no longer"):
        jobs.answer_input(
            second["id"], pending["id"], actor_id="alice", expected_revision=1, answer="csv"
        )
    assert jobs.get_task(second["id"])["questions"][0]["state"] == "cancelled"


def test_input_answer_fences_task_and_answerer_authorization(jobs):
    versions = {"alice": 1, "bob": 1}

    def auth(actor):
        if actor not in versions:
            raise PermissionError("revoked")
        return {"membership": versions[actor]}

    jobs.authorization_snapshot = auth
    task = submit(jobs)
    question = ask(jobs, task)
    del versions["bob"]
    with pytest.raises(PermissionError):
        jobs.answer_input(
            task["id"],
            question["id"],
            actor_id="bob",
            expected_revision=1,
            answer="text",
            authorized=True,
        )
    versions["bob"] = 3
    versions["alice"] = 3
    with pytest.raises(ExecutionConflict, match="authorization"):
        jobs.answer_input(
            task["id"],
            question["id"],
            actor_id="bob",
            expected_revision=1,
            answer="text",
            authorized=True,
        )
    assert jobs.get_task(task["id"])["questions"][0]["state"] == "open"


def test_waiting_input_expiry_closes_question_and_releases_snapshots(jobs, clock):
    task = submit(jobs, input_manifest=[file_item()])
    ask(jobs, task)
    clock.advance(7 * 86400 + 1)
    jobs.reconcile()
    fresh = jobs.get_task(task["id"])
    assert fresh["state"] == "expired"
    assert fresh["questions"][0]["state"] == "cancelled"
    assert jobs.db.scalar("SELECT COUNT(*) FROM execution_inputs") == 0
