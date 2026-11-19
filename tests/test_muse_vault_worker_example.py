"""Muse's adapter reuses the real bridge with offline Vault/worker boundaries."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from personal_agent.agent_adapters import bridge
from personal_agent.orchestration.models import OrchestrationAccessDenied, OrchestrationConflict
from personal_agent.orchestration.store import OrchestrationStore

EXAMPLE = Path(__file__).parents[1] / "examples" / "muse" / "muse_vault_worker.py"
SPEC = importlib.util.spec_from_file_location("muse_worker_example_test", EXAMPLE)
muse = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = muse
SPEC.loader.exec_module(muse)
muse.core = bridge


class WorkerFixture:
    origin = "https://control.example.test"
    agent_id = "agent_muse"
    space_id = "space_fixture"

    def __init__(self):
        self.ready = False
        self.claims = {}
        self.calls = []
        self.lose_result = False
        self.stopped = False
        self.stale = False

    def request(self, method, action, payload=None):
        self.calls.append((method, action, copy.deepcopy(payload)))
        if action == "claim":
            nonce = payload["worker_id"]
            if nonce not in self.claims:
                if not self.ready:
                    return {"attempt": None}
                number = len(self.claims) + 1
                self.claims[nonce] = {
                    "attempt_id": f"attempt_{number}",
                    "task_id": f"task_{number}",
                    "step_id": "summary",
                    "agent_id": self.agent_id,
                    "worker_id": nonce,
                    "generation": 1,
                    "lease_token": "fixture-private-lease",
                    "state": "claimed",
                    "desired_state": "run",
                    "desired_cancel": False,
                    "last_sequence": 0,
                    "payload": {
                        "space_id": self.space_id,
                        "prompt": "Summarize the approved material.",
                        "dependencies": [],
                    },
                }
            return {"attempt": copy.deepcopy(self.claims[nonce])}
        attempt_id = action.split("/")[1]
        attempt = next(value for value in self.claims.values() if value["attempt_id"] == attempt_id)
        if action.endswith("/stop-receipt"):
            return {
                "attempt_id": attempt_id,
                "generation": 1,
                "state": "stopped" if self.stopped else "uncertain",
                "stopped_by": "owner" if self.stopped else None,
                "stopped_at": 1234 if self.stopped else None,
            }
        if self.stale:
            raise bridge.RequestError("Stale fixture receipt.", status=409)
        if action.endswith("/result"):
            attempt.update(
                state="result_reported", result={"outcome": payload["status"]}, result_eligible=True
            )
            if self.lose_result:
                self.lose_result = False
                raise bridge.RequestError("Synthetic lost response.")
        if action.endswith("/events"):
            attempt.update(last_sequence=payload["sequence"], state="running")
        return copy.deepcopy(attempt)


def configured(tmp_path):
    client = WorkerFixture()
    worker = bridge.WorkerBridge(tmp_path / "private-state", client=client)
    return client, worker, muse.Dispatcher(worker)


def test_empty_queue_reuses_nonce_and_handoff_is_once_across_restart(tmp_path):
    client, worker, dispatcher = configured(tmp_path)
    assert dispatcher.claim() == {"claimed": False, "idle": True}
    nonce = client.calls[-1][2]["worker_id"]
    assert dispatcher.claim()["idle"]
    assert client.calls[-1][2]["worker_id"] == nonce
    client.ready = True
    first = dispatcher.claim()
    assert first["claimed"]
    assert first["payload"]["prompt"] == "Summarize the approved material."
    resumed = muse.Dispatcher(bridge.WorkerBridge(worker.home, client=client))
    repeated = resumed.claim()
    assert repeated["busy"] and "payload" not in repeated
    assert len([call for call in client.calls if call[1] == "claim"]) == 3
    assert "fixture-private-lease" not in json.dumps([first, repeated])
    assert all(path.stat().st_mode & 0o077 == 0 for path in worker.home.rglob("*.json"))


def test_confirmed_result_advances_nonce_without_replaying_old_task(tmp_path):
    client, worker, dispatcher = configured(tmp_path)
    client.ready = True
    first = dispatcher.claim()
    first_nonce = dispatcher.read()["worker_id"]
    worker.report(first["attempt_id"], status="succeeded", text="Completed summary.")
    second = dispatcher.claim()
    assert second["claimed"] and second["attempt_id"] != first["attempt_id"]
    assert dispatcher.read()["worker_id"] != first_nonce
    assert worker._read(first["attempt_id"])["result_receipt"]["state"] == "result_reported"


def test_pending_result_survives_restart_and_retries_original_file_bytes(tmp_path):
    client, worker, dispatcher = configured(tmp_path)
    client.ready = True
    attempt_id = dispatcher.claim()["attempt_id"]
    artifact = tmp_path / "report.md"
    artifact.write_text("Original report.\n")
    client.lose_result = True
    with pytest.raises(bridge.RequestError, match="lost response"):
        worker.report(attempt_id, status="succeeded", text="Summary", files=[artifact])
    pending = copy.deepcopy(worker._read(attempt_id)["pending"])
    artifact.write_text("Subsequent change must not alter retry.\n")
    restored = bridge.WorkerBridge(worker.home, client=client)
    gate = muse.Dispatcher(restored)
    assert gate.claim()["next"] == "status_then_retry"
    assert len(client.claims) == 1
    assert restored.retry(attempt_id)["state"] == "result_reported"
    assert client.calls[-1][2] == pending["payload"]
    assert restored._read(attempt_id)["pending"] is None
    assert gate.claim()["claimed"]


def test_pending_after_rotation_requires_explicit_owner_stop_and_preserves_outbox(tmp_path):
    client, worker, dispatcher = configured(tmp_path)
    client.ready = True
    attempt_id = dispatcher.claim()["attempt_id"]
    client.lose_result = True
    with pytest.raises(bridge.RequestError):
        worker.report(attempt_id, status="succeeded", text="Summary")
    pending = copy.deepcopy(worker._read(attempt_id)["pending"])
    client.stale = True
    with pytest.raises(bridge.BridgeError, match="No explicit owner stop"):
        dispatcher.reconcile()
    client.stopped = True
    assert dispatcher.claim()["busy"]
    assert dispatcher.reconcile() == {
        "released": True,
        "attempt_id": attempt_id,
        "pending_preserved": True,
    }
    assert worker._read(attempt_id)["pending"] == pending
    client.stale = False
    assert dispatcher.claim()["attempt_id"] != attempt_id


class StoreClient:
    """Real controller transitions with only the HTTP connection replaced."""

    origin = "https://control.example.test"
    space_id = "space_fixture"

    def __init__(self, store, agent_id):
        self.store, self.agent_id = store, agent_id
        self.lose_result = False
        self.validator = object.__new__(muse.VaultClient)
        self.validator.agent_id, self.validator.space_id = agent_id, self.space_id

    def request(self, method, action, payload=None):
        try:
            if action == "claim":
                result = self.store.claim(self.agent_id, payload["worker_id"])
            else:
                attempt_id, operation = action.split("/")[1:]
                if operation == "status":
                    result = self.store.attempt_status(attempt_id, self.agent_id)
                elif operation == "stop-receipt":
                    result = self.store.stop_receipt(attempt_id, self.agent_id)
                elif operation == "result":
                    assert not payload["artifacts"]  # This recovery test uses a text-only result.
                    result = self.store.report_result(
                        attempt_id,
                        self.agent_id,
                        generation=payload["generation"],
                        lease_token=payload["lease_token"],
                        text=payload["text"],
                        outcome=payload["status"],
                    )
                    if self.lose_result:
                        self.lose_result = False
                        raise bridge.RequestError("Synthetic lost response.")
                else:
                    raise AssertionError("Unexpected fixture operation.")
        except OrchestrationConflict:
            raise bridge.RequestError("Fenced fixture generation.", status=409) from None
        self.validator.validate_response(action, payload, result)
        return result


def test_accepted_lost_receipt_after_agent_update_needs_owner_stop_proof(db, clock, tmp_path):
    store = OrchestrationStore(db, clock, space_id=StoreClient.space_id)
    agent_id = store.create_agent("owner", name="Muse fixture", kind="muse")["agent"]["id"]

    def plan(request_id):
        task = store.create_task(
            "owner",
            request_id=request_id,
            title="Recovery fixture",
            steps=[
                {
                    "id": "work",
                    "agent_id": agent_id,
                    "prompt": "Summarize approved material.",
                    "acceptance": "Provide the completed summary.",
                }
            ],
        )
        store.approve_task(
            task["id"], "owner", revision=task["revision"], plan_hash=task["plan_hash"]
        )
        return task

    task = plan("first")
    client = StoreClient(store, agent_id)
    worker = bridge.WorkerBridge(tmp_path / "store-worker", client=client)
    dispatcher = muse.Dispatcher(worker)
    attempt_id = dispatcher.claim()["attempt_id"]
    client.lose_result = True
    with pytest.raises(bridge.RequestError, match="lost response"):
        worker.report(attempt_id, status="succeeded", text="Accepted original result.")
    pending = copy.deepcopy(worker._read(attempt_id)["pending"])
    store.accept_result(
        task["id"],
        "work",
        "owner",
        attempt_id=attempt_id,
        revision=task["revision"],
        plan_hash=task["plan_hash"],
    )
    accepted = dict(db.row("SELECT * FROM orchestration_attempts WHERE id=?", (attempt_id,)))
    store.update_agent(agent_id, "owner", name="Muse updated")
    assert store.get_agent(agent_id, "owner")["generation"] == accepted["agent_generation"] + 1
    for operation in (lambda: worker.status(attempt_id), lambda: worker.retry(attempt_id)):
        with pytest.raises(bridge.RequestError) as caught:
            operation()
        assert caught.value.status == 409
    assert worker.stop_receipt(attempt_id)["state"] == "accepted"
    with pytest.raises(bridge.BridgeError, match="No explicit owner stop"):
        dispatcher.reconcile()
    with pytest.raises(OrchestrationAccessDenied):
        store.confirm_stopped(task["id"], "work", "other", attempt_id, "Not the resource owner.")
    with pytest.raises(OrchestrationConflict, match="current attempt"):
        store.confirm_stopped(task["id"], "work", "owner", "attempt_wrong", "Wrong attempt.")
    assert dispatcher.claim()["next"] == "status_then_retry"

    note = "Verified the original Muse execution has ended."
    stopped = store.confirm_stopped(task["id"], "work", "owner", attempt_id, note)
    assert store.confirm_stopped(task["id"], "work", "owner", attempt_id, note) == stopped
    assert stopped["state"] == "succeeded"
    after = dict(db.row("SELECT * FROM orchestration_attempts WHERE id=?", (attempt_id,)))
    stop_fields = {"stopped_by", "stopped_at", "stop_note"}
    assert {key: value for key, value in after.items() if key not in stop_fields} == {
        key: value for key, value in accepted.items() if key not in stop_fields
    }
    assert (
        dispatcher.claim()["next"] == "status_then_retry"
    )  # Attestation alone does not erase pending.
    restored = muse.Dispatcher(bridge.WorkerBridge(worker.home, client=client))
    assert restored.reconcile() == {
        "released": True,
        "attempt_id": attempt_id,
        "pending_preserved": True,
    }
    assert restored.reconcile()["released"]
    assert worker._read(attempt_id)["pending"] == pending
    assert "result_receipt" not in worker._read(attempt_id)
    second = plan("second")
    claimed = restored.claim()
    assert claimed["claimed"] and claimed["task_id"] == second["id"]
    assert claimed["attempt_id"] != attempt_id
    assert worker._read(attempt_id)["pending"] == pending
    assert "result_receipt" not in worker._read(attempt_id)
    assert store.get_task(task["id"], "owner")["state"] == "succeeded"


@pytest.mark.parametrize(
    "invalid",
    [
        {"attempt_id": "attempt_wrong"},
        {"generation": 2},
        {"generation": True},
        {"generation": 1.0},
        {"stopped_by": None},
        {"stopped_by": ""},
        {"stopped_by": " "},
        {"stopped_by": True},
        {"stopped_at": None},
        {"stopped_at": 0},
        {"stopped_at": True},
        {"stopped_at": float("nan")},
        {"stopped_at": float("inf")},
        {"stopped_at": float("-inf")},
        {"state": "result_reported"},
    ],
)
def test_accepted_reconcile_rejects_mismatched_or_incomplete_stop_proof(
    tmp_path, monkeypatch, invalid
):
    client, worker, dispatcher = configured(tmp_path)
    client.ready = True
    attempt_id = dispatcher.claim()["attempt_id"]
    client.lose_result = True
    with pytest.raises(bridge.RequestError):
        worker.report(attempt_id, status="succeeded", text="Result awaiting confirmation.")
    pending = copy.deepcopy(worker._read(attempt_id)["pending"])
    proof = {
        "attempt_id": attempt_id,
        "generation": 1,
        "state": "accepted",
        "stopped_by": "owner",
        "stopped_at": 1234,
        **invalid,
    }
    monkeypatch.setattr(worker, "stop_receipt", lambda _: proof)
    with pytest.raises(bridge.BridgeError, match="No explicit owner stop"):
        dispatcher.reconcile()
    assert not dispatcher.read().get("released")
    assert worker._read(attempt_id)["pending"] == pending


@pytest.mark.parametrize(
    "invalid",
    [
        {"attempt_id": "attempt_wrong"},
        {"generation": 2},
        {"stopped_at": float("nan")},
        {"stopped_at": float("inf")},
        {"stopped_at": float("-inf")},
    ],
)
def test_claim_cannot_advance_nonce_on_invalid_old_generation_stop_receipt(
    db, clock, tmp_path, monkeypatch, invalid
):
    store = OrchestrationStore(db, clock, space_id=StoreClient.space_id)
    agent_id = store.create_agent("owner", name="Muse fixture", kind="muse")["agent"]["id"]

    def plan(request_id):
        task = store.create_task(
            "owner",
            request_id=request_id,
            title="Stop proof fixture",
            steps=[
                {
                    "id": "work",
                    "agent_id": agent_id,
                    "prompt": "Summarize approved material.",
                    "acceptance": "Return the completed summary.",
                }
            ],
        )
        store.approve_task(
            task["id"], "owner", revision=task["revision"], plan_hash=task["plan_hash"]
        )
        return task

    first = plan("first")
    client = StoreClient(store, agent_id)
    worker = bridge.WorkerBridge(tmp_path / "stop-proof-worker", client=client)
    dispatcher = muse.Dispatcher(worker)
    attempt_id = dispatcher.claim()["attempt_id"]
    record = worker._read(attempt_id)
    assert record["pending"] is None and "result_receipt" not in record
    saved_dispatch = dispatcher.path.read_bytes()
    saved_nonces = set((worker.home / "workers").iterdir())
    store.cancel_task(first["id"], "owner")
    store.confirm_stopped(first["id"], "work", "owner", attempt_id, "Verified execution ended.")
    store.update_agent(agent_id, "owner", name="Muse updated")
    second = plan("second")
    original_request = client.request
    new_claims = []

    def with_invalid_stop_receipt(method, action, payload=None):
        if action == "claim":
            new_claims.append(payload["worker_id"])
        result = original_request(method, action, payload)
        if action.endswith("/stop-receipt"):
            return {**result, **invalid}
        return result

    monkeypatch.setattr(client, "request", with_invalid_stop_receipt)
    with pytest.raises(bridge.RequestError) as fenced:
        worker.status(attempt_id)
    assert fenced.value.status == 409
    assert dispatcher.claim()["busy"]
    assert new_claims == []
    assert dispatcher.path.read_bytes() == saved_dispatch
    assert set((worker.home / "workers").iterdir()) == saved_nonces
    assert worker._read(attempt_id) == record

    monkeypatch.setattr(client, "request", original_request)
    resumed = dispatcher.claim()
    assert resumed["claimed"] and resumed["task_id"] == second["id"]
    assert resumed["attempt_id"] != attempt_id
    assert dispatcher.read()["worker_id"] != record["worker_id"]
    assert worker._read(attempt_id) == record


def test_local_status_never_uses_vault_or_network_and_preserves_private_ledger(
    tmp_path, monkeypatch
):
    client, worker, dispatcher = configured(tmp_path)
    client.ready = True
    attempt_id = dispatcher.claim()["attempt_id"]
    client.lose_result = True
    with pytest.raises(bridge.RequestError):
        worker.report(attempt_id, status="succeeded", text="Private pending result text.")
    saved = {
        path.relative_to(worker.home): path.read_bytes() for path in worker.home.rglob("*.json")
    }

    def forbidden(*args, **kwargs):
        pytest.fail("Local diagnostics must not access the Vault helper or HTTP transport.")

    monkeypatch.setattr(muse, "load_vault_helpers", forbidden)
    monkeypatch.setattr(muse.VaultClient, "request", forbidden)
    monkeypatch.setattr(muse.urllib.request, "build_opener", forbidden)
    args = muse.parser().parse_args(
        [
            "--url",
            client.origin,
            "--credential",
            "custom.worker",
            "--agent",
            client.agent_id,
            "--space",
            client.space_id,
            "--home",
            str(worker.home),
            "local-status",
        ]
    )
    output = muse.execute(args)
    assert output == {
        "initialized": True,
        "attempt_id": attempt_id,
        "delivered": True,
        "released": False,
        "pending_action": "result",
        "result_confirmed": False,
        "sequence": 0,
    }
    encoded = json.dumps(output)
    assert "fixture-private-lease" not in encoded and "Private pending result text." not in encoded
    assert {
        path.relative_to(worker.home): path.read_bytes() for path in worker.home.rglob("*.json")
    } == saved


def vault_client(monkeypatch):
    authorizations = []

    def authorize(request, credential, *, allowed_hosts):
        authorizations.append((request, credential, allowed_hosts))

    monkeypatch.setattr(
        muse,
        "load_vault_helpers",
        lambda: (authorize, lambda response: json.loads(response.read())),
    )
    client = muse.VaultClient(
        "https://control.example.test", "custom.worker", "agent_muse", "space_fixture"
    )
    return client, authorizations


def test_malformed_success_response_keeps_result_outbox(tmp_path, monkeypatch):
    fixture, worker, dispatcher = configured(tmp_path)
    fixture.ready = True
    attempt_id = dispatcher.claim()["attempt_id"]
    client, authorizations = vault_client(monkeypatch)

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            assert timeout == 120
            return Response(b'{"error":"unexpected success-shaped error"}')

    client.opener = Opener()
    worker.client = client
    with pytest.raises(bridge.RequestError, match="does not match"):
        worker.report(attempt_id, status="succeeded", text="Original report")
    assert worker._read(attempt_id)["pending"]["payload"]["text"] == "Original report"
    assert "result_receipt" not in worker._read(attempt_id)
    assert authorizations[0][1:] == ("custom.worker", ["control.example.test"])
    assert authorizations[0][0].full_url.startswith("https://control.example.test/api/v4/agents/")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "https://*.example.test",
        "https://user@example.test",
        "https://example.test/path",
    ],
)
def test_vault_rejects_ambiguous_or_insecure_origins(monkeypatch, url):
    monkeypatch.setattr(
        muse, "load_vault_helpers", lambda: pytest.fail("No helper before URL validation")
    )
    with pytest.raises((muse.AdapterError, bridge.BridgeError)):
        muse.VaultClient(url, "custom.worker", "agent_muse", "space_fixture")


def test_official_helper_missing_does_not_import_same_named_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(muse, "HELPERS", tmp_path / "missing-official-helper")
    monkeypatch.setitem(sys.modules, "dynamic_credentials", object())
    with pytest.raises(muse.AdapterError, match="official custom connector helper is unavailable"):
        muse.load_vault_helpers()


def test_external_bridge_needs_matching_explicit_hash(tmp_path):
    exported = tmp_path / "pa-agent-bridge.py"
    raw = Path(bridge.__file__).read_bytes()
    exported.write_bytes(raw)
    with pytest.raises(muse.AdapterError, match="independently verified"):
        muse.load_core(exported)
    with pytest.raises(muse.AdapterError, match="mismatch"):
        muse.load_core(exported, "0" * 64)
    loaded = muse.load_core(exported, hashlib.sha256(raw).hexdigest())
    assert loaded.VERSION == bridge.VERSION
    assert loaded.WorkerBridge.__name__ == "WorkerBridge"
