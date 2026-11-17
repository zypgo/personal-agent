"""Strict public contracts preserve authentication order, privacy and existing protocol fences."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from personal_agent.channels.web.openapi import document
from test_v3_execution_http import client_for as client_for
from test_v3_execution_http import create, member, pair_node, report
from test_v3_execution_http import site as site


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", 42),
        ("command", ["echo", "hello"]),
        ("timeout_s", True),
        ("timeout_s", "60"),
        ("timeout_s", float("inf")),
        ("input_paths", "input.txt"),
        ("input_paths", [1]),
        ("output_paths", [None]),
        ("client_request_id", 42),
    ],
)
def test_task_contract_refuses_coercion_before_creating_any_record(site, client_for, field, value):
    # stdlib JSON can encode Infinity; it still must not enter a durable execution plan.
    payload = {"title": "Task", "command": "true", "client_request_id": "strict", field: value}
    refused = client_for().post(
        "/api/v3/tasks", content=json.dumps(payload), headers={"Content-Type": "application/json"}
    )
    assert refused.status_code == 400
    assert set(refused.json()) == {"error"}
    assert not site.app.jobs.list_tasks()


def test_authentication_and_space_permission_win_over_invalid_json(site, client_for):
    with TestClient(site.client.app, base_url=site.origin, headers=site.write) as anonymous:
        assert anonymous.post("/api/v3/tasks", content="not JSON").status_code == 401
    viewer = member(site, role="viewer")
    response = client_for(viewer.principal.id).post(
        "/api/v3/tasks", params={"space": viewer.space.id}, content="not JSON"
    )
    assert response.status_code == 403
    assert not site.app.collaboration.get(viewer.space.id).jobs.list_tasks()


def test_validation_error_never_echoes_a_secret_or_the_pydantic_input(site, client_for):
    secret = "not-a-real-secret-should-not-appear-in-errors"
    response = client_for().post("/api/v3/devices/pair", json={"label": {"secret": secret}})
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid label."}
    assert secret not in response.text
    assert "input" not in response.text and "ctx" not in response.text
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("revision", [True, "0", 0.0])
def test_knowledge_revision_is_a_strict_integer(site, client_for, revision):
    response = client_for().post(
        "/api/v3/knowledge",
        json={"path": "note.md", "text": "must not persist", "revision": revision},
    )
    assert response.status_code == 400
    assert (
        site.app.db.scalar("SELECT COUNT(*) FROM memory_documents WHERE path LIKE '%/knowledge/%'")
        == 0
    )


def test_member_revoke_requires_a_boolean_and_cannot_be_a_truthy_string(site, client_for):
    enrollment = member(site)
    response = client_for().post(
        "/api/v3/members",
        params={"space": enrollment.space.id},
        json={"principal_id": enrollment.principal.id, "revoke": "false"},
    )
    assert response.status_code == 400
    assert site.app.identities.require_access(
        enrollment.principal.id, enrollment.space.id
    ).can_write


def test_node_generation_keeps_conflict_semantics_for_wrong_json_type(site, client_for):
    from test_v3_execution_http import approve, claim

    owner = client_for()
    task = create(owner).json()["task"]
    approve(owner, task)
    node, _ = pair_node(site, client_for)
    attempt = claim(node)
    for value in [True, "1", 1.0, None]:
        response = report(node, attempt, "status", generation=value)
        assert response.status_code == 409
        assert response.json() == {"error": "Stale attempt generation."}


def test_older_step_clients_can_still_send_the_revision_alias(site, client_for):
    owner = client_for()
    task = create(owner).json()["task"]
    response = owner.post(
        f"/api/v3/tasks/{task['id']}/steps",
        json={"revision": 1, "step_id": "2", "command": "printf second"},
    )
    assert response.status_code == 201
    assert response.json()["job"]["task_revision"] == 1


def test_openapi_is_authenticated_private_and_does_not_enable_external_docs(site, client_for):
    with TestClient(site.client.app, base_url=site.origin, headers=site.write) as anonymous:
        assert anonymous.get("/api/v3/openapi.json").status_code == 401
    response = client_for().get("/api/v3/openapi.json")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    schema = response.json()
    assert schema == document()
    for path in ["/docs", "/redoc", "/openapi.json", "/api/v3/docs", "/api/v3/redoc"]:
        assert client_for().get(path).status_code == 404
    assert schema["components"]["securitySchemes"]["browserSession"]["name"] == "pa_session"
    assert schema["paths"]["/api/v3/nodes/claim"]["post"]["security"] == [{"nodeBearer": []}]
    assert schema["paths"]["/api/v3/enroll"]["post"]["security"] == []


def test_every_schema_reference_resolves_and_request_limits_match_the_runtime_contract():
    schema = document()

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                assert reference.startswith("#/")
                target = schema
                for key in reference[2:].split("/"):
                    target = target[key.replace("~1", "/").replace("~0", "~")]
                assert target
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    task = schema["components"]["schemas"]["CreateTask"]
    assert task["properties"]["title"]["maxLength"] == 200
    assert task["properties"]["input_paths"]["maxItems"] == 32
    assert task["properties"]["timeout_s"]["maximum"] == 3600
    assert "actor_id" not in task["properties"]
    request_body = schema["paths"]["/api/v3/tasks"]["post"]["requestBody"]
    assert request_body["content"]["application/json"]["schema"]["$ref"].endswith("/CreateTask")
    assert schema["paths"]["/api/v3/tasks"]["get"]["security"] == [{"browserSession": []}]


def test_get_routes_keep_head_compatibility(site, client_for):
    response = client_for().head("/api/v3/me")
    assert response.status_code == 200 and not response.content
    assert response.headers["cache-control"] == "private, no-store"


def test_schema_export_does_not_initialize_application_data(tmp_path, monkeypatch):
    home = tmp_path / "unused-agent-home"
    monkeypatch.setenv("PA_HOME", str(home))
    target = tmp_path / "api.json"
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, str(root / "scripts/export_api_schema.py"), "--output", str(target)],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    )
    assert not home.exists()
    assert json.loads(target.read_text()) == document()
