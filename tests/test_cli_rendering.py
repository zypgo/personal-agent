"""What the commands print, and how a command ends.

Everything the terminal shows goes through Rich, which reads `[like this]` as markup. Text that
came from somewhere else (the owner's own words, a model's answer, a tool's output, a config file,
the name of a timer) must be shown as written, not eaten, and never crash the command.
"""

from __future__ import annotations

import pytest
import typer
from agents.testing import ScriptedModel, assistant_message
from hypothesis import given
from hypothesis import strategies as st

from helpers import invoke
from personal_agent.commands import common
from personal_agent.config import ConfigError
from personal_agent.daemon import Engine
from personal_agent.presence import LOCK_NAME, EngineLock

# What would break (or silently vanish from) a line that was handed to Rich as markup.
AWKWARD = ["[/oops]", "[bold]kept[/bold]", "[red]", "[", "]", "[[x]]", "[/]", "\\[escaped]"]


# ---- the little helpers every command uses ----------------------------------------------
@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (0, "0s"),
        (59, "59s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86399, "23h"),
        (86400, "1d"),
        (86400 * 40, "40d"),
        (-5, "0s"),  # a clock that went backwards is not a negative span
        (0.9, "0s"),
    ],
)
def test_span_names_the_largest_unit_that_fits_at_least_once(seconds, shown):
    assert common.span(seconds) == shown


@given(st.floats(min_value=0, max_value=10**9))
def test_span_is_always_a_number_and_one_unit(seconds):
    text = common.span(seconds)
    assert text[:-1].isdigit() and text[-1] in "dhms"
    assert int(text[:-1]) >= (0 if text.endswith("s") else 1)


def test_ago_says_how_long_and_dashes_what_never_happened():
    assert common.ago(None, 1000.0) == "-"
    assert common.ago(700.0, 1000.0) == "5m ago"
    assert common.ago(1000.0, 1000.0) == "0s ago"


def test_fail_exits_with_the_code_and_prints_both_halves_as_plain_text(capsys):
    with pytest.raises(typer.Exit) as stop:
        common.fail("[/oops] broke", "Try [bold]this[/bold].")
    assert stop.value.exit_code == common.FAILED == 1
    out = capsys.readouterr().out
    assert "[/oops] broke" in out and "Try [bold]this[/bold]." in out


def test_a_usage_problem_exits_with_two():
    with pytest.raises(typer.Exit) as stop:
        common.fail("no home", code=common.USAGE)
    assert stop.value.exit_code == common.USAGE == 2


@pytest.mark.parametrize("text", AWKWARD)
def test_warn_and_echo_show_text_as_it_is(text, capsys):
    common.warn(text)
    common.echo(text)
    lines = capsys.readouterr().out.splitlines()
    assert lines == [text, text]


def test_a_table_has_the_columns_it_was_asked_for_and_no_frame():
    shown = common.table("id", "name")
    assert [c.header for c in shown.columns] == ["id", "name"]
    assert shown.box is None


# ---- what the owner, a model or a tool wrote reaches the terminal as written ------------
@pytest.mark.parametrize("text", AWKWARD)
def test_pa_run_shows_what_was_said_even_when_it_looks_like_markup(wired, text):
    app = wired(ScriptedModel([[assistant_message(f"you wrote {text}")]]))
    invoke("ask", f"see {text} here")
    run_id = app.runs.recent(1)[0].id

    detail = invoke("run", run_id)

    assert detail.exit_code == 0, detail.output
    assert f"see {text} here" in detail.output
    assert f"you wrote {text}" in detail.output


def test_pa_run_shows_the_error_of_a_failed_run_as_written(wired):
    app = wired(ScriptedModel([[assistant_message("hi")]]))
    invoke("ask", "hello")
    run_id = app.runs.recent(1)[0].id
    app.runs.finish(run_id, "failed", error="[/oops] the tool said [red]no[/red]")

    detail = invoke("run", run_id)

    assert detail.exit_code == 0, detail.output
    assert "[/oops] the tool said [red]no[/red]" in detail.output


def test_pa_outbox_previews_a_message_as_written(wired):
    app = wired()
    app.outbox.enqueue(
        session_key="main", channel="telegram", text="[/oops]\nand [bold]this[/bold]"
    )

    listing = invoke("outbox")

    assert listing.exit_code == 0, listing.output
    assert "[/oops] and [bold]this[/bold]" in listing.output


def test_pa_audit_shows_a_tool_name_and_detail_as_written(wired):
    app = wired()
    app.audit.record(
        run_id=None,
        session_key="main",
        call_id="call_1",
        tool="[/oops]",
        category="read",
        verdict="allow",
        layer="policy",
        reason=None,
        args={},
        detail="because [bold]reasons[/bold]",
        outcome="ok",
    )

    shown = invoke("audit")

    assert shown.exit_code == 0, shown.output
    assert "[/oops]" in shown.output and "because [bold]reasons[/bold]" in shown.output


def test_pa_timers_says_what_it_could_not_find_as_typed(wired):
    wired()

    gone = invoke("timers", "cancel", "[/nope]")

    assert gone.exit_code == 1
    assert "Could not cancel '[/nope]': no such timer." in gone.output


def test_pa_timers_add_reports_a_schedule_that_cannot_be_read_as_typed(wired):
    wired()

    refused = invoke("timers", "add", "n", "do it", "--cron", "[/x] * * *")

    assert refused.exit_code == 1
    assert "[/x]" in refused.output or "not" in refused.output.lower()
    assert "Traceback" not in refused.output


def test_a_broken_config_is_reported_as_written_and_is_a_usage_problem(monkeypatch):
    def broken() -> object:
        raise ConfigError("config.toml: [/web] is not a table, [bold]fix it[/bold]")

    monkeypatch.setattr(common, "build", broken)

    shown = invoke("status")

    assert shown.exit_code == 2
    assert "[/web] is not a table, [bold]fix it[/bold]" in shown.output


# ---- a command always leaves the home as it found it --------------------------------------
def test_the_agent_is_closed_when_a_command_ends_in_a_failure(make_app, monkeypatch):
    agent = make_app()
    monkeypatch.setattr(common, "build", lambda: agent)

    with pytest.raises(typer.Exit), common.opened():
        common.fail("nothing to see")

    with pytest.raises(Exception, match="closed"):
        agent.db.one("SELECT 1")


def test_the_agent_is_closed_when_a_command_ends_in_a_crash(make_app, monkeypatch):
    agent = make_app()
    monkeypatch.setattr(common, "build", lambda: agent)

    with pytest.raises(RuntimeError, match="boom"), common.opened():
        raise RuntimeError("boom")

    with pytest.raises(Exception, match="closed"):
        agent.db.one("SELECT 1")


def test_an_app_used_as_a_context_manager_is_closed_after_it(make_app):
    with make_app() as agent:
        assert agent.db.one("SELECT 1") is not None
    with pytest.raises(Exception, match="closed"):
        agent.db.one("SELECT 1")


def test_an_engine_used_as_a_context_manager_gives_the_lock_back(make_app):
    agent = make_app()
    lock = agent.paths.run_dir / LOCK_NAME

    with Engine.acquire(agent) as engine:
        assert engine.lock is not None and engine.lock.held
        assert EngineLock.is_taken(lock)

    assert not EngineLock.is_taken(lock)


def test_an_engine_that_could_not_get_the_lock_has_nothing_to_give_back(make_app):
    agent = make_app()
    holder = EngineLock(agent.paths.run_dir / LOCK_NAME)
    assert holder.acquire()
    try:
        with Engine.acquire(agent) as engine:
            assert engine.lock is None and engine.holder_pid is not None
        assert EngineLock.is_taken(agent.paths.run_dir / LOCK_NAME)  # still the holder's
    finally:
        holder.release()
