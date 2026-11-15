from __future__ import annotations

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from typer.testing import CliRunner

from personal_agent import cli

runner = CliRunner()


@pytest.fixture
def wired(make_app, monkeypatch):
    """Run CLI commands against the test app (and keep it open between commands)."""

    def wire(model: ScriptedModel | None = None):
        app = make_app(model)
        monkeypatch.setattr(app, "close", lambda: None)
        monkeypatch.setattr(cli, "_build", lambda: app)
        return app

    return wire


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_HOME", str(tmp_path / "fresh-home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return tmp_path / "fresh-home"


def invoke(*args: str, input: str | None = None):
    return runner.invoke(cli.app, list(args), input=input)


# ---- setup commands ---------------------------------------------------------------------
def test_version_flag():
    result = invoke("--version")
    assert result.exit_code == 0 and "personal-agent" in result.output


def test_init_creates_a_private_home_and_is_safe_to_repeat(home):
    first = invoke("init")
    assert first.exit_code == 0, first.output
    for name in (
        "config.toml",
        ".env",
        "state.db",
        "memory/preferences/instructions.md",
        "memory/state/MEMORY.md",
    ):
        assert (home / name).exists(), name
    assert (home / ".env").stat().st_mode & 0o077 == 0
    assert "OPENAI_API_KEY" in first.output

    (home / "memory/preferences/instructions.md").write_text("Reply in Chinese.", encoding="utf-8")
    second = invoke("init")
    assert second.exit_code == 0
    assert (
        home / "memory/preferences/instructions.md"
    ).read_text() == "Reply in Chinese."  # never overwritten


def test_doctor_fails_without_a_key_and_says_where_to_put_it(home):
    invoke("init")
    result = invoke("doctor")
    assert result.exit_code == 1
    assert "OpenAI key" in result.output and str(home / ".env") in result.output.replace("\n", "")


def test_doctor_passes_with_a_key(home, monkeypatch):
    invoke("init")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "FAIL" not in result.output


def test_doctor_never_prints_the_key(home, monkeypatch):
    invoke("init")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value-1234567890")
    assert "secret-value" not in invoke("doctor").output


def test_doctor_rejects_a_broken_config(home):
    invoke("init")
    (home / "config.toml").write_text("[model]\nmian = 'typo'\n", encoding="utf-8")
    result = invoke("doctor")
    assert result.exit_code == 1 and "config" in result.output


def test_commands_report_a_broken_config_instead_of_crashing(home):
    invoke("init")
    (home / "config.toml").write_text("[agent]\nmax_turns = 0\n", encoding="utf-8")
    result = invoke("status")
    assert result.exit_code == 2 and "max_turns" in result.output


# ---- talking ----------------------------------------------------------------------------
def test_ask_prints_the_answer(wired):
    app = wired(ScriptedModel([[assistant_message("The answer is **42**.")]]))
    result = invoke("ask", "meaning of life?")
    assert result.exit_code == 0, result.output
    assert "42" in result.output
    assert app.outbox.counts()["sent"] == 1


def test_ask_without_a_key_explains_instead_of_dumping_a_traceback(home):
    invoke("init")
    result = invoke("ask", "hello")
    assert result.exit_code == 1
    assert "pa doctor" in result.output and "Traceback" not in result.output


def test_ask_can_use_tools(wired):
    app = wired(
        ScriptedModel(
            [
                [
                    function_call(
                        "memory",
                        {"command": "create", "path": "/memories/state/x.md", "file_text": "hi"},
                        call_id="c1",
                    )
                ],
                [assistant_message("Saved.")],
            ]
        )
    )
    result = invoke("ask", "remember hi")
    assert result.exit_code == 0 and "Saved." in result.output
    assert app.memory.read_text("/memories/state/x.md") == "hi"


def test_a_conversation_continues_across_asks(wired):
    model = ScriptedModel([[assistant_message("Noted.")], [assistant_message("You said blue.")]])
    wired(model)
    invoke("ask", "my favourite colour is blue")
    invoke("ask", "what did I say?")
    seen = " ".join(str(i) for i in model.calls[1].input)
    assert "favourite colour is blue" in seen


def test_chat_loop_runs_commands_and_turns(wired, capsys):
    app = wired(ScriptedModel([[assistant_message("Hello there!")]]))
    lines = iter(["hi", "/help", "/status", "/bogus", "/exit", "never reached"])

    cli.chat_loop(app, "main", read_line=lambda _prompt: next(lines))

    output = capsys.readouterr().out
    assert "Hello there!" in output  # the agent's reply was printed
    assert "start a fresh conversation" in output  # /help
    assert "spend" in output  # /status
    assert "Unknown command /bogus" in output
    assert next(lines) == "never reached"  # /exit really stopped the loop
    assert app.outbox.counts()["sent"] == 1
    assert [r.status for r in app.runs.recent()] == ["completed"]


def test_chat_ends_on_end_of_input(wired):
    app = wired(ScriptedModel())

    def eof(_prompt):
        raise EOFError

    cli.chat_loop(app, "main", read_line=eof)  # returns instead of hanging or raising


def test_chat_new_starts_a_fresh_conversation(wired):
    model = ScriptedModel([[assistant_message("one")], [assistant_message("two")]])
    app = wired(model)
    lines = iter(["first topic", "/new", "second topic", "/exit"])

    cli.chat_loop(app, "main", read_line=lambda _prompt: next(lines))

    second_call = " ".join(str(i) for i in model.calls[1].input)
    assert "second topic" in second_call and "first topic" not in second_call


def test_chat_delivers_messages_that_arrived_while_away(wired, capsys):
    app = wired(ScriptedModel())
    app.outbox.enqueue(
        session_key="timer:brief", channel="cli", text="Your morning brief is ready."
    )

    cli.chat_loop(app, "main", read_line=lambda _prompt: "/exit")

    assert "morning brief" in capsys.readouterr().out
    assert app.outbox.counts()["sent"] == 1


# ---- inspecting -------------------------------------------------------------------------
def test_status_runs_and_run_details(wired):
    app = wired(ScriptedModel([[assistant_message("hi")]]))
    invoke("ask", "hello")

    status = invoke("status")
    assert status.exit_code == 0 and "spend" in status.output and "completed 1" in status.output

    listing = invoke("runs")
    run_id = app.runs.recent(1)[0].id
    assert run_id in listing.output

    detail = invoke("run", run_id)
    assert detail.exit_code == 0 and "hello" in detail.output
    assert invoke("run", "doesnotexist").exit_code == 1


def test_outbox_lists_and_dismisses_unresolved_messages(wired):
    app = wired()
    message_id = app.outbox.enqueue(session_key="main", channel="telegram", text="did this arrive?")
    app.outbox.claim_due()
    app.outbox.mark_maybe_sent(message_id)

    assert "did this arrive?" in invoke("outbox").output
    result = invoke("outbox", "dismiss", "--all")
    assert "Dismissed 1" in result.output
    assert app.outbox.unresolved() == []


def test_sessions_list_and_clear(wired):
    app = wired(ScriptedModel([[assistant_message("hi")]]))
    invoke("ask", "hello")
    assert "main" in invoke("sessions").output
    assert invoke("sessions", "clear", "main").exit_code == 0
    assert app.sessions.items("main") == []


def test_memory_view_history_and_restore(wired):
    app = wired()
    app.memory.create("/memories/state/n.md", "v1")
    app.memory.str_replace("/memories/state/n.md", "v1", "v2")

    assert "n.md" in invoke("memory", "view").output
    history = invoke("memory", "history", "/memories/state/n.md").output
    assert "update" in history and "create" in history
    version = app.memory.history("/memories/state/n.md")[0]["id"]
    assert "Restored" in invoke("memory", "restore", str(version)).output
    assert app.memory.read_text("/memories/state/n.md") == "v1"


def test_prefs_show_and_proposals(wired):
    app = wired()
    app.memory.write_text("/memories/preferences/instructions.md", "Reply in Chinese.")
    app.memory.execute(
        {
            "command": "insert",
            "path": "/memories/state/proposals.md",
            "insert_line": 0,
            "insert_text": "Allow Fridays off",
        }
    )

    assert "Reply in Chinese." in invoke("prefs", "show").output
    assert "Allow Fridays off" in invoke("prefs", "proposals").output


def test_prefs_edit_saves_through_the_versioned_store(wired, monkeypatch):
    app = wired()
    monkeypatch.setattr(cli.click, "edit", lambda text, **_: "Never call me before 9.\n")

    result = invoke("prefs", "edit")

    assert "Saved" in result.output
    assert (
        app.memory.read_text("/memories/preferences/instructions.md") == "Never call me before 9.\n"
    )
    assert app.memory.history("/memories/preferences/instructions.md")[0]["actor"] == "user"


def test_prefs_edit_with_no_change_does_nothing(wired, monkeypatch):
    wired()
    monkeypatch.setattr(cli.click, "edit", lambda text, **_: None)
    assert "No changes" in invoke("prefs", "edit").output
