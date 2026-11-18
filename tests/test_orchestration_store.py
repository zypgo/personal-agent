"""State-machine, authority and crash recovery boundaries for remote orchestration."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from personal_agent.clock import ManualClock
from personal_agent.db import Database, open_database
from personal_agent.orchestration.models import (
    OrchestrationAccessDenied,
    OrchestrationConflict,
    OrchestrationError,
    OrchestrationNotFound,
    canonical,
)
from personal_agent.orchestration.store import OrchestrationStore


@pytest.fixture
def versions() -> dict[str, int]:
    return {"alice": 1, "bob": 1, "owner": 1, "viewer": 1}


@pytest.fixture
def store(db: Database, clock: ManualClock, versions: dict[str, int]) -> OrchestrationStore:
    def read(actor_id: str) -> dict[str, int]:
        if actor_id not in versions:
            raise OrchestrationAccessDenied("Membership ended.")
        return {"principal_version": versions[actor_id], "membership_version": versions[actor_id]}

    def write(actor_id: str) -> dict[str, int]:
        if actor_id == "viewer":
            raise OrchestrationAccessDenied("Read-only membership.")
        return read(actor_id)

    return OrchestrationStore(
        db,
        clock,
        write,
        space_id="project",
        space_owner_id="owner",
        read_authorization_snapshot=read,
    )


def agent(store: OrchestrationStore, owner: str = "alice", **kwargs: Any) -> dict[str, Any]:
    return store.create_agent(owner, name="Remote helper", kind="muse", **kwargs)


def step(agent_id: str, step_id: str = "research", **kwargs: Any) -> dict[str, Any]:
    return {
        "id": step_id,
        "agent_id": agent_id,
        "prompt": "Review the sample.",
        "acceptance": "Supply evidence and a conclusion.",
        **kwargs,
    }


def task(store: OrchestrationStore, agent_id: str, **kwargs: Any) -> dict[str, Any]:
    values = {
        "request_id": "request-1",
        "title": "Sample review",
        "steps": [step(agent_id)],
        **kwargs,
    }
    actor = values.pop("actor_id", "alice")
    return store.create_task(actor, **values)


def approve(
    store: OrchestrationStore, plan: dict[str, Any], actor: str = "alice"
) -> dict[str, Any]:
    return store.approve_task(
        plan["id"], actor, revision=plan["revision"], plan_hash=plan["plan_hash"]
    )


def credentials(attempt: dict[str, Any]) -> dict[str, Any]:
    return {"generation": attempt["generation"], "lease_token": attempt["lease_token"]}


def report(store: OrchestrationStore, attempt: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return store.report_result(
        attempt["attempt_id"],
        attempt["agent_id"],
        **credentials(attempt),
        text="Evidence supplied.",
        **kwargs,
    )


def accept(
    store: OrchestrationStore, plan: dict[str, Any], attempt: dict[str, Any]
) -> dict[str, Any]:
    return store.accept_result(
        plan["id"],
        attempt["step_id"],
        "alice",
        attempt_id=attempt["attempt_id"],
        revision=plan["revision"],
        plan_hash=plan["plan_hash"],
    )


def test_credentials_are_hash_only_and_shared_agent_views_are_minimal(
    store: OrchestrationStore,
) -> None:
    created = agent(
        store,
        allowed_actor_ids=["bob"],
        secret_env={"webhook_url": "PA_TEST_WEBHOOK"},
        capabilities=["research"],
    )
    agent_id = created["agent"]["id"]
    token = created["worker_token"]
    persisted = dict(store.db.row("SELECT * FROM orchestration_agents WHERE id=?", (agent_id,)))
    assert persisted["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in canonical(persisted)
    shared = store.get_agent(agent_id, "bob")
    assert "secret_env" not in shared and "allowed_actor_ids" not in shared
    assert "token_hash" not in canonical(store.list_agents("alice"))
    with pytest.raises(OrchestrationNotFound):
        store.get_agent(agent_id, "owner")
    with pytest.raises(OrchestrationAccessDenied):
        store.rotate_agent_token(agent_id, "bob")
    store.hello(agent_id, capabilities=["untrusted_claim"], runtime_version="test-1")
    observed = store.authenticate_worker(token)
    assert observed["capabilities"] == ["research"]
    assert observed["observed_capabilities"] == ["untrusted_claim"]
    assert observed["generation"] == 1
    rotated = store.rotate_agent_token(agent_id, "alice")
    assert rotated["agent"]["generation"] == 2
    with pytest.raises(OrchestrationAccessDenied):
        store.authenticate_worker(token)
    assert store.authenticate_worker(rotated["worker_token"])["id"] == agent_id


def test_tokens_and_requests_cannot_cross_space_databases(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    first = agent(store)
    database = open_database(":memory:")
    try:
        second = OrchestrationStore(database, clock, space_id="different")
        with pytest.raises(OrchestrationAccessDenied):
            second.authenticate_worker(first["worker_token"])
        with pytest.raises(OrchestrationNotFound):
            second.get_agent(first["agent"]["id"], "alice")
        another = agent(second)
        assert task(store, first["agent"]["id"])["id"] != task(second, another["agent"]["id"])["id"]
    finally:
        database.close()


def test_creation_idempotency_freezes_first_deadline_and_rejects_changed_plan(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    agent_id = agent(store, allowed_actor_ids=["bob"])["agent"]["id"]
    original = task(store, agent_id, deadline_at=clock.now() + 1_000)
    clock.advance(5)
    retried = task(store, agent_id, deadline_at=clock.now() + 1_000)
    assert retried["id"] == original["id"]
    assert retried["deadline_at"] == original["deadline_at"]
    assert retried["plan_hash"] == original["plan_hash"]
    with pytest.raises(OrchestrationConflict):
        task(store, agent_id, title="Changed plan")
    with pytest.raises(OrchestrationConflict):
        task(store, agent_id, visibility="space")
    assert task(store, agent_id, actor_id="bob")["id"] != original["id"]
    with pytest.raises(OrchestrationAccessDenied):
        task(store, agent_id, actor_id="owner")


def test_private_tasks_stay_private_even_from_space_and_resource_owners(
    store: OrchestrationStore,
) -> None:
    agent_id = agent(store, "bob", allowed_actor_ids=["alice"])["agent"]["id"]
    plan = task(store, agent_id)
    assert plan["can_manage"] and plan["can_approve"]
    for other in ("bob", "owner", "viewer"):
        assert store.list_tasks(other) == []
        with pytest.raises(OrchestrationNotFound):
            store.get_task(plan["id"], other)
        with pytest.raises(OrchestrationNotFound):
            store.events(plan["id"], other)
    with pytest.raises(OrchestrationNotFound):
        approve(store, plan, "owner")
    public = task(store, agent_id, request_id="shared", visibility="space")
    read = store.get_task(public["id"], "viewer")
    assert not read["can_manage"] and not read["can_approve"]
    assert store.get_task(public["id"], "owner")["can_manage"]
    with pytest.raises(OrchestrationAccessDenied):
        approve(store, public, "bob")
    assert approve(store, public, "owner")["state"] == "queued"


@pytest.mark.parametrize(
    "variant", ["cycle", "missing", "duplicate", "no_required", "wrong_boolean"]
)
def test_invalid_dependency_plans_never_persist(store: OrchestrationStore, variant: str) -> None:
    agent_id = agent(store)["agent"]["id"]
    steps = [step(agent_id, "a"), step(agent_id, "b", depends_on=["a"])]
    if variant == "cycle":
        steps[0]["depends_on"] = ["b"]
    elif variant == "missing":
        steps[0]["depends_on"] = ["unknown"]
    elif variant == "duplicate":
        steps[1]["id"] = "a"
    elif variant == "no_required":
        steps[0]["required"] = steps[1]["required"] = False
    else:
        steps[0]["required"] = "yes"
    with pytest.raises(OrchestrationError):
        task(store, agent_id, steps=steps)
    assert store.list_tasks("alice") == []


def test_approval_binds_plan_and_agent_grants(store: OrchestrationStore) -> None:
    agent_id = agent(store)["agent"]["id"]
    plan = task(store, agent_id)
    assert store.claim(agent_id, "before-approval")["attempt"] is None
    with pytest.raises(OrchestrationConflict):
        store.approve_task(plan["id"], "alice", revision=2, plan_hash=plan["plan_hash"])
    with pytest.raises(OrchestrationConflict):
        store.approve_task(plan["id"], "alice", revision=1, plan_hash="stale")
    store.update_agent(agent_id, "alice", capabilities=["research"])
    assert store.get_task(plan["id"], "alice")["state"] == "revoked"
    with pytest.raises(OrchestrationConflict):
        approve(store, plan)
    assert store.claim(agent_id, "after-change")["attempt"] is None


def test_atomic_claim_across_connections_and_restart(tmp_path: Path, clock: ManualClock) -> None:
    path = tmp_path / "orchestration.sqlite"
    db1, db2 = open_database(path), open_database(path)
    try:
        first, second = OrchestrationStore(db1, clock), OrchestrationStore(db2, clock)
        agent_id = agent(first)["agent"]["id"]
        approve(first, task(first, agent_id))
        barrier = Barrier(2)

        def claim_one(item: tuple[OrchestrationStore, str]) -> dict[str, Any]:
            service, nonce = item
            barrier.wait(timeout=5)
            return service.claim(agent_id, nonce)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(claim_one, [(first, "one"), (second, "two")]))
        winners = [response["attempt"] for response in responses if response["attempt"]]
        assert len(winners) == 1
        receipt = winners[0]
        assert first.db.scalar("SELECT count(*) FROM orchestration_attempts") == 1
        recovered = OrchestrationStore(db2, clock).claim(agent_id, receipt["worker_id"])["attempt"]
        assert recovered["attempt_id"] == receipt["attempt_id"]
        assert recovered["lease_token"] == receipt["lease_token"]
        assert recovered["generation"] == 1
        assert second.claim(agent_id, "third")["attempt"] is None
    finally:
        db1.close()
        db2.close()
    reopened = open_database(path)
    try:
        restored = OrchestrationStore(reopened, clock).claim(agent_id, receipt["worker_id"])[
            "attempt"
        ]
        assert restored["attempt_id"] == receipt["attempt_id"]
    finally:
        reopened.close()


def test_only_acceptance_releases_dependencies_and_payload_is_minimal(
    store: OrchestrationStore,
) -> None:
    a, b, c = [agent(store)["agent"]["id"] for _ in range(3)]
    plan = task(
        store,
        a,
        steps=[
            step(a, "a"),
            step(b, "b", depends_on=["a"]),
            step(c, "private_side", prompt="Separate sensitive prompt."),
        ],
    )
    approve(store, plan)
    one = store.claim(a, "first")["attempt"]
    assert store.claim(b, "second")["attempt"] is None
    candidate = report(
        store,
        one,
        artifacts=[
            {
                "id": "artifact-a",
                "name": "report.txt",
                "size": 2,
                "mime": "text/plain",
                "sha256": "a" * 64,
            }
        ],
    )
    assert candidate["state"] == "result_reported"
    assert store.claim(b, "second")["attempt"] is None
    before = store.get_task(plan["id"], "alice")
    assert before["can_accept"]
    assert "lease_token" not in canonical(before)
    assert "secret_env" not in canonical(before)
    accept(store, plan, one)
    two = store.claim(b, "second")["attempt"]
    assert two["payload"]["dependencies"] == [
        {
            "step_id": "a",
            "text": "Evidence supplied.",
            "artifacts": candidate["result"]["artifacts"],
        }
    ]
    assert "Separate sensitive prompt" not in canonical(two)
    assert "actor_id" not in two["payload"] and "title" not in two["payload"]
    assert store.allowed_artifact(two["attempt_id"], b, "artifact-a")
    assert not store.allowed_artifact(two["attempt_id"], c, "artifact-a")
    assert not store.allowed_artifact(two["attempt_id"], b, "unrelated")
    report(store, two)
    accept(store, plan, two)
    three = store.claim(c, "third")["attempt"]
    report(store, three)
    final = accept(store, plan, three)
    assert final["state"] == "succeeded"


def test_fences_cannot_be_omitted_and_cross_agent_status_is_hidden(
    store: OrchestrationStore,
) -> None:
    a, b = [agent(store)["agent"]["id"] for _ in range(2)]
    approve(store, task(store, a))
    attempt = store.claim(a, "one")["attempt"]
    with pytest.raises(OrchestrationNotFound):
        store.attempt_status(attempt["attempt_id"], b)
    for fields in (
        {"generation": 99, "lease_token": attempt["lease_token"]},
        {"generation": 1, "lease_token": "wrong"},
        {"generation": None, "lease_token": None},
    ):
        with pytest.raises(OrchestrationConflict):
            store.heartbeat(attempt["attempt_id"], a, **fields)


def test_expired_claim_is_uncertain_and_cannot_be_reassigned(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    a = agent(store)["agent"]["id"]
    plan = task(store, a)
    approve(store, plan)
    attempt = store.claim(a, "stable", lease_seconds=10)["attempt"]
    clock.advance(11)
    store.reconcile()
    recovered = store.claim(a, "stable")["attempt"]
    assert recovered["attempt_id"] == attempt["attempt_id"]
    assert recovered["state"] == "uncertain" and recovered["desired_cancel"]
    assert store.claim(a, "other")["attempt"] is None
    heartbeat = store.heartbeat(attempt["attempt_id"], a, **credentials(attempt))
    assert heartbeat["lease_expires_at"] == attempt["lease_expires_at"]
    late = report(store, attempt)
    assert late["state"] == "uncertain" and not late["result_eligible"]
    with pytest.raises(OrchestrationConflict):
        accept(store, plan, attempt)
    assert store.get_task(plan["id"], "alice")["state"] == "uncertain"
    stopped = store.confirm_stopped(
        plan["id"],
        "research",
        "alice",
        attempt["attempt_id"],
        "Verified that the external run ended.",
    )
    assert stopped["state"] == "failed"
    assert stopped["steps"][0]["attempt"]["state"] == "stopped"
    approve(store, task(store, a, request_id="explicit-new-plan"))
    assert store.claim(a, "new-nonce")["attempt"]["task_id"] != plan["id"]
    assert store.claim(a, "stable")["attempt"]["state"] == "stopped"


def test_resource_owner_stop_confirmation_does_not_disclose_private_task(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    a = agent(store, "bob", allowed_actor_ids=["alice"])["agent"]["id"]
    plan = task(store, a)
    approve(store, plan)
    attempt = store.claim(a, "one", lease_seconds=1)["attempt"]
    clock.advance(2)
    with pytest.raises(OrchestrationAccessDenied):
        store.confirm_stopped(
            plan["id"], "research", "alice", attempt["attempt_id"], "I assume it ended."
        )
    receipt = store.confirm_stopped(
        plan["id"], "research", "bob", attempt["attempt_id"], "I verified the process stopped."
    )
    assert receipt["redacted"] and "steps" not in receipt and "title" not in receipt
    assert store.get_task(plan["id"], "alice")["steps"][0]["attempt"]["stopped_by"] == "bob"


def test_cancel_requires_acknowledgment_and_never_releases_dependents(
    store: OrchestrationStore,
) -> None:
    a, b = [agent(store)["agent"]["id"] for _ in range(2)]
    plan = task(store, a, steps=[step(a), step(b, "after", depends_on=["research"])])
    approve(store, plan)
    attempt = store.claim(a, "one")["attempt"]
    assert store.cancel_task(plan["id"], "alice")["state"] == "cancelling"
    assert store.heartbeat(attempt["attempt_id"], a, **credentials(attempt))["desired_cancel"]
    report(store, attempt, outcome="cancelled")
    assert store.get_task(plan["id"], "alice")["state"] == "cancelled"
    assert store.claim(b, "never")["attempt"] is None


def test_membership_rejoin_and_credential_rotation_fence_old_work(
    store: OrchestrationStore, versions: dict[str, int]
) -> None:
    created = agent(store, "bob", allowed_actor_ids=["alice"])
    a = created["agent"]["id"]
    plan = task(store, a)
    approve(store, plan)
    attempt = store.claim(a, "one")["attempt"]
    versions["alice"] += 1
    assert store.reconcile()["tasks_revoked"] == 1
    assert store.attempt_status(attempt["attempt_id"], a)["desired_cancel"]
    with pytest.raises(OrchestrationConflict):
        report(store, attempt)
    with pytest.raises(OrchestrationAccessDenied):
        store.get_task(plan["id"], "alice")
    store.rotate_agent_token(a, "bob")
    with pytest.raises(OrchestrationConflict):
        store.heartbeat(attempt["attempt_id"], a, **credentials(attempt))
    with pytest.raises(OrchestrationAccessDenied):
        store.authenticate_worker(created["worker_token"])


def test_event_cursor_and_worker_sequence_retries_do_not_duplicate(
    store: OrchestrationStore,
) -> None:
    a = agent(store)["agent"]["id"]
    plan = task(store, a)
    approve(store, plan)
    attempt = store.claim(a, "one")["attempt"]
    kwargs = {
        **credentials(attempt),
        "sequence": 1,
        "event_type": "progress",
        "data": {"text": "Checking evidence."},
    }
    store.report_event(attempt["attempt_id"], a, **kwargs)
    store.report_event(attempt["attempt_id"], a, **kwargs)
    with pytest.raises(OrchestrationConflict):
        store.report_event(attempt["attempt_id"], a, **{**kwargs, "data": {"text": "Different."}})
    with pytest.raises(OrchestrationConflict):
        store.report_event(attempt["attempt_id"], a, **{**kwargs, "sequence": 3})
    first = store.events(plan["id"], "alice", limit=2)
    second = store.events(plan["id"], "alice", after=first["cursor"])
    combined = first["events"] + second["events"]
    assert len([event for event in combined if event["event_type"] == "worker.progress"]) == 1
    assert store.events(plan["id"], "alice", after=second["cursor"])["events"] == []


def test_questions_keep_lease_and_require_current_private_authority(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    a = agent(store)["agent"]["id"]
    plan = task(store, a)
    approve(store, plan)
    attempt = store.claim(a, "one", lease_seconds=10)["attempt"]
    question = {
        **credentials(attempt),
        "question_id": "q1",
        "text": "Which format?",
        "choices": ["PDF", "Text"],
    }
    asked = store.ask_question(attempt["attempt_id"], a, **question)
    assert asked["state"] == "waiting_input"
    assert len(store.ask_question(attempt["attempt_id"], a, **question)["questions"]) == 1
    with pytest.raises(OrchestrationNotFound):
        store.answer_question(plan["id"], "q1", "owner", answer="PDF")
    store.answer_question(plan["id"], "q1", "alice", answer="PDF")
    status = store.attempt_status(attempt["attempt_id"], a)
    assert status["questions"][0]["answer"] == "PDF"
    clock.advance(8)
    heartbeat = store.heartbeat(attempt["attempt_id"], a, **credentials(attempt), lease_seconds=10)
    assert heartbeat["state"] == "running"
    assert heartbeat["lease_expires_at"] == clock.now() + 10
    store.ask_question(attempt["attempt_id"], a, **{**question, "question_id": "q2"})
    clock.advance(11)
    with pytest.raises(OrchestrationConflict):
        store.answer_question(plan["id"], "q2", "alice", answer="Text")
    assert store.get_task(plan["id"], "alice")["questions"][1]["state"] == "closed"


def test_wake_reservation_is_durable_and_lost_reply_never_resends(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    a = agent(store, wake_mode="verified_hook")["agent"]["id"]
    plan = task(store, a)
    approve(store, plan)
    assert store.wake_candidates()[0]["task_id"] == plan["id"]
    reserved = store.reserve_wake(a, plan["id"], timeout_seconds=5)
    assert reserved is not None and reserved["state"] == "reserved"
    assert store.reserve_wake(a, plan["id"]) is None
    clock.advance(6)
    assert store.reconcile()["wakes_uncertain"] == 1
    assert store.wake_candidates() == []
    assert store.reserve_wake(a, plan["id"]) is None
    assert store.get_task(plan["id"], "alice")["wakes"][0]["state"] == "uncertain"
    settled = store.finish_wake(reserved["id"], outcome="sent", http_status=202)
    assert settled["state"] == "sent"
    assert store.get_task(plan["id"], "alice")["state"] == "queued"
    assert store.db.scalar("SELECT count(*) FROM orchestration_wakes") == 1
    with pytest.raises(OrchestrationConflict):
        store.finish_wake(reserved["id"], outcome="failed")


def test_cancelled_unclaimed_wake_needs_owner_reconciliation_before_next_wake(
    store: OrchestrationStore,
) -> None:
    a = agent(store)["agent"]["id"]
    first = approve(store, task(store, a))
    wake = store.reserve_wake(a, first["id"])
    assert wake is not None
    store.finish_wake(wake["id"], outcome="sent", http_status=200)
    store.cancel_task(first["id"], "alice")
    second = approve(store, task(store, a, request_id="second"))
    assert not store.wake_candidates()
    assert store.reserve_wake(a, second["id"]) is None
    with pytest.raises(OrchestrationAccessDenied):
        store.confirm_wake_stopped(first["id"], wake["id"], "bob", note="Not mine")
    stopped = store.confirm_wake_stopped(
        first["id"], wake["id"], "alice", note="Native run ended without claiming."
    )
    assert stopped["wakes"][0]["state"] == "stopped"
    assert stopped["wakes"][0]["http_status"] == 200
    assert (
        store.confirm_wake_stopped(
            first["id"], wake["id"], "alice", note="Native run ended without claiming."
        )["id"]
        == first["id"]
    )
    assert store.reserve_wake(a, first["id"]) is None
    assert store.wake_candidates()[0]["task_id"] == second["id"]
    assert store.reserve_wake(a, second["id"]) is not None


def test_claimed_wake_cannot_replace_attempt_stop_confirmation(store: OrchestrationStore) -> None:
    a = agent(store)["agent"]["id"]
    plan = approve(store, task(store, a))
    wake = store.reserve_wake(a, plan["id"])
    assert wake is not None
    store.finish_wake(wake["id"], outcome="sent", http_status=200)
    store.claim(a, "native-turn")
    with pytest.raises(OrchestrationConflict):
        store.confirm_wake_stopped(plan["id"], wake["id"], "alice", note="Need actual process stop")


def test_resource_owner_occupancy_does_not_expose_private_task_contents(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    a = agent(store, owner="alice", allowed_actor_ids=["bob"])["agent"]["id"]
    plan = task(store, a, actor_id="bob", title="Private business details")
    approve(store, plan, actor="bob")
    claimed = store.claim(a, "native-turn")["attempt"]
    clock.advance(301)
    occupied = store.owner_occupancy(a, "alice")
    assert occupied["attempts"][0]["attempt_id"] == claimed["attempt_id"]
    assert occupied["attempts"][0]["can_confirm_stopped"]
    assert "Private business details" not in canonical(occupied)
    assert "prompt" not in canonical(occupied) and "lease_token" not in canonical(occupied)
    with pytest.raises(OrchestrationAccessDenied):
        store.owner_occupancy(a, "bob")


def test_wake_filter_precedes_limit_so_manual_agents_cannot_starve_grok(
    store: OrchestrationStore,
) -> None:
    for index in range(12):
        a = agent(store)["agent"]["id"]
        approve(store, task(store, a, request_id=f"manual-{index}"))
    grok = store.create_agent("alice", name="Grok", kind="grok", wake_mode="grok_routine")["agent"][
        "id"
    ]
    plan = approve(store, task(store, grok, request_id="wakeable"))
    candidates = store.wake_candidates(limit=1, kind="grok", wake_mode="grok_routine")
    assert [item["task_id"] for item in candidates] == [plan["id"]]


def test_deadline_closes_unclaimed_and_preserves_active_uncertainty(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    a, b = [agent(store)["agent"]["id"] for _ in range(2)]
    unclaimed = task(store, a, deadline_at=clock.now() + 10)
    live = task(store, b, request_id="second", deadline_at=clock.now() + 10)
    approve(store, live)
    attempt = store.claim(b, "one")["attempt"]
    assert attempt["lease_expires_at"] == live["deadline_at"]
    clock.advance(11)
    store.reconcile()
    assert store.get_task(unclaimed["id"], "alice")["state"] == "expired"
    assert store.get_task(live["id"], "alice")["state"] == "uncertain"
    assert store.claim(b, "no-retry")["attempt"] is None


def test_report_bounds_prevent_inline_files_and_terminal_claims_are_not_reused(
    store: OrchestrationStore,
) -> None:
    with pytest.raises(OrchestrationError):
        agent(store, secret_env={"webhook_url": "https://example.test/secret"})
    a = agent(store)["agent"]["id"]
    plan = task(store, a)
    approve(store, plan)
    attempt = store.claim(a, "one")["attempt"]
    with pytest.raises(OrchestrationError):
        report(store, attempt, artifacts=[{"name": "report.txt", "content_base64": "YQ=="}])
    with pytest.raises(OrchestrationError):
        store.report_result(attempt["attempt_id"], a, **credentials(attempt), text="x" * 64_001)
    with pytest.raises(OrchestrationError):
        store.report_event(
            attempt["attempt_id"],
            a,
            **credentials(attempt),
            sequence=1,
            event_type="progress",
            data={"worker_token": "synthetic"},
        )
    first = report(store, attempt)
    assert report(store, attempt)["result"] == first["result"]
    with pytest.raises(OrchestrationConflict):
        store.report_result(
            attempt["attempt_id"], a, **credentials(attempt), text="Different result."
        )
    accept(store, plan, attempt)
    assert store.claim(a, "one")["attempt"]["state"] == "accepted"
    assert store.claim(a, "different")["attempt"] is None


def test_rejection_blocks_descendants_and_does_not_requeue(store: OrchestrationStore) -> None:
    a, b = [agent(store)["agent"]["id"] for _ in range(2)]
    plan = task(store, a, steps=[step(a), step(b, "after", depends_on=["research"])])
    approve(store, plan)
    attempt = store.claim(a, "one")["attempt"]
    report(store, attempt)
    rejected = store.reject_result(
        plan["id"],
        "research",
        "alice",
        attempt_id=attempt["attempt_id"],
        revision=plan["revision"],
        plan_hash=plan["plan_hash"],
        reason="Evidence missing.",
    )
    assert rejected["state"] == "failed"
    assert rejected["steps"][1]["state"] == "skipped"
    assert store.claim(b, "after")["attempt"] is None


def test_one_wake_covers_queued_work_until_an_attempt_is_claimed(store: OrchestrationStore) -> None:
    a = agent(store, wake_mode="grok_routine")["agent"]["id"]
    plan = task(store, a, steps=[step(a, "one"), step(a, "two")])
    approve(store, plan)
    wake = store.reserve_wake(a, plan["id"])
    assert wake is not None
    store.finish_wake(wake["id"], outcome="sent", http_status=200)
    assert store.wake_candidates() == []
    assert store.reserve_wake(a, plan["id"]) is None
    attempt = store.claim(a, "one")["attempt"]
    report(store, attempt)
    accept(store, plan, attempt)
    assert store.get_task(plan["id"], "alice")["wakes"][0]["attempt_id"] == attempt["attempt_id"]
    next_wake = store.reserve_wake(a, plan["id"])
    assert next_wake is not None and next_wake["id"] != wake["id"]
    assert next_wake["step_id"] == "two"


def test_deadline_cancellation_receipt_retains_expired_cause(
    store: OrchestrationStore, clock: ManualClock
) -> None:
    a = agent(store)["agent"]["id"]
    plan = task(store, a, deadline_at=clock.now() + 5)
    approve(store, plan)
    attempt = store.claim(a, "one")["attempt"]
    clock.advance(6)
    report(store, attempt, outcome="cancelled")
    assert store.get_task(plan["id"], "alice")["state"] == "expired"


def test_private_tasks_cannot_enter_another_owners_shared_computer(
    store: OrchestrationStore,
) -> None:
    created = store.create_agent(
        "bob", name="Shared Bot", kind="grok", allowed_actor_ids=["alice"], shared_environment=False
    )
    a = created["agent"]["id"]
    assert created["agent"]["shared_environment"]
    assert store.update_agent(a, "bob", shared_environment=False)["shared_environment"]
    with pytest.raises(OrchestrationAccessDenied):
        task(store, a)
    assert task(store, a, visibility="space")["visibility"] == "space"
