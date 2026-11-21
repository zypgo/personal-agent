"""A task is a thread: where it stands, putting it away, bringing it back and quieting it."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from test_agent_orchestration_http import agents as agents
from test_agent_orchestration_http import approve, create_task
from test_agent_orchestration_http import site as site
from test_v3_collaboration_http import group as group


def thread(site, task_id, action, data=None, **kwargs):
    return site.client.post(
        f"/api/v4/tasks/{task_id}/{action}", headers=site.write, json=data or {}, **kwargs
    )


@pytest.fixture
def task(site, agents):
    grok, _worker, _path = agents()
    return create_task(site, [grok["agent"]["id"]])


def standing(site, task_id):
    tasks = site.client.get("/api/v4/tasks").json()["tasks"]
    return next(t for t in tasks if t["id"] == task_id)["thread"]


def test_every_task_says_where_it_stands(site, task):
    waiting = standing(site, task["id"])
    assert waiting["phase"] == "needs_you" and waiting["need"] == "approve"
    assert waiting["label"] and waiting["archived"] is False and waiting["snoozed_until"] is None
    approve(site, task)
    running = standing(site, task["id"])
    assert running["phase"] == "running" and running["need"] is None
    single = site.client.get(f"/api/v4/tasks/{task['id']}").json()["task"]
    assert single["thread"]["phase"] == "running"


def test_a_task_that_is_not_over_cannot_be_put_away(site, task):
    refused = thread(site, task["id"], "archive")
    assert refused.status_code == 409 and "还没有结束" in refused.json()["error"]
    assert standing(site, task["id"])["archived"] is False


def test_a_finished_task_is_put_away_and_brought_back(site, task):
    site.client.post(f"/api/v4/tasks/{task['id']}/cancel", headers=site.write, json={})
    assert standing(site, task["id"])["phase"] == "closed"
    away = thread(site, task["id"], "archive")
    assert away.status_code == 200
    assert away.json()["task"]["thread"]["phase"] == "archived"
    assert standing(site, task["id"])["archived"] is True
    assert site.client.get(f"/api/v4/tasks/{task['id']}").status_code == 200  # still there
    again = thread(site, task["id"], "archive")  # saying it twice changes nothing
    assert again.status_code == 200
    back = thread(site, task["id"], "unarchive")
    assert back.json()["task"]["thread"] == {**standing(site, task["id"]), "archived": False}
    assert standing(site, task["id"])["phase"] == "closed"


def test_a_task_can_be_left_alone_for_a_while(site, task):
    approve(site, task)
    quiet = thread(site, task["id"], "snooze", {"hours": 2})
    assert quiet.status_code == 200
    until = quiet.json()["task"]["thread"]["snoozed_until"]
    assert until == pytest.approx(site.app.clock.now() + 2 * 3600)
    default = thread(site, task["id"], "snooze", {})
    assert default.json()["task"]["thread"]["snoozed_until"] == pytest.approx(
        site.app.clock.now() + 24 * 3600
    )


@pytest.mark.parametrize("hours", [0, -1, 721, "soon", True, None, float("inf")])
def test_a_nonsense_snooze_is_refused(site, task, hours):
    approve(site, task)
    refused = site.client.post(
        f"/api/v4/tasks/{task['id']}/snooze",
        content=json.dumps({"hours": hours}),
        headers={**site.write, "Content-Type": "application/json"},
    )
    assert refused.status_code == 400
    assert standing(site, task["id"])["snoozed_until"] is None


def test_the_routes_ask_for_a_session_and_the_page_header(site, task):
    site.client.post(f"/api/v4/tasks/{task['id']}/cancel", headers=site.write, json={})
    no_header = site.client.post(f"/api/v4/tasks/{task['id']}/archive", json={})
    assert no_header.status_code == 403
    with TestClient(site.client.app, base_url=site.origin, headers=site.write) as anonymous:
        assert anonymous.post(f"/api/v4/tasks/{task['id']}/archive", json={}).status_code == 401
    assert standing(site, task["id"])["archived"] is False
    unknown = thread(site, "missing", "archive")
    assert unknown.status_code == 404


def test_a_member_cannot_put_away_the_owners_task(group):
    _root, shared, _private, _member, owner, user = group
    space = {"space": shared.space_id}
    created = owner.client.post(
        "/api/v4/agents", params=space, headers=owner.write, json={"name": "g", "kind": "grok"}
    )
    agent = created.json()["agent"]["id"]
    made = owner.client.post(
        "/api/v4/tasks",
        params=space,
        headers=owner.write,
        json={
            "client_request_id": "shared-task",
            "title": "查一下",
            "visibility": "space",
            "steps": [{"id": "s1", "agent_id": agent, "prompt": "go"}],
        },
    )
    task_id = made.json()["task"]["id"]
    owner.client.post(f"/api/v4/tasks/{task_id}/cancel", params=space, headers=owner.write, json={})
    seen = user.client.get("/api/v4/tasks", params=space).json()["tasks"]
    assert seen[0]["thread"]["phase"] == "closed"
    for action in ("archive", "unarchive", "snooze"):
        refused = user.client.post(
            f"/api/v4/tasks/{task_id}/{action}", params=space, headers=user.write, json={}
        )
        assert refused.status_code == 403
    away = owner.client.post(
        f"/api/v4/tasks/{task_id}/archive", params=space, headers=owner.write, json={}
    )
    assert away.status_code == 200 and away.json()["task"]["thread"]["archived"] is True
