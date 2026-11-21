"""Cloud workers through the actual authenticated API, including the official MCP SDK."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.testclient import TestClient

from personal_agent.agent_adapters.cloud import WakeReceipt
from personal_agent.orchestration.service import OrchestrationService
from web_fake import Web


@pytest.fixture
def site(make_app):
    web = Web(make_app())
    web.pair()
    yield web
    web.client.close()


@pytest.fixture
def agents(site):
    clients = []

    def create(kind="grok", **kwargs):
        response = site.client.post(
            "/api/v4/agents",
            headers=site.write,
            json={"name": kind, "kind": kind, **kwargs},
        )
        assert response.status_code == 201, response.text
        result = response.json()
        # A native connector has no browser cookies or custom CSRF header.
        worker = TestClient(
            site.client.app,
            base_url=site.origin,
            headers={
                "Authorization": "Bearer " + result["worker_token"],
            },
        )
        clients.append(worker)
        return result, worker, f"/api/v4/agents/{result['agent']['id']}/worker"

    yield create
    for client in clients:
        client.close()


def create_task(site, agent_ids, *, key="task-request", dependencies=True):
    response = site.client.post(
        "/api/v4/tasks",
        headers=site.write,
        json={
            "client_request_id": key,
            "title": "Cloud agent collaboration",
            "steps": [
                {
                    "id": f"step{i}",
                    "agent_id": agent_id,
                    "prompt": f"Research part {i}",
                    "acceptance": "Provide a sourced result.",
                    "depends_on": [f"step{i - 1}"] if i and dependencies else [],
                }
                for i, agent_id in enumerate(agent_ids)
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


def approve(site, task):
    response = site.client.post(
        f"/api/v4/tasks/{task['id']}/approve",
        headers=site.write,
        json={"revision": task["revision"], "plan_hash": task["plan_hash"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]


def claim(worker, path, nonce="turn-1"):
    response = worker.post(path + "/claim", json={"worker_id": nonce})
    assert response.status_code == 200, response.text
    return response.json()["attempt"]


def report(worker, path, attempt, action="result", **fields):
    return worker.post(
        path + f"/runs/{attempt['attempt_id']}/{action}",
        json={
            "generation": attempt["generation"],
            "lease_token": attempt["lease_token"],
            **fields,
        },
    )


def accept(site, task, step, attempt):
    response = site.client.post(
        f"/api/v4/tasks/{task['id']}/accept",
        headers=site.write,
        json={
            "step_id": step,
            "attempt_id": attempt["attempt_id"],
            "revision": task["revision"],
            "plan_hash": task["plan_hash"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]


def test_grok_to_muse_requires_acceptance_and_preserves_files(site, agents, clock):
    grok, first, path1 = agents()
    muse, second, path2 = agents("muse", wake_mode="native_schedule")
    ids = [grok["agent"]["id"], muse["agent"]["id"]]
    task = create_task(site, ids)
    clock.advance(2)
    assert create_task(site, ids)["id"] == task["id"]
    assert claim(first, path1) is None
    approve(site, task)
    run = claim(first, path1)
    assert claim(first, path1) == run
    assert claim(first, path1, "different-turn") is None
    assert claim(second, path2) is None
    assert report(first, path1, run, "heartbeat").status_code == 200
    content = b"Sourced research result"
    file = {
        "name": "research.txt",
        "mime": "text/plain",
        "content_base64": base64.b64encode(content).decode(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    done = report(first, path1, run, text="Research complete", artifacts=[file])
    assert done.status_code == 200, done.text
    assert report(first, path1, run, text="Research complete", artifacts=[file]).status_code == 200
    assert claim(second, path2) is None
    reviewed = accept(site, task, "step0", run)
    file_id = reviewed["steps"][0]["attempt"]["result"]["artifacts"][0]["id"]
    downloaded = site.client.get(f"/api/v4/artifacts/{file_id}")
    assert downloaded.content == content
    assert "attachment" in downloaded.headers["content-disposition"]
    assert "lease_token" not in json.dumps(reviewed)
    next_run = claim(second, path2)
    assert next_run["payload"]["dependencies"][0]["text"] == "Research complete"
    dependency = second.get(path2 + f"/runs/{next_run['attempt_id']}/artifacts/{file_id}")
    assert dependency.content == content, dependency.text
    encoded = second.get(
        path2 + f"/runs/{next_run['attempt_id']}/artifacts/{file_id}?encoding=base64"
    )
    assert encoded.status_code == 200
    assert base64.b64decode(encoded.json()["content_base64"]) == content
    schema = second.get(path2 + "/openapi.json").json()
    assert (
        schema["paths"]["/runs/{attempt_id}/artifacts/{artifact_id}"]["get"]["operationId"]
        == "fetchArtifact"
    )
    assert report(second, path2, next_run, text="Combined result").status_code == 200
    assert accept(site, task, "step1", next_run)["state"] == "succeeded"


def test_rotated_worker_can_only_read_its_minimal_stop_receipt(site, agents):
    agent, worker, path = agents("codex")
    _other, other_worker, other_path = agents("cursor")
    task = create_task(site, [agent["agent"]["id"]])
    approve(site, task)
    run = claim(worker, path)
    rotated = site.client.post(f"/api/v4/agents/{agent['agent']['id']}/token", headers=site.write)
    assert rotated.status_code == 200
    worker.headers["Authorization"] = "Bearer " + rotated.json()["worker_token"]
    assert worker.get(path + f"/runs/{run['attempt_id']}/status").status_code == 409
    receipt_path = f"/runs/{run['attempt_id']}/stop-receipt"
    assert other_worker.get(other_path + receipt_path).status_code == 404
    before = worker.get(path + receipt_path)
    assert before.status_code == 200
    assert before.json()["stopped_at"] is None
    assert set(before.json()) == {
        "attempt_id",
        "state",
        "generation",
        "agent_generation",
        "stopped_by",
        "stopped_at",
    }
    stopped = site.client.post(
        f"/api/v4/tasks/{task['id']}/stopped",
        headers=site.write,
        json={
            "step_id": "step0",
            "attempt_id": run["attempt_id"],
            "note": "Verified process exited.",
        },
    )
    assert stopped.status_code == 200, stopped.text
    receipt = worker.get(path + receipt_path).json()
    assert receipt["state"] == "stopped" and receipt["stopped_by"] == "owner"
    assert receipt["stopped_at"] is not None
    assert worker.get(path + f"/runs/{run['attempt_id']}/status").status_code == 409


def test_revoked_report_requires_owner_stop_proof_with_current_worker_token(site, agents):
    agent, worker, path = agents()
    _other, other_worker, other_path = agents("cursor")
    task = create_task(site, [agent["agent"]["id"]])
    approve(site, task)
    run = claim(worker, path)
    reported = report(worker, path, run, text="Recorded result awaiting owner acceptance.")
    assert reported.status_code == 200
    assert reported.json()["state"] == "result_reported"
    rotated = site.client.post(f"/api/v4/agents/{agent['agent']['id']}/token", headers=site.write)
    assert rotated.status_code == 200
    receipt_path = f"/runs/{run['attempt_id']}/stop-receipt"
    assert worker.get(path + receipt_path).status_code == 401
    worker.headers["Authorization"] = "Bearer " + rotated.json()["worker_token"]
    assert worker.get(path + f"/runs/{run['attempt_id']}/status").status_code == 409
    before = worker.get(path + receipt_path)
    assert before.status_code == 200
    assert before.json()["state"] == "revoked" and before.json()["stopped_at"] is None
    assert other_worker.get(other_path + receipt_path).status_code == 404
    current = site.client.get(f"/api/v4/tasks/{task['id']}").json()["task"]
    assert current["state"] == "revoked" and current["steps"][0]["can_confirm_stopped"]
    proof_input = {
        "step_id": "step0",
        "attempt_id": run["attempt_id"],
        "note": "Verified the original native execution ended after its result was received.",
    }
    stopped_path = f"/api/v4/tasks/{task['id']}/stopped"
    assert worker.post(stopped_path, json=proof_input).status_code == 403
    assert worker.post(stopped_path, headers=site.write, json=proof_input).status_code == 401
    stopped = site.client.post(stopped_path, headers=site.write, json=proof_input)
    assert stopped.status_code == 200, stopped.text
    saved = stopped.json()["task"]
    assert saved["state"] == saved["steps"][0]["state"] == "revoked"
    assert saved["steps"][0]["attempt"]["result"] == current["steps"][0]["attempt"]["result"]
    assert not saved["steps"][0]["attempt"]["result_eligible"]
    assert not saved["steps"][0]["can_confirm_stopped"]
    receipt = worker.get(path + receipt_path).json()
    assert receipt == {
        **before.json(),
        "state": "stopped",
        "stopped_by": "owner",
        "stopped_at": saved["steps"][0]["attempt"]["stopped_at"],
    }
    assert receipt["stopped_at"] is not None
    assert worker.get(path + f"/runs/{run['attempt_id']}/status").status_code == 409
    assert report(worker, path, run, "heartbeat").status_code == 409
    following = create_task(site, [agent["agent"]["id"]], key="after-revoked-stop")
    approve(site, following)
    assert claim(worker, path, nonce="fresh-after-stop")["task_id"] == following["id"]


def test_worker_scope_csrf_and_rotation(site, agents):
    agent, worker, path = agents()
    _other, _, other_path = agents("muse")
    assert worker.get(other_path + "/status").status_code == 401
    assert worker.get("/api/v4/tasks").status_code == 401
    assert (
        worker.post(
            path + "/hello", json={}, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert (
        worker.post(path + "/hello", json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code
        == 403
    )
    assert site.client.post("/api/v4/tasks", json={}).status_code == 403
    invalid = TestClient(
        site.client.app, base_url=site.origin, headers={"Authorization": "Bearer " + "x" * 40}
    )
    try:
        denied = invalid.post(
            path + "/claim", content="not json", headers={"Content-Type": "application/json"}
        )
        assert denied.status_code == 401
    finally:
        invalid.close()
    rotated = site.client.post(f"/api/v4/agents/{agent['agent']['id']}/token", headers=site.write)
    assert rotated.status_code == 200
    assert worker.get(path + "/status").status_code == 401
    assert agent["worker_token"] not in site.client.get("/api/v4/agents").text


def test_cancellation_and_late_result_cannot_be_accepted(site, agents, clock):
    agent, worker, path = agents()
    task = approve(site, create_task(site, [agent["agent"]["id"]]))
    run = claim(worker, path)
    assert (
        site.client.post(f"/api/v4/tasks/{task['id']}/cancel", headers=site.write).status_code
        == 200
    )
    assert report(worker, path, run, "heartbeat").json()["desired_state"] == "cancel"
    reported = report(worker, path, run, text="Too late")
    assert reported.status_code == 200, reported.text
    response = site.client.post(
        f"/api/v4/tasks/{task['id']}/accept",
        headers=site.write,
        json={
            "step_id": "step0",
            "attempt_id": run["attempt_id"],
            "revision": task["revision"],
            "plan_hash": task["plan_hash"],
        },
    )
    assert response.status_code == 409


def test_question_is_answered_without_approving_new_actions(site, agents):
    agent, worker, path = agents("muse")
    task = approve(site, create_task(site, [agent["agent"]["id"]]))
    run = claim(worker, path)
    asked = report(
        worker,
        path,
        run,
        "question",
        request_id="format",
        text="Which format?",
        choices=["Markdown", "Text"],
    )
    assert asked.status_code == 200, asked.text
    answer = site.client.post(
        f"/api/v4/tasks/{task['id']}/questions/format/answer",
        headers=site.write,
        json={"answer": "Markdown"},
    )
    assert answer.status_code == 200, answer.text
    current = worker.get(path + f"/runs/{run['attempt_id']}/status").json()
    assert current["questions"][0]["answer"] == "Markdown"


def test_artifacts_fail_atomically_and_do_not_save_credentials(site, agents):
    agent, worker, path = agents()
    approve(site, create_task(site, [agent["agent"]["id"]]))
    run = claim(worker, path)

    def file(name, content):
        return {
            "name": name,
            "mime": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    result = report(
        worker,
        path,
        run,
        artifacts=[file("valid.txt", b"ok"), file("secret.txt", agent["worker_token"].encode())],
    )
    assert result.status_code == 400
    assert site.app.db.scalar("SELECT COUNT(*) FROM orchestration_artifacts") == 0
    assert agent["worker_token"] not in result.text
    result = report(worker, path, run, artifacts=[file(agent["worker_token"] + ".txt", b"ok")])
    assert result.status_code == 400
    assert site.app.db.scalar("SELECT COUNT(*) FROM orchestration_artifacts") == 0
    malformed = worker.post(
        path + f"/runs/{run['attempt_id']}/result",
        json={
            "generation": run["generation"],
            "lease_token": "无效凭据",
            "text": "output",
        },
    )
    assert malformed.status_code in {400, 409}
    result = report(worker, path, run, text="token " + agent["worker_token"])
    assert result.status_code == 200, result.text
    assert agent["worker_token"] not in result.text


@pytest.mark.parametrize("protocol", ["2025-11-25", LATEST_PROTOCOL_VERSION])
def test_standard_mcp_initialize_list_claim_and_result(site, agents, protocol):
    agent, worker, path = agents("muse")
    task = approve(site, create_task(site, [agent["agent"]["id"]]))
    mcp_headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol,
    }

    def rpc(method, params):
        if protocol == LATEST_PROTOCOL_VERSION:
            params["_meta"] = {
                "io.modelcontextprotocol/protocolVersion": protocol,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        response = worker.post(
            path + "/mcp",
            headers={
                **mcp_headers,
                "Mcp-Method": method,
                **({"Mcp-Name": params["name"]} if "name" in params else {}),
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]

    if protocol == "2025-11-25":
        initialized = rpc(
            "initialize",
            {
                "protocolVersion": protocol,
                "capabilities": {},
                "clientInfo": {"name": "test-muse", "version": "1"},
            },
        )
        assert initialized["serverInfo"]["name"] == "Qixu task connector"
    listing = rpc("tools/list", {})
    assert {t["name"] for t in listing["tools"]} >= {"claim_task", "submit_result", "ask_question"}
    claimed = rpc(
        "tools/call", {"name": "claim_task", "arguments": {"worker_id": "muse-native-turn"}}
    )
    assert not claimed.get("isError"), claimed
    run = claimed["structuredContent"]["attempt"]
    completed = rpc(
        "tools/call",
        {
            "name": "submit_result",
            "arguments": {
                "attempt_id": run["attempt_id"],
                "generation": run["generation"],
                "lease_token": run["lease_token"],
                "text": "Muse result",
            },
        },
    )
    assert not completed.get("isError"), completed
    assert accept(site, task, "step0", run)["state"] == "succeeded"


@pytest.mark.asyncio
async def test_wake_receipt_survives_timeout_without_resending(site, agents, monkeypatch, clock):
    agent, _, _ = agents(
        wake_mode="grok_routine",
        secret_env={"webhook_url": "TEST_GROK_URL", "webhook_key": "TEST_GROK_KEY"},
    )
    approve(site, create_task(site, [agent["agent"]["id"]]))
    sent = []

    async def wake(config, **kwargs):
        sent.append(kwargs)
        # A durable reservation must already exist before anything can reach the provider.
        assert (
            site.app.db.scalar("SELECT count(*) FROM orchestration_wakes WHERE state='reserved'")
            == 1
        )
        return WakeReceipt("wake_unknown", "delivery_unconfirmed")

    monkeypatch.setattr("personal_agent.orchestration.service.wake_cloud_agent", wake)
    service = OrchestrationService(site.app)
    assert await service.tick() == 1
    clock.advance(61)
    assert await service.tick() == 0
    assert len(sent) == 1
    assert site.app.db.one("SELECT state FROM orchestration_wakes")["state"] == "uncertain"


def test_openapi_and_downloaded_bridge_are_available(site, agents):
    _, worker, path = agents("muse")
    definition = worker.get(path + "/openapi.json")
    assert definition.status_code == 200, definition.text
    assert definition.json()["security"] == [{"agentToken": []}]
    script = site.client.get("/api/v4/bridge.py")
    assert script.status_code == 200
    assert hashlib.sha256(script.content).hexdigest() == script.headers["X-Content-SHA256"]
    assert b"def main" in script.content


def test_project_visibility_and_worker_grants_are_independent(site):
    identities = site.app.identities
    project = identities.create_project("owner", "Collaborative research")
    first_invite = identities.create_member_invite("owner", project.id, "member")
    first = identities.redeem_member(first_invite.code, label="First collaborator")
    viewer_invite = identities.create_member_invite("owner", project.id, "viewer")
    viewer = identities.redeem_member(viewer_invite.code, label="Reader")
    first_session = site.app.web_auth.create_session(first.principal.id)
    viewer_session = site.app.web_auth.create_session(viewer.principal.id)
    with (
        TestClient(site.client.app, base_url=site.origin, headers=site.write) as member_client,
        TestClient(
            site.client.app,
            base_url=site.origin,
            headers=site.write,
        ) as reader,
    ):
        member_client.cookies.set("pa_session", first_session.token)
        reader.cookies.set("pa_session", viewer_session.token)
        params = {"space": project.id}
        agent = member_client.post(
            "/api/v4/agents", params=params, json={"name": "Muse", "kind": "muse"}
        )
        assert agent.status_code == 201, agent.text
        agent_id = agent.json()["agent"]["id"]
        body = {
            "client_request_id": "member-private",
            "title": "Private result",
            "steps": [{"id": "research", "agent_id": agent_id, "prompt": "private question"}],
        }
        private = member_client.post("/api/v4/tasks", params=params, json=body)
        assert private.status_code == 201, private.text
        task_id = private.json()["task"]["id"]
        assert site.client.get(f"/api/v4/tasks/{task_id}", params=params).status_code == 404
        assert reader.get(f"/api/v4/tasks/{task_id}", params=params).status_code == 404
        shared = member_client.post(
            "/api/v4/tasks",
            params=params,
            json={**body, "client_request_id": "shared", "visibility": "space"},
        )
        assert shared.status_code == 201, shared.text
        shared_id = shared.json()["task"]["id"]
        visible = reader.get(f"/api/v4/tasks/{shared_id}", params=params)
        assert visible.status_code == 200, visible.text
        assert not visible.json()["task"]["can_approve"]
        assert (
            reader.post(f"/api/v4/tasks/{shared_id}/cancel", params=params, json={}).status_code
            == 403
        )
        # Project ownership does not implicitly grant access to another member's cloud account.
        ungranted = site.client.post(
            "/api/v4/tasks",
            params=params,
            headers=site.write,
            json={**body, "client_request_id": "owner-ungranted"},
        )
        assert ungranted.status_code == 403


def test_expired_run_requires_confirmed_stop_before_new_work(site, agents, clock):
    agent, worker, path = agents()
    task = approve(site, create_task(site, [agent["agent"]["id"]]))
    run = claim(worker, path)
    clock.advance(301)
    current = site.client.get(f"/api/v4/tasks/{task['id']}").json()["task"]
    assert current["state"] == "uncertain"
    assert current["steps"][0]["can_confirm_stopped"]
    another = approve(site, create_task(site, [agent["agent"]["id"]], key="next-plan"))
    assert claim(worker, path, "new-turn") is None
    stopped = site.client.post(
        f"/api/v4/tasks/{task['id']}/stopped",
        headers=site.write,
        json={
            "step_id": "step0",
            "attempt_id": run["attempt_id"],
            "note": "Confirmed the original native task has stopped.",
        },
    )
    assert stopped.status_code == 200, stopped.text
    assert claim(worker, path, "new-turn")["task_id"] == another["id"]


def test_token_rotation_after_body_authentication_cannot_claim_new_generation(
    site, agents, monkeypatch
):
    agent, worker, path = agents()
    approve(site, create_task(site, [agent["agent"]["id"]]))
    store = site.app.orchestration
    original = store.authenticate_worker
    calls = 0

    def authenticate(token):
        nonlocal calls
        authenticated = original(token)
        calls += 1
        if calls == 2:
            store.rotate_agent_token(agent["agent"]["id"], "owner")
        return authenticated

    monkeypatch.setattr(store, "authenticate_worker", authenticate)
    response = worker.post(path + "/claim", json={"worker_id": "stale-token"})
    assert response.status_code == 401, response.text
    assert site.app.db.scalar("SELECT COUNT(*) FROM orchestration_attempts") == 0
