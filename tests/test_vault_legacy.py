"""The old 资料 documents move into the knowledge base once, and stay in step after that."""

from __future__ import annotations

from pathlib import Path

from personal_agent.config import Config, VaultConfig
from personal_agent.memory.store import Actor
from personal_agent.vault.legacy import FLAG, bring_over, mirror
from web_fake import Web

OLD = "/memories/state/knowledge/"


def old_document(app, name, text):
    app.memory.write_text(OLD + name, text, actor=Actor.USER)


def not_moved_yet(app):
    """Building the app already ran the move (for nothing); undo that to run it by hand."""
    app.db.execute("DELETE FROM kv WHERE key=?", (FLAG,))


def test_old_documents_become_notes_once(make_app):
    app = make_app()
    old_document(app, "weekend.md", "# 周末\n去爬山")
    old_document(app, "budget.md", "预算 5000")
    not_moved_yet(app)
    made = bring_over(app.db, app.vault, app.clock)
    assert made == ["wiki/budget.md", "wiki/weekend.md"]
    assert app.vault.read_text("wiki/weekend.md") == "# 周末\n去爬山"
    assert app.memory.read_text(OLD + "weekend.md") == "# 周末\n去爬山"  # the original stays
    log = app.vault.read_text("wiki/log.md")
    assert "搬家" in log and "2 篇" in log
    again = bring_over(app.db, app.vault, app.clock)
    assert again == [] and app.db.kv_get(FLAG) == "2"


def test_nothing_to_bring_is_remembered_too(make_app):
    app = make_app()
    assert bring_over(app.db, app.vault, app.clock) == []
    assert app.db.kv_get(FLAG) == "0"
    old_document(app, "late.md", "later")  # saved after the move: not picked up by the move
    assert bring_over(app.db, app.vault, app.clock) == []
    assert app.vault.read_text("wiki/late.md") is None


def test_a_note_that_is_already_there_is_left_alone(make_app):
    app = make_app()
    app.vault.write("wiki/weekend.md", "我自己写的", actor=Actor.USER)
    old_document(app, "weekend.md", "旧的")
    old_document(app, "other.md", "别的")
    not_moved_yet(app)
    assert bring_over(app.db, app.vault, app.clock) == ["wiki/other.md"]
    assert app.vault.read_text("wiki/weekend.md") == "我自己写的"
    assert "1 篇没有搬" in app.vault.read_text("wiki/log.md")
    assert "weekend.md" in app.vault.read_text("wiki/log.md")


def test_a_document_too_large_for_a_note_is_reported_not_lost(make_app):
    config = Config(vault=VaultConfig(max_note_bytes=4096))
    app = make_app(config=config)
    old_document(app, "big.md", "字" * 3000)
    old_document(app, "ok.md", "小")
    not_moved_yet(app)
    assert bring_over(app.db, app.vault, app.clock) == ["wiki/ok.md"]
    assert app.memory.read_text(OLD + "big.md") == "字" * 3000
    assert "big.md" in app.vault.read_text("wiki/log.md")


def test_opening_a_home_with_old_documents_brings_them_over(make_app):
    first = make_app()
    old_document(first, "plan.md", "计划")
    first.db.execute("DELETE FROM kv WHERE key=?", (FLAG,))  # as if no move had happened yet
    reopened = make_app()
    assert reopened.vault.read_text("wiki/plan.md") == "计划"
    assert reopened.db.kv_get(FLAG) == "1"


def test_saving_the_old_way_is_saved_in_the_knowledge_base_too(make_app):
    app = make_app()
    web = Web(app)
    web.pair()
    try:
        first = web.client.post(
            "/api/v3/knowledge",
            headers=web.write,
            json={"path": "plan.md", "text": "第一版", "revision": 0},
        )
        assert first.status_code == 200
        assert app.vault.read_text("wiki/plan.md") == "第一版"
        second = web.client.post(
            "/api/v3/knowledge",
            headers=web.write,
            json={"path": "plan.md", "text": "第二版", "revision": 1},
        )
        assert second.status_code == 200
        assert app.vault.read_text("wiki/plan.md") == "第二版"
        assert app.vault.history("wiki/plan.md")[0]["before"] == "第一版"
    finally:
        web.client.close()


def test_a_save_still_works_when_the_knowledge_base_cannot_take_it(make_app):
    app = make_app(config=Config(vault=VaultConfig(max_note_bytes=4096)))
    web = Web(app)
    web.pair()
    try:
        saved = web.client.post(
            "/api/v3/knowledge",
            headers=web.write,
            json={"path": "long.md", "text": "字" * 3000, "revision": 0},
        )
        assert saved.status_code == 200
        assert app.memory.read_text(OLD + "long.md") == "字" * 3000
        assert app.vault.read_text("wiki/long.md") is None
    finally:
        web.client.close()


def test_mirroring_into_a_folder_that_is_not_there_makes_it(make_app, tmp_path: Path):
    app = make_app()
    mirror(app.vault, "fresh.md", "新的")
    assert (app.vault.root / "wiki" / "fresh.md").read_text() == "新的"
