"""Browser and node requests at the edge: what the HTTP layer refuses before anything else runs.

Most of what a person or a node may do is decided again deeper down, by the stores. These tests
pin what only the edge decides: the role needed for a route, the cookie a browser is given, how
often one source may knock, and what a node's request does to work it has no right to any more.
"""

from __future__ import annotations

import pytest

from personal_agent.channels.web.collaboration import V3Api
from personal_agent.collaboration.models import OWNER_SPACE, AccessDenied
from personal_agent.nodes.registry import DEFAULT_CAPABILITIES
from test_v3_authorization_boundaries import request
from test_v3_collaboration_http import group as group
from test_v3_execution_http import (
    admitted,
    approve,
    claim,
    create,
    member,
    pair_node,
    report,
)
from test_v3_execution_http import client_for as client_for
from test_v3_execution_http import site as site
from web_fake import Web

ALL = list(DEFAULT_CAPABILITIES)


def post(web, path, data):
    return web.client.post(path, json=data, headers=web.write)


# ---- the browser ----


def test_a_viewer_who_selects_a_project_cannot_write_through_the_chat_routes(site, client_for):
    viewer_enrollment = member(site, role="viewer")
    viewer = client_for(viewer_enrollment.principal.id)
    project = viewer_enrollment.space.id
    sent = viewer.post("/api/messages", params={"space": project}, files={"text": (None, "hi")})
    assert sent.status_code == 403
    assert viewer.get("/api/history", params={"space": project}).status_code == 200
    # Signing out is the one thing a viewer may do with a POST.
    assert viewer.post("/api/logout", params={"space": project}).status_code == 200


def test_a_writer_who_is_not_an_owner_cannot_decide_chat_approvals(site, client_for):
    enrollment = member(site)
    project = enrollment.space.id
    collaborator, owner = client_for(enrollment.principal.id), client_for()
    decision = {"decision": "approve"}
    refused = collaborator.post("/api/approvals/1", params={"space": project}, json=decision)
    assert refused.status_code == 403
    # An owner gets past the edge; what happens to an approval nobody asked for is up to the
    # approvals store.
    assert (
        owner.post("/api/approvals/1", params={"space": project}, json=decision).status_code != 403
    )


def test_a_write_to_a_route_nobody_has_written_yet_is_refused_by_role_first(site, client_for):
    # The edge asks for the role before it looks for the route, so a write route added later
    # starts out closed to viewers even if it forgets to ask.
    enrollment = member(site, role="viewer")
    project = enrollment.space.id
    viewer, owner = client_for(enrollment.principal.id), client_for()
    assert viewer.post("/api/not-yet-written", params={"space": project}).status_code == 403
    assert viewer.get("/api/not-yet-written", params={"space": project}).status_code == 404
    assert owner.post("/api/not-yet-written", params={"space": project}).status_code == 404


def test_everything_under_the_approvals_path_is_for_owners_only_at_the_edge(site, client_for):
    enrollment = member(site)
    project = enrollment.space.id
    collaborator, owner = client_for(enrollment.principal.id), client_for()
    path = "/api/approvals/1/not-yet-written"
    assert collaborator.post(path, params={"space": project}).status_code == 403
    assert owner.post(path, params={"space": project}).status_code == 404


def test_removing_a_member_on_the_web_cancels_their_tasks(site, client_for):
    enrollment = member(site)
    project = enrollment.space.id
    owner, collaborator = client_for(), client_for(enrollment.principal.id)
    task = create(collaborator, space=project).json()["task"]
    removed = owner.post(
        "/api/v3/members",
        params={"space": project},
        json={"principal_id": enrollment.principal.id, "revoke": True},
    )
    assert removed.status_code == 200, removed.text
    assert site.app.collaboration.get(project).jobs.get_task(task["id"])["state"] == "cancelled"


def test_a_member_who_became_a_viewer_cannot_cancel_the_task_they_asked_for(site, client_for):
    # Cancelling needs no request body, so the role needed for the route is decided here and
    # nowhere else: the task's requester may cancel it only while they may still write.
    enrollment = member(site)
    project = enrollment.space.id
    collaborator = client_for(enrollment.principal.id)
    task = create(collaborator, space=project).json()["task"]
    site.app.identities.set_role("owner", project, enrollment.principal.id, "viewer")
    refused = collaborator.post(
        f"/api/v3/tasks/{task['id']}/cancel", params={"space": project}, json={}
    )
    assert refused.status_code == 403
    assert site.app.collaboration.get(project).jobs.get_task(task["id"])["state"] != "cancelled"


async def test_a_request_that_outlives_a_membership_is_refused_after_it_is_read(group):
    root, shared, _, member, _, user = group

    def removed_and_added_again():
        root.identities.revoke_member("owner", shared.space_id, member.principal.id)
        invite = root.identities.create_member_invite("owner", shared.space_id)
        root.identities.redeem_member(invite.code, existing_principal_id=member.principal.id)

    with pytest.raises(AccessDenied):
        await V3Api(root, secure=False).tasks(
            request(
                "/api/v3/tasks",
                user.token(),
                query=f"space={shared.space_id}",
                data={"title": "Late", "command": "true", "client_request_id": "late"},
                after_read=removed_and_added_again,
            )
        )
    assert not shared.jobs.list_tasks()


def test_the_session_cookie_is_closed_to_scripts_and_to_other_sites(make_app):
    root = make_app()
    project = root.identities.create_project("owner", "Shared")
    invited = root.identities.create_member_invite("owner", project.id)
    web = Web(root)
    try:
        enrolled = post(web, "/api/v3/enroll", {"code": invited.code, "display_name": "Sam"})
        assert enrolled.status_code == 200, enrolled.text
        cookie = enrolled.headers["set-cookie"].lower()
        assert cookie.startswith("pa_session=")
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    finally:
        web.client.close()


def test_one_source_gets_ten_tries_across_enrollment_recovery_and_node_pairing(make_app):
    root = make_app()
    web = Web(root)
    try:
        first_ten = [
            post(web, "/api/v3/enroll", {"code": f"wrong-{i}"}).status_code for i in range(10)
        ]
        assert 429 not in first_ten
        assert post(web, "/api/v3/enroll", {"code": "wrong"}).status_code == 429
        assert post(web, "/api/v3/recover", {"code": "wrong"}).status_code == 429
        assert post(web, "/api/v3/nodes/pair", {"code": "wrong"}).status_code == 429
        root.clock.advance(61)
        assert post(web, "/api/v3/enroll", {"code": "wrong"}).status_code != 429
    finally:
        web.client.close()


def test_only_an_administrator_revokes_a_node(site, client_for):
    enrollment = member(site)
    collaborator, owner = client_for(enrollment.principal.id), client_for()
    node, node_id = pair_node(site, client_for)
    assert collaborator.post(f"/api/v3/nodes/{node_id}/revoke", json={}).status_code == 403
    assert node.post("/api/v3/nodes/claim", json={"capabilities": ALL}).status_code == 200
    assert owner.post(f"/api/v3/nodes/{node_id}/revoke", json={}).json() == {"ok": True}
    assert node.post("/api/v3/nodes/claim", json={"capabilities": ALL}).status_code == 401


# ---- the node ----


def running(site, client_for):
    owner = client_for()
    task = create(owner).json()["task"]
    approve(owner, task)
    node, node_id = pair_node(site, client_for)
    env = claim(node)
    return task, node, node_id, env


def grants_change(site, node_id):
    """The owner saves the node's grants again: the node has a new credential version."""
    site.app.nodes.set_grants(node_id, "owner", [OWNER_SPACE], ALL)


def test_a_node_does_not_take_work_that_somebody_other_than_its_owner_approved(site, client_for):
    jobs = site.app.jobs
    foreign = jobs.create_task(
        title="Approved elsewhere",
        command="printf foreign",
        actor_id="owner",
        request_id="foreign",
        approver_id="someone-else",
    )
    [job] = foreign["jobs"]
    jobs.approve_job(job["id"], actor_id="someone-else", plan_hash=job["plan_hash"], allowed=True)
    ours = create(client_for(), key="ours").json()["task"]
    node, _ = pair_node(site, client_for)
    assert claim(node) is None  # the older job is there, approved, and still left alone
    approve(client_for(), ours)
    assert claim(node)["task_id"] == ours["id"]


def test_a_node_that_stops_offering_what_a_running_task_needs_has_the_task_cancelled(
    site, client_for
):
    task, node, _, env = running(site, client_for)
    admitted(node, env)
    reduced = node.post("/api/v3/nodes/claim", json={"capabilities": ["network_none"]})
    assert reduced.status_code == 200 and reduced.json()["attempt"] is None
    assert site.app.jobs.get_task(task["id"])["state"] == "cancel_requested"
    status = report(node, env, "status").json()
    assert status["authorized"] is False and status["desired_state"] == "cancel"


def test_a_node_asking_for_work_after_its_grants_changed_has_the_task_cancelled(site, client_for):
    task, node, node_id, env = running(site, client_for)
    admitted(node, env)
    grants_change(site, node_id)
    assert claim(node) is None
    assert site.app.jobs.get_task(task["id"])["state"] == "cancel_requested"


def test_a_status_request_after_the_grants_changed_cancels_the_task_for_good(site, client_for):
    task, node, node_id, env = running(site, client_for)
    admitted(node, env)
    grants_change(site, node_id)
    assert report(node, env, "status").json()["authorized"] is False
    assert site.app.jobs.get_task(task["id"])["state"] == "cancel_requested"
    assert site.app.jobs.get_attempt(env["attempt_id"])["desired_state"] == "cancel"


def test_a_node_cannot_admit_work_after_its_grants_changed(site, client_for):
    _task, node, node_id, env = running(site, client_for)
    grants_change(site, node_id)
    refused = report(node, env, "admit", resource_id="container-late")
    assert refused.status_code == 403
    assert not site.app.jobs.get_attempt(env["attempt_id"])["resource_id"]


def test_a_result_reported_after_the_grants_changed_is_kept_but_never_sent(site, client_for):
    _task, node, node_id, env = running(site, client_for)
    admitted(node, env)
    grants_change(site, node_id)
    done = report(node, env, "complete", status="succeeded", exit_code=0, stdout="finished")
    assert done.status_code == 200, done.text
    assert site.app.jobs.get_job(env["job_id"])["state"] == "succeeded"
    assert site.app.db.scalar("SELECT COUNT(*) FROM outbox") == 0
    assert site.app.db.row("SELECT status FROM events WHERE source='execution'")["status"] == "done"
