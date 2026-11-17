"""Execution through authenticated HTTP: isolation, revocation and durable node receipts."""

from __future__ import annotations

import base64
import hashlib

import pytest
from starlette.testclient import TestClient

from personal_agent.collaboration.models import OWNER_SPACE
from personal_agent.nodes.registry import DEFAULT_CAPABILITIES
from web_fake import Web


@pytest.fixture
def site(make_app):
    web = Web(make_app())
    web.pair()
    yield web
    web.client.close()


@pytest.fixture
def client_for(site):
    clients = []

    def make(principal="owner", *, token=None):
        client = TestClient(site.client.app, base_url=site.origin, headers=site.write)
        if token is not None:
            client.headers["Authorization"] = f"Bearer {token}"
        else:
            session = site.app.web_auth.create_session(principal)
            client.cookies.set("pa_session", session.token)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def create(client, *, space=OWNER_SPACE, key="request-1", **kwargs):
    return client.post(
        "/api/v3/tasks",
        params={"space": space},
        json={
            "client_request_id": key,
            "title": "Compute report",
            "command": "printf report",
            **kwargs,
        },
    )


def approve(client, task, *, space=OWNER_SPACE):
    job = task["jobs"][0]
    response = client.post(
        f"/api/v3/jobs/{job['id']}/approve",
        params={"space": space},
        json={"allowed": True, "plan_hash": job["plan_hash"]},
    )
    assert response.status_code == 200, response.text


def pair_node(site, client_for, *, spaces=None):
    invitation = site.client.post(
        "/api/v3/node-invites",
        headers=site.write,
        json={"space_ids": spaces or [OWNER_SPACE], "label": "Owned computer"},
    )
    assert invitation.status_code == 201, invitation.text
    paired = site.client.post(
        "/api/v3/nodes/pair",
        headers=site.write,
        json={"code": invitation.json()["code"], "capabilities": list(DEFAULT_CAPABILITIES)},
    )
    assert paired.status_code == 200, paired.text
    body = paired.json()
    return client_for(token=body["token"]), body["node"]["id"]


def claim(client):
    response = client.post("/api/v3/nodes/claim", json={"capabilities": list(DEFAULT_CAPABILITIES)})
    assert response.status_code == 200, response.text
    return response.json()["attempt"]


def report(client, env, action, **kwargs):
    return client.post(
        f"/api/v3/nodes/attempts/{env['attempt_id']}/{action}",
        json={
            "space_id": env["space_id"],
            "generation": env["generation"],
            "plan_hash": env["plan_hash"],
            **kwargs,
        },
    )


def admitted(client, env):
    response = report(client, env, "admit", resource_id="container-" + env["attempt_id"])
    assert response.status_code == 200, response.text


def member(site, *, role="member"):
    identities = site.app.identities
    project = identities.create_project("owner", "Shared research")
    invite = identities.create_member_invite("owner", project.id, role)
    return identities.redeem_member(invite.code, label="Collaborator")


def test_http_plan_approval_node_result_artifact_and_ack_are_durable(site, client_for):
    owner = client_for()
    site.app.paths.workspace_dir.joinpath("input.txt").write_text("approved snapshot")
    response = create(owner, input_paths=["input.txt"], output_paths=["report.txt"])
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert "content_base64" not in task["jobs"][0]["plan"]["input_manifest"][0]
    repeated = create(owner, input_paths=["input.txt"], output_paths=["report.txt"])
    assert repeated.json()["task"]["id"] == task["id"]
    assert create(owner, command="printf changed").status_code == 409
    node, _ = pair_node(site, client_for)
    assert claim(node) is None
    approve(owner, task)
    env = claim(node)
    assert claim(node)["attempt_id"] == env["attempt_id"]
    assert base64.b64decode(env["input_manifest"][0]["content_base64"]) == b"approved snapshot"
    admitted(node, env)
    assert (
        report(node, env, "observe", state="running", sequence=1, stdout="working").status_code
        == 200
    )
    output = b"result"
    result = {
        "status": "succeeded",
        "exit_code": 0,
        "stdout": "finished",
        "artifacts": [
            {
                "path": "report.txt",
                "size": len(output),
                "sha256": hashlib.sha256(output).hexdigest(),
                "content_base64": base64.b64encode(output).decode(),
            }
        ],
    }
    done = report(node, env, "complete", **result)
    assert done.status_code == 200 and done.json()["accepted"], done.text
    assert report(node, env, "complete", **result).status_code == 200
    assert site.app.db.scalar("SELECT COUNT(*) FROM events WHERE source='execution'") == 1
    assert site.app.db.scalar("SELECT COUNT(*) FROM artifacts") == 1
    assert site.app.db.scalar("SELECT COUNT(*) FROM outbox") == 2
    assert report(node, env, "cleanup", released=True).status_code == 200
    assert site.app.db.scalar("SELECT COUNT(*) FROM execution_inputs") == 0
    assert claim(node) is None
    artifact = site.app.jobs.get_job(env["job_id"])["result"]["artifacts"][0]
    assert artifact["sha256"] == hashlib.sha256(output).hexdigest()


def test_members_and_viewers_cannot_self_approve_or_cross_private_spaces(site, client_for):
    enrollment = member(site)
    collaborator = client_for(enrollment.principal.id)
    project = enrollment.space.id
    response = create(
        collaborator, space=project, actor_id="owner", approver_id=enrollment.principal.id
    )
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert task["actor_id"] == enrollment.principal.id
    job = task["jobs"][0]
    assert job["approver_id"] == "owner"
    rejected = collaborator.post(
        f"/api/v3/jobs/{job['id']}/approve",
        params={"space": project},
        json={"allowed": True, "plan_hash": job["plan_hash"]},
    )
    assert rejected.status_code == 403
    assert collaborator.get("/api/v3/tasks", params={"space": OWNER_SPACE}).status_code == 403
    assert client_for().get(f"/api/v3/tasks/{task['id']}").status_code == 404
    identities = site.app.identities
    identities.set_role("owner", project, enrollment.principal.id, "viewer")
    assert create(collaborator, space=project, key="viewer-request").status_code == 403
    assert collaborator.get("/api/v3/tasks", params={"space": project}).status_code == 200


def test_admin_can_review_resource_plan_without_access_to_personal_history(site, client_for):
    enrollment = member(site)
    private = site.app.identities.personal_space(enrollment.principal.id).id
    collaborator, owner = client_for(enrollment.principal.id), client_for()
    response = create(collaborator, space=private)
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert owner.get("/api/v3/tasks", params={"space": private}).status_code == 403
    pending = owner.get("/api/v3/review-jobs").json()["pending"]
    assert any(
        item["space_id"] == private and item["job"]["id"] == task["jobs"][0]["id"]
        for item in pending
    )
    job = task["jobs"][0]
    result = owner.post(
        "/api/v3/review-jobs",
        json={
            "space_id": private,
            "job_id": job["id"],
            "plan_hash": job["plan_hash"],
            "allowed": True,
        },
    )
    assert result.status_code == 200, result.text


def test_status_and_reports_require_exact_node_plan_and_generation(site, client_for):
    owner = client_for()
    task = create(owner).json()["task"]
    approve(owner, task)
    node, _ = pair_node(site, client_for)
    other, _ = pair_node(site, client_for)
    env = claim(node)
    assert report(other, env, "status").status_code == 403
    assert report(node, env, "status", plan_hash="0" * 64).status_code == 409
    assert report(node, env, "status", generation=2).status_code == 409
    assert report(node, env, "complete", status="succeeded", exit_code=0).status_code == 409
    unknown = {**env, "attempt_id": "0" * 32}
    assert report(node, unknown, "status").status_code == 404
    admitted(node, env)
    assert report(node, env, "cleanup", released=True).status_code == 409


def test_member_revocation_and_rejoin_fences_claim_status_and_delivery(site, client_for):
    enrollment = member(site)
    project = enrollment.space.id
    owner, collaborator = client_for(), client_for(enrollment.principal.id)
    task = create(collaborator, space=project).json()["task"]
    approve(owner, task, space=project)
    node, _ = pair_node(site, client_for, spaces=[project])
    env = claim(node)
    admitted(node, env)
    identities = site.app.identities
    identities.revoke_member("owner", project, enrollment.principal.id)
    identities.redeem_member(
        identities.create_member_invite("owner", project).code,
        existing_principal_id=enrollment.principal.id,
    )
    assert claim(node) is None
    status = report(node, env, "status")
    assert status.status_code == 200
    assert status.json()["authorized"] is False and status.json()["desired_state"] == "cancel"
    assert "plan" not in status.json() and "input_manifest" not in status.json()
    assert report(node, env, "complete", status="succeeded", exit_code=0).status_code == 200
    scoped = site.app.collaboration.get(project)
    assert scoped.db.scalar("SELECT COUNT(*) FROM outbox") == 0
    assert scoped.db.row("SELECT status FROM events WHERE source='execution'")["status"] == "done"


def test_node_grant_revocation_never_retransmits_inputs_or_claims_second_job(site, client_for):
    owner = client_for()
    task = create(owner).json()["task"]
    approve(owner, task)
    node, node_id = pair_node(site, client_for)
    env = claim(node)
    admitted(node, env)
    project = site.app.identities.create_project("owner", "Other space")
    second = create(owner, space=project.id).json()["task"]
    approve(owner, second, space=project.id)
    site.app.nodes.set_grants(node_id, "owner", [project.id], DEFAULT_CAPABILITIES)
    site.app.nodes.set_grants(node_id, "owner", [project.id, OWNER_SPACE], DEFAULT_CAPABILITIES)
    assert claim(node) is None  # uncertain/cancelled work still occupies the node slot
    response = report(node, env, "status")
    assert response.json()["authorized"] is False
    assert response.json()["desired_state"] == "cancel"
    assert report(node, env, "complete", status="cancelled").json()["accepted"]
    assert report(node, env, "cleanup", released=True).json()["accepted"]
    assert claim(node)["job_id"] == second["jobs"][0]["id"]


def test_cancel_between_claim_and_admission_can_be_confirmed_without_running(site, client_for):
    owner = client_for()
    task = create(owner).json()["task"]
    approve(owner, task)
    node, _ = pair_node(site, client_for)
    env = claim(node)
    assert owner.post(f"/api/v3/tasks/{task['id']}/cancel", json={}).status_code == 200
    assert report(node, env, "status").json()["desired_state"] == "cancel"
    assert report(node, env, "complete", status="cancelled").json()["accepted"]
    assert report(node, env, "cleanup", released=True).json()["accepted"]
    assert site.app.jobs.get_task(task["id"])["state"] == "cancelled"


def test_steps_need_separate_approval_and_wait_for_successful_predecessor(site, client_for):
    owner = client_for()
    task = create(owner).json()["task"]
    second = owner.post(
        f"/api/v3/tasks/{task['id']}/steps",
        json={
            "expected_revision": 1,
            "step_id": "2",
            "command": "printf second",
            "output_paths": [],
        },
    )
    assert second.status_code == 201, second.text
    job = second.json()["job"]
    response = owner.post(
        f"/api/v3/jobs/{job['id']}/approve",
        json={
            "allowed": True,
            "plan_hash": job["plan_hash"],
        },
    )
    assert response.status_code == 200
    node, _ = pair_node(site, client_for)
    assert claim(node) is None
    approve(owner, task)
    first = claim(node)
    admitted(node, first)
    assert report(node, first, "complete", status="succeeded", exit_code=0).json()["accepted"]
    assert claim(node)["job_id"] == job["id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"timeout_s": True},
        {"timeout_s": 7200},
        {"output_paths": "result.txt"},
        {"input_paths": ["../secret"]},
        {"input_paths": [".env"]},
    ],
)
def test_invalid_plans_never_enter_queue(site, client_for, payload):
    result = create(client_for(), **payload)
    assert result.status_code == 400, result.text
    assert site.app.db.scalar("SELECT COUNT(*) FROM execution_tasks") == 0


def test_budget_limit_keeps_cancel_and_status_available(site, client_for):
    owner = client_for()
    site.app.jobs.max_pending = 1
    task = create(owner).json()["task"]
    assert create(owner, key="too-many").status_code == 400
    assert owner.get(f"/api/v3/tasks/{task['id']}").status_code == 200
    assert owner.post(f"/api/v3/tasks/{task['id']}/cancel", json={}).status_code == 200
    assert create(owner, key="next").status_code == 201


def test_writable_collaborator_can_answer_once_but_viewer_cannot(site, client_for):
    enrollment = member(site)
    owner, collaborator = client_for(), client_for(enrollment.principal.id)
    project = enrollment.space.id
    task = create(owner, space=project).json()["task"]
    url = f"/api/v3/tasks/{task['id']}/questions"
    request = {
        "expected_revision": 1,
        "question": "Which output?",
        "choices": ["text", "csv"],
        "request_id": "format",
    }
    assert collaborator.post(url, params={"space": project}, json=request).status_code == 400
    asked = owner.post(url, params={"space": project}, json=request)
    assert asked.status_code == 201, asked.text
    question = asked.json()["question"]
    answer_url = f"{url}/{question['id']}/answer"
    site.app.identities.set_role("owner", project, enrollment.principal.id, "viewer")
    payload = {"expected_revision": 1, "answer": "text"}
    assert collaborator.post(answer_url, params={"space": project}, json=payload).status_code == 403
    site.app.identities.set_role("owner", project, enrollment.principal.id, "member")
    response = collaborator.post(answer_url, params={"space": project}, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["question"]["answered_by"] == enrollment.principal.id
    assert collaborator.post(answer_url, params={"space": project}, json=payload).status_code == 200
    assert (
        collaborator.post(
            answer_url, params={"space": project}, json={**payload, "answer": "csv"}
        ).status_code
        == 409
    )
