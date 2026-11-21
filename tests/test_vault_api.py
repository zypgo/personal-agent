"""The knowledge base over HTTP: who sees which notes, and what the page can do with them."""

from __future__ import annotations

import pytest

from personal_agent.channels.web.openapi import document
from personal_agent.config import Config, VaultConfig
from personal_agent.memory.store import Actor
from web_fake import Web


@pytest.fixture
def group(make_app):
    root = make_app()
    directory = root.identities
    project = directory.create_project("owner", "Shared")
    invitation = directory.create_member_invite("owner", project.id)
    member = directory.redeem_member(invitation.code, label="Collaborator")
    shared = root.collaboration.get(project.id)
    owner_web, member_web = Web(root), Web(root)
    owner_web.pair()
    member_web.client.cookies.set(
        "pa_session", root.web_auth.create_session(member.principal.id).token
    )
    yield root, shared, member, owner_web, member_web
    owner_web.client.close()
    member_web.client.close()


def put(web, data, space=None):
    suffix = f"?space={space}" if space else ""
    return web.client.post("/api/v3/vault/note" + suffix, json=data, headers=web.write)


def test_the_owner_writes_reads_edits_and_finds_a_note(group):
    _, _, _, owner, _ = group
    created = put(owner, {"path": "wiki/张三.md", "mode": "create", "text": "# 张三\n负责预算。"})
    assert created.status_code == 200
    note = created.json()["note"]
    assert note["title"] == "张三" and note["origin"] == "owner" and not note["outside"]
    assert note["obsidian"].startswith("obsidian://open?path=")
    edited = put(
        owner,
        {"path": "wiki/张三.md", "mode": "replace", "rev": note["rev"], "text": "# 张三\n改了。"},
    )
    assert edited.status_code == 200 and "改了" in edited.json()["note"]["text"]
    got = owner.client.get("/api/v3/vault/note", params={"path": "wiki/张三.md"}).json()["note"]
    assert got["rev"] == edited.json()["note"]["rev"]
    found = owner.client.get("/api/v3/vault/notes", params={"q": "改了"}).json()
    assert [n["path"] for n in found["notes"]] == ["wiki/张三.md"]
    listing = owner.client.get("/api/v3/vault/notes", params={"folder": "wiki"}).json()
    assert [n["title"] for n in listing["notes"]] == ["张三"]
    root_listing = owner.client.get("/api/v3/vault/notes").json()
    assert root_listing["folders"] == [{"name": "wiki", "count": 1}]


def test_a_stale_revision_is_a_conflict_and_an_unknown_note_is_not_found(group):
    _, _, _, owner, _ = group
    first = put(owner, {"path": "wiki/a.md", "mode": "create", "text": "一"}).json()["note"]
    put(owner, {"path": "wiki/a.md", "mode": "replace", "rev": first["rev"], "text": "二"})
    stale = put(owner, {"path": "wiki/a.md", "mode": "replace", "rev": first["rev"], "text": "三"})
    assert stale.status_code == 409 and "又被改过" in stale.json()["error"]
    no_rev = put(owner, {"path": "wiki/a.md", "mode": "replace", "text": "四"})
    assert no_rev.status_code == 400 and "rev" in no_rev.json()["error"]
    missing = owner.client.get("/api/v3/vault/note", params={"path": "wiki/none.md"})
    assert missing.status_code == 404 and "没有找到" in missing.json()["error"]
    again = put(owner, {"path": "wiki/a.md", "mode": "create", "text": "五"})
    assert again.status_code == 400 and "已经有了" in again.json()["error"]


@pytest.mark.parametrize(
    "path", ["../x.md", "/etc/x.md", ".obsidian/app.json", "wiki/a:b.md", "a/b/c/d/e/f/g.md"]
)
def test_paths_that_leave_the_folder_are_refused(group, path):
    _, _, _, owner, _ = group
    refused = put(owner, {"path": path, "mode": "create", "text": "x"})
    assert refused.status_code == 400
    assert owner.client.get("/api/v3/vault/note", params={"path": path}).status_code == 400


def test_notes_the_product_writes_by_itself_cannot_be_edited_by_hand(group):
    root, _, _, owner, _ = group
    root.vault.put("wiki/index.md", "---\ngenerated: true\n---\n# 目录\n")
    refused = put(owner, {"path": "wiki/index.md", "mode": "append", "text": "我加的"})
    assert refused.status_code == 400 and "自动生成" in refused.json()["error"]
    page = owner.client.get("/api/v3/vault/note", params={"path": "wiki/index.md"}).json()["note"]
    assert page["generated"] is True


def test_what_came_from_outside_is_marked(group):
    root, _, _, owner, _ = group
    path, _ = root.vault.capture(
        folder="agents/Grok", title="报告", body="数据", key="k", front={}, day="2026-11-20"
    )
    page = owner.client.get("/api/v3/vault/note", params={"path": path}).json()["note"]
    assert page["outside"] is True and page["origin"] == "agent"
    found = owner.client.get("/api/v3/vault/notes", params={"q": "数据"}).json()["notes"]
    assert found[0]["outside"] is True


def test_the_owner_sees_private_notes_that_the_assistant_never_does(group):
    root, _, _, owner, _ = group
    root.vault.write("wiki/秘密.md", "---\nprivate: true\n---\n只有我看", actor=Actor.USER)
    assert owner.client.get("/api/v3/vault/notes", params={"q": "只有我看"}).json()["notes"]
    assert not root.vault.search("只有我看", actor=Actor.AGENT)


def test_every_change_can_be_looked_up_and_put_back(group):
    _, _, _, owner, _ = group
    first = put(owner, {"path": "wiki/a.md", "mode": "create", "text": "原来的"}).json()["note"]
    put(owner, {"path": "wiki/a.md", "mode": "replace", "rev": first["rev"], "text": "改过的"})
    versions = owner.client.get("/api/v3/vault/history", params={"path": "wiki/a.md"}).json()
    assert versions["path"] == "wiki/a.md"
    [latest, created] = versions["versions"]
    assert latest["text"] == "原来的" and latest["restorable"] and not created["restorable"]
    back = owner.client.post(
        "/api/v3/vault/restore", json={"version_id": latest["id"]}, headers=owner.write
    )
    assert back.json() == {"path": "wiki/a.md"}
    page = owner.client.get("/api/v3/vault/note", params={"path": "wiki/a.md"}).json()["note"]
    assert page["text"] == "原来的"
    refused = owner.client.post(
        "/api/v3/vault/restore", json={"version_id": created["id"]}, headers=owner.write
    )
    assert refused.status_code == 400


def test_status_counts_notes_and_points_out_what_needs_a_look(group):
    root, shared, _, owner, _ = group
    put(owner, {"path": "wiki/a.md", "mode": "create", "text": "见 [[不存在]]"})
    root.vault.capture(folder="agents/G", title="t", body="b", key="k", front={}, day="2026-11-20")
    status = owner.client.get("/api/v3/vault").json()
    assert status["enabled"] and status["exists"] and status["notes"] == 2
    assert status["areas"]["wiki"] == 1 and status["areas"]["raw"] == 1
    assert status["backlog"] == 1 and status["issues"]["broken_link"] == 1
    assert status["root"] == str(root.vault.root)  # the owner is told where it is
    issues = owner.client.get("/api/v3/vault/lint").json()
    assert {i["kind"] for i in issues["issues"]} >= {"broken_link", "orphan", "uncompiled"}
    assert issues["counts"]["broken_link"] == 1
    shared_status = owner.client.get(f"/api/v3/vault?space={shared.space_id}").json()
    assert shared_status["root"] is None and shared_status["notes"] == 0  # not the owner's folder


def test_each_project_has_its_own_notes_and_its_own_readers(group):
    root, shared, _member, owner, user = group
    put(owner, {"path": "wiki/我的.md", "mode": "create", "text": "个人"})
    put(owner, {"path": "wiki/共享.md", "mode": "create", "text": "项目"}, space=shared.space_id)
    shared_list = user.client.get(
        "/api/v3/vault/notes", params={"folder": "wiki", "space": shared.space_id}
    ).json()
    assert [n["title"] for n in shared_list["notes"]] == ["共享"]
    own_list = user.client.get("/api/v3/vault/notes", params={"folder": "wiki"}).json()
    assert own_list["notes"] == []  # the member's own space, not the owner's
    forbidden = user.client.get(f"/api/v3/vault/notes?space={root.space_id}")
    assert forbidden.status_code == 403
    assert root.vault.root != shared.vault.root


def test_a_viewer_can_read_but_not_write(group):
    root, shared, member, owner, user = group
    put(owner, {"path": "wiki/共享.md", "mode": "create", "text": "项目"}, space=shared.space_id)
    root.identities.set_role("owner", shared.space_id, member.principal.id, "viewer")
    seen = user.client.get(
        "/api/v3/vault/notes", params={"folder": "wiki", "space": shared.space_id}
    )
    assert seen.status_code == 200 and [n["title"] for n in seen.json()["notes"]] == ["共享"]
    denied = put(user, {"path": "wiki/新.md", "mode": "create", "text": "x"}, space=shared.space_id)
    assert denied.status_code == 403
    restore = user.client.post(
        f"/api/v3/vault/restore?space={shared.space_id}",
        json={"version_id": 1},
        headers=user.write,
    )
    assert restore.status_code == 403


def test_a_switched_off_knowledge_base_says_so(make_app):
    app = make_app(config=Config(vault=VaultConfig(enabled=False)))
    web = Web(app)
    web.pair()
    try:
        assert web.client.get("/api/v3/vault").json() == {"enabled": False}
        assert web.client.get("/api/v3/vault/notes").status_code == 404
        assert put(web, {"path": "wiki/a.md", "mode": "create", "text": "x"}).status_code == 404
    finally:
        web.client.close()


def test_the_public_schema_describes_the_routes():
    paths = document()["paths"]
    for path in ("/vault", "/vault/notes", "/vault/note", "/vault/history", "/vault/restore"):
        assert "/api/v3" + path in paths
    write = paths["/api/v3/vault/note"]["post"]
    assert write["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "WriteNote"
    )
    query = {p["name"] for p in paths["/api/v3/vault/notes"]["get"]["parameters"]}
    assert {"folder", "q", "tag", "kind", "status", "space"} <= query
