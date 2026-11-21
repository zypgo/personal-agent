"""`pa vault`: the knowledge base from the terminal. It looks, opens and takes back; it never writes
a note."""

from __future__ import annotations

import webbrowser

import pytest

from helpers import invoke
from personal_agent.config import Config, VaultConfig
from personal_agent.memory.store import Actor


def flat(output: str) -> str:
    return " ".join(output.split())


@pytest.fixture
def vault(wired):
    app = wired()
    app.vault.write(
        "wiki/张三.md", "---\ntags: [财务]\n---\n# 张三\n负责预算和报销。", actor=Actor.USER
    )
    app.vault.write(
        "wiki/预算.md", "# 预算\n负责人 [[张三]]，参考 [[没有这篇]]。", actor=Actor.AGENT
    )
    app.vault.capture(
        folder="agents/Grok",
        title="预算报告",
        body="超支 12%",
        key="k1",
        front={},
        day="2026-11-14",
    )
    return app


# ---- the overview -----------------------------------------------------------------------------
def test_the_overview_says_where_the_folder_is_and_what_is_in_it(vault):
    out = flat(invoke("vault").output)
    assert str(vault.vault.root) in out
    assert "notes 3" in out and "wiki 2" in out and "raw 1" in out
    assert "1 captured note" in out and "pa vault lint" in out and "Obsidian" in out


def test_a_folder_that_is_not_there_yet_says_so(wired):
    wired()
    out = flat(invoke("vault").output)
    assert "does not exist yet" in out


def test_a_switched_off_knowledge_base_says_how_to_turn_it_on(wired):
    wired(config=Config(vault=VaultConfig(enabled=False)))
    result = invoke("vault")
    assert result.exit_code == 2 and "enabled = true" in flat(result.output)


# ---- looking ----------------------------------------------------------------------------------
def test_search_lists_notes_by_their_words_and_tags(vault):
    out = invoke("vault", "search", "报销").output
    assert "wiki/张三.md" in out and "wiki/预算.md" not in out
    assert "wiki/张三.md" in invoke("vault", "search", "--tag", "财务").output
    assert "Nothing found" in invoke("vault", "search", "不存在的东西").output
    narrowed = invoke("vault", "search", "预算", "--folder", "raw").output
    assert "raw/agents/Grok/" in narrowed and "wiki/" not in narrowed


def test_lint_groups_what_is_worth_a_look(vault):
    out = invoke("vault", "lint").output
    assert "broken_link (1)" in out and "[[没有这篇]]" in out
    assert "uncompiled (1)" in out and "raw/agents/Grok/" in out


def test_a_tidy_knowledge_base_has_nothing_to_look_at(wired):
    app = wired()
    app.vault.write("wiki/一.md", "# 一\n见 [[二]]", actor=Actor.USER)
    app.vault.write("wiki/二.md", "# 二\n见 [[一]]", actor=Actor.USER)
    assert "Nothing to look at" in invoke("vault", "lint").output


# ---- opening ----------------------------------------------------------------------------------
def test_open_hands_a_note_to_obsidian_and_prints_the_address(vault, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda address: opened.append(address) or True)
    out = flat(invoke("vault", "open", "wiki/张三.md").output)
    assert opened and opened[0].startswith("obsidian://open?path=")
    assert "%E5%BC%A0%E4%B8%89.md" in opened[0] and opened[0] in out


def test_open_without_a_note_opens_the_folder_and_print_only_opens_nothing(vault, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda address: opened.append(address) or True)
    out = flat(invoke("vault", "open", "--print").output)
    assert "obsidian://open?path=" in out and opened == []
    invoke("vault", "open")
    assert len(opened) == 1 and opened[0].endswith("%2F") is False


def test_open_says_so_when_nothing_could_open_it(vault, monkeypatch):
    monkeypatch.setattr(webbrowser, "open", lambda address: False)
    assert "Is Obsidian installed" in flat(invoke("vault", "open").output)


def test_open_refuses_a_path_outside_the_folder(vault):
    result = invoke("vault", "open", "../../etc/passwd")
    assert result.exit_code == 2


# ---- the contents note ------------------------------------------------------------------------
def test_index_writes_the_contents_note_now(vault):
    assert "Wrote wiki/index.md" in invoke("vault", "index").output
    text = vault.vault.read("wiki/index.md", actor=Actor.USER).text
    assert "张三" in text and "还没整理的资料（1）" in text


def test_index_has_nothing_to_write_into_before_there_is_a_folder(wired):
    wired()
    assert invoke("vault", "index").exit_code == 1


# ---- taking back ------------------------------------------------------------------------------
def test_log_lists_changes_with_the_version_and_run_to_undo_them_by(vault):
    run = vault.runs.start(session_key="s", trigger="t", profile="full", model="m", event_ids=[])
    vault.vault.write("wiki/新的.md", "# 新的", actor=Actor.AGENT, run_id=run)
    out = flat(invoke("vault", "log").output)
    assert "wiki/新的.md" in out and run in out and "assistant" in out and "create" in out
    assert "pa vault restore" in out and "pa vault undo" in out


def test_log_with_no_changes_says_so(wired):
    wired()
    assert "No changes yet" in invoke("vault", "log").output


def test_restore_puts_one_note_back(vault):
    vault.vault.write("wiki/张三.md", "改了。", mode="append", actor=Actor.USER)
    version = vault.vault.history("wiki/张三.md")[0]["id"]
    assert "Put wiki/张三.md back" in flat(invoke("vault", "restore", str(version)).output)
    assert "改了" not in vault.vault.read("wiki/张三.md", actor=Actor.USER).text
    assert invoke("vault", "restore", "99999").exit_code == 1


def test_undo_takes_back_a_whole_run_and_leaves_what_was_edited_since(vault):
    run = vault.runs.start(session_key="s", trigger="t", profile="full", model="m", event_ids=[])
    vault.vault.write("wiki/甲.md", "# 甲", actor=Actor.AGENT, run_id=run)
    vault.vault.write("wiki/乙.md", "# 乙", actor=Actor.AGENT, run_id=run)
    vault.vault.write("wiki/乙.md", "我补了一句。", mode="append", actor=Actor.USER)
    out = flat(invoke("vault", "undo", run).output)
    assert "Took back wiki/甲.md" in out and "Left wiki/乙.md" in out
    assert "我补了一句" in vault.vault.read("wiki/乙.md", actor=Actor.USER).text


def test_undo_of_a_run_that_wrote_nothing_says_so(vault):
    assert invoke("vault", "undo", "no-such-run").exit_code == 1
