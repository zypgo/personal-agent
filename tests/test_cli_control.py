"""The owner's controls from the terminal: approvals, timers, audit, budget, the service."""

from __future__ import annotations

import os

from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import invoke
from personal_agent import cli
from personal_agent.config import BudgetConfig, Config, PolicyConfig
from personal_agent.core.approvals import DENIED_MESSAGE
from personal_agent.daemon import HEARTBEAT_KEY, LOCK_NAME, EngineLock, daemon_state
from test_budget import spend
from tooling import Spy, install


def push(call_id: str = "c1", command: str = "git push origin main"):
    return function_call("shell", {"command": command}, call_id=call_id)


def suspended(wired, script):
    """An app where the owner asked for something risky and the run is waiting for a yes."""
    app = wired(ScriptedModel(script))
    spy = install(app, Spy())
    result = invoke("ask", "please push my branch")
    assert result.exit_code == 0, result.output
    assert app.approvals.pending(), "the run should be waiting for approval"
    return app, spy, result


# ---- approvals --------------------------------------------------------------------------
def test_ask_shows_the_approval_request_and_the_work_waits(wired):
    _app, spy, result = suspended(wired, [[push()]])
    assert "approval" in result.output and "git push origin main" in result.output
    assert "/approve 1" in result.output
    assert spy.ran() == []


def test_pa_approve_lets_the_paused_run_finish_in_this_terminal(wired):
    app, spy, _ = suspended(wired, [[push()], [assistant_message("Pushed it.")]])

    waiting = invoke("approvals")
    assert "git push origin main" in waiting.output and "#1" in waiting.output

    result = invoke("approve")  # only one is waiting, so no number is needed
    assert result.exit_code == 0, result.output
    assert "Approved #1" in result.output and "Pushed it." in result.output
    assert spy.ran("shell") == ["git push origin main"]
    assert app.approvals.pending() == []


def test_pa_deny_passes_the_reason_on_to_the_agent(wired):
    model = ScriptedModel([[push()], [assistant_message("Understood.")]])
    app = wired(model)
    spy = install(app, Spy())
    invoke("ask", "push")

    result = invoke("deny", "1", "--note", "not on main")

    assert "Denied #1" in result.output and "Understood." in result.output
    assert spy.ran() == []
    seen = str(model.calls[1].input)
    assert DENIED_MESSAGE in seen and "not on main" in seen


def test_with_several_waiting_the_owner_must_say_which(wired):
    app, spy, _ = suspended(
        wired, [[push("c1", "git push a"), push("c2", "git push b")], [assistant_message("Done.")]]
    )
    assert len(app.approvals.pending()) == 2

    vague = invoke("approve")
    assert "Several actions are waiting" in vague.output and len(app.approvals.pending()) == 2

    one = invoke("approve", "1")
    assert "Approved #1" in one.output and [a.id for a in app.approvals.pending()] == [2]
    assert spy.ran() == []  # the run continues only after every request is answered

    rest = invoke("approve", "all")
    assert "Approved #2" in rest.output and "Done." in rest.output
    assert sorted(spy.ran("shell")) == ["git push a", "git push b"]


def test_answering_twice_or_a_wrong_number_changes_nothing(wired):
    app, spy, _ = suspended(wired, [[push()], [assistant_message("ok")]])
    assert "There is no approval #99" in invoke("approve", "99").output
    assert app.approvals.pending()  # still waiting

    invoke("approve", "1")
    again = invoke("approve", "1")
    assert "Approval #1 is already approved" in again.output
    assert spy.ran("shell") == ["git push origin main"]  # ran once


def test_pa_approvals_all_includes_answered_requests(wired):
    suspended(wired, [[push()], [assistant_message("ok")]])
    invoke("approve")
    assert "Nothing is waiting" in invoke("approvals").output
    history = invoke("approvals", "--all").output
    assert "approved" in history and "git push origin main" in history


def test_chat_can_approve_with_slash_commands(wired, capsys):
    model = ScriptedModel([[push()], [assistant_message("All pushed.")]])
    app = wired(model)
    spy = install(app, Spy())
    lines = iter(["push my branch", "/approvals", "/approve", "/exit"])

    cli.chat_loop(app, "main", read_line=lambda _prompt: next(lines))

    output = capsys.readouterr().out
    assert "git push origin main" in output  # the request, then the table
    assert "Approved #1" in output and "All pushed." in output
    assert spy.ran("shell") == ["git push origin main"]


def test_chat_deny_takes_a_reason_after_the_number(wired, capsys):
    model = ScriptedModel([[push()], [assistant_message("Skipped.")]])
    app = wired(model)
    spy = install(app, Spy())
    lines = iter(["push", "/deny 1 not today, I am travelling", "/exit"])

    cli.chat_loop(app, "main", read_line=lambda _prompt: next(lines))

    assert spy.ran() == []
    assert "not today, I am travelling" in str(model.calls[1].input)
    assert "Skipped." in capsys.readouterr().out


def test_chat_reminds_about_requests_that_are_still_waiting(wired, capsys):
    app, _, _ = suspended(wired, [[push()]])
    capsys.readouterr()

    cli.chat_loop(app, "main", read_line=lambda _prompt: "/exit")

    assert "1 action(s) are waiting" in capsys.readouterr().out


def test_a_new_message_closes_old_requests_instead_of_leaving_them_to_trigger_later(wired):
    app, spy, _ = suspended(wired, [[push()], [assistant_message("Never mind, then.")]])
    invoke("ask", "actually, forget it")
    assert app.approvals.pending() == []
    assert "already superseded" in invoke("approve", "1").output
    assert spy.ran() == []


# ---- timers -----------------------------------------------------------------------------
def test_timers_can_be_added_listed_paused_resumed_and_cancelled(wired):
    app = wired()

    added = invoke("timers", "add", "stretch", "Remind me to stretch.", "--in", "45m")
    assert added.exit_code == 0 and "Scheduled 'stretch'" in added.output
    assert "pa serve" in added.output  # nothing runs it yet
    brief = invoke("timers", "add", "brief", "Give me my morning brief.", "--cron", "0 8 * * 1-5")
    assert brief.exit_code == 0, brief.output
    assert app.timers.get("brief").created_by == "owner"

    listing = invoke("timers").output
    assert "stretch" in listing and "once" in listing and "0 8 * * 1-5" in listing

    assert "Paused 'stretch'" in invoke("timers", "pause", "stretch").output
    assert app.timers.get("stretch").status == "paused"
    assert "Resumed 'stretch'" in invoke("timers", "resume", "stretch").output
    assert "Cancelled 'stretch'" in invoke("timers", "cancel", "stretch").output
    assert "stretch" not in invoke("timers").output
    assert "stretch" in invoke("timers", "--all").output


def test_resuming_a_schedule_that_can_no_longer_be_read_says_so_instead_of_crashing(wired):
    app = wired()
    invoke("timers", "add", "brief", "Morning brief.", "--cron", "0 8 * * 1-5")
    invoke("timers", "pause", "brief")
    app.db.execute("UPDATE timers SET cron = 'not a cron' WHERE name = 'brief'")

    result = invoke("timers", "resume", "brief")

    assert result.exit_code == 1
    assert "cannot be read" in result.output and "Traceback" not in result.output
    assert app.timers.get("brief").status == "paused"


def test_timers_at_takes_a_local_time(wired):
    app = wired()
    result = invoke("timers", "add", "dentist", "Call the dentist.", "--at", "2023-11-15 09:30")
    assert result.exit_code == 0, result.output
    assert app.timers.get("dentist").due_at == 1_700_040_600  # 2023-11-15 09:30 UTC


def test_timers_add_rejects_unclear_or_invalid_input(wired):
    app = wired()
    neither = invoke("timers", "add", "x", "do it")
    assert neither.exit_code == 2 and "exactly one" in neither.output
    both = invoke("timers", "add", "x", "do it", "--in", "1h", "--cron", "0 8 * * *")
    assert both.exit_code == 2
    assert invoke("timers", "add", "x", "do it", "--in", "soon").exit_code == 2
    assert invoke("timers", "add", "x", "do it", "--at", "tomorrow").exit_code == 2
    too_often = invoke("timers", "add", "x", "do it", "--cron", "* * * * *")
    assert too_often.exit_code == 1 and "Traceback" not in too_often.output
    assert app.timers.list() == []


def test_timers_actions_on_unknown_timers_fail_politely(wired):
    wired()
    result = invoke("timers", "cancel", "nope")
    assert result.exit_code == 1 and "no such timer" in result.output


# ---- audit and budget -------------------------------------------------------------------
def test_audit_shows_what_ran_and_how_the_policy_decided(wired):
    app = wired(
        ScriptedModel([[push("c1", "git status")], [assistant_message("clean")]]),
        config=Config(policy=PolicyConfig(allow=["shell:git status*"])),
    )
    spy = install(app, Spy())
    invoke("ask", "status?")
    assert spy.ran("shell") == ["git status"]

    shown = invoke("audit").output
    assert "shell" in shown and "allow" in shown and "ok" in shown and "git status" in shown
    assert "shell" in invoke("audit", "--tool", "shell").output
    assert "Nothing recorded" in invoke("audit", "--tool", "nothing_like_this").output
    assert "Nothing recorded" in invoke("audit", "--verdict", "deny").output


def test_budget_shows_the_limits_and_extending_lets_paused_work_continue(wired):
    app = wired(config=Config(budget=BudgetConfig(daily_usd=1.0)))
    shown = invoke("budget").output
    assert "$1.00" in shown and "midnight" in shown

    result = invoke("budget", "extend", "2")
    assert result.exit_code == 0 and "$3.00" in result.output
    assert app.budget.daily_limit() == 3.0
    assert "including $2.00 you added" in invoke("budget").output
    assert invoke("budget", "extend", "0").exit_code == 2


# ---- status and the service -------------------------------------------------------------
def test_status_says_when_nothing_is_running_and_what_is_waiting(wired):
    app, _, _ = suspended(wired, [[push()]])
    app.timers.add_followup(name="stretch", prompt="Stretch!", due_at=app.clock.now() + 600)

    output = invoke("status").output

    assert "not running" in output and "pa serve" in output
    assert "timers  1 active" in output and "next in 10m" in output
    assert "1 waiting for your answer" in output
    assert "of $5.00" in output


def test_status_recognises_the_background_service_by_its_heartbeat(wired):
    app = wired()
    lock = EngineLock(app.paths.run_dir / LOCK_NAME)
    assert lock.acquire()
    try:
        assert daemon_state(app).kind == "terminal"  # holds the lock but sends no heartbeat
        app.db.kv_set(HEARTBEAT_KEY, f"{os.getpid()} {app.clock.now() - 5}")
        state = daemon_state(app)
        assert (state.kind, state.pid) == ("daemon", os.getpid())
        assert "running in the background" in invoke("status").output

        app.db.kv_set(HEARTBEAT_KEY, f"{os.getpid()} {app.clock.now() - 3600}")
        assert daemon_state(app).kind == "terminal"  # an old heartbeat proves nothing
    finally:
        lock.release()
    assert daemon_state(app).kind == "stopped"


def test_status_flags_a_budget_pause(wired):
    app = wired(config=Config(budget=BudgetConfig(daily_usd=0.01)))
    spend(app.runs, "main", 0.02)
    output = invoke("status").output
    assert "Paused for the budget" in output and "pa budget extend" in output


def test_serve_refuses_to_start_a_second_copy(wired):
    app = wired()
    lock = EngineLock(app.paths.run_dir / LOCK_NAME)
    assert lock.acquire()
    try:
        result = invoke("serve")
    finally:
        lock.release()
    assert result.exit_code == 1 and "already running" in result.output.replace("\n", " ")
