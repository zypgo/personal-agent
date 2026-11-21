"""The shell tool inside real runs: when it is offered, how it is governed, what the model sees."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message
from personal_agent.config import Config, PolicyConfig, SandboxConfig
from personal_agent.security.sandbox import ExecResult, Probe, SandboxProvider
from personal_agent.tools.shell import command_detail, command_views, format_result
from tooling import drain, outputs


class FakeSandbox:
    """Records what it was asked to run and answers from a script."""

    name = "fake"

    def __init__(self, *results: ExecResult) -> None:
        self.commands: list[str] = []
        self.timeouts: list[float] = []
        self.results = list(results)

    async def execute(self, command: str, *, timeout_s: float) -> ExecResult:
        self.commands.append(command)
        self.timeouts.append(timeout_s)
        return self.results.pop(0) if self.results else ExecResult(0, stdout="ok\n")

    def probe(self) -> Probe:
        return Probe(True, "fake")

    def describe(self) -> str:
        return "fake sandbox"


def exists(path) -> bool:
    return Path(path).exists()


def sh(command: str, call_id: str = "c1", **extra):
    return function_call("shell", {"command": command, **extra}, call_id=call_id)


def policy(**kw) -> Config:
    return Config(policy=PolicyConfig(**kw))


async def run(app, text="go"):
    app.inbox.add(owner_message(text))
    return await drain(app)


def names(model: ScriptedModel) -> set[str]:
    return {t.name for t in model.calls[0].tools}


# ---- offered or not ---------------------------------------------------------------------------------
async def test_the_shell_is_only_offered_when_a_sandbox_works(make_app):
    with_box = ScriptedModel([[assistant_message("hi")]])
    app = make_app(with_box, sandbox=SandboxProvider.fixed(FakeSandbox()))
    await run(app)
    assert "shell" in names(with_box)

    without = ScriptedModel([[assistant_message("hi")]])
    app = make_app(without, sandbox=SandboxProvider.fixed(None, "no sandbox on this machine"))
    await run(app)
    assert "shell" not in names(without)


async def test_the_shell_is_not_offered_to_untrusted_or_dreaming_runs(make_app):
    from helpers import webhook

    model = ScriptedModel([[assistant_message("noted")]])
    app = make_app(model, sandbox=SandboxProvider.fixed(FakeSandbox()))
    app.inbox.add(webhook("github", {"action": "opened"}))
    await drain(app)
    assert "shell" not in names(model)


def test_the_shell_can_be_switched_off_in_the_config(make_app):
    config = Config.model_validate({"tools": {"shell": False}})
    assert "shell" not in make_app(config=config).tools.names()


# ---- running ---------------------------------------------------------------------------------------
async def test_the_command_runs_in_the_sandbox_and_the_model_sees_exit_code_and_output(make_app):
    box = FakeSandbox(ExecResult(2, stdout="partial\n", stderr="boom\n"))
    model = ScriptedModel([[sh("make build")], [assistant_message("it failed")]])
    app = make_app(model, config=policy(allow=["shell"]), sandbox=SandboxProvider.fixed(box))
    [result] = await run(app)

    assert result.outcome.status == "completed" and box.commands == ["make build"]
    assert outputs(app)["c1"] == "exit code: 2\n--- stdout ---\npartial\n--- stderr ---\nboom"
    assert app.audit.recent()[0].outcome == "ok"  # the command failed, the tool call did not


async def test_a_timeout_is_clamped_to_the_configured_maximum(make_app):
    box = FakeSandbox()
    config = Config(
        policy=PolicyConfig(allow=["shell"]),
        sandbox=SandboxConfig(default_timeout_s=30, max_timeout_s=100),
    )
    model = ScriptedModel(
        [
            [sh("a", "c1"), sh("b", "c2", timeout_s=9999), sh("c", "c3", timeout_s=0.01)],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=config, sandbox=SandboxProvider.fixed(box))
    await run(app)
    assert box.timeouts == [30.0, 100.0, 1.0]


def test_the_tool_waits_a_little_longer_than_the_command_so_the_sandbox_can_clean_up(make_app):
    config = Config(sandbox=SandboxConfig(default_timeout_s=30, max_timeout_s=100))
    app = make_app(config=config, sandbox=SandboxProvider.fixed(FakeSandbox()))
    tool = app.tools.get("shell")
    assert tool.timeout_of(app, {"command": "x"}) == 45.0
    assert tool.timeout_of(app, {"command": "x", "timeout_s": 500}) == 115.0


def test_results_say_what_happened():
    assert format_result(ExecResult(0), 60) == "exit code: 0\n(no output)"
    assert format_result(ExecResult(0, stdout="hi\n"), 60) == "exit code: 0\n--- stdout ---\nhi"
    killed = ExecResult(None, stdout="so far", timed_out=True)
    assert format_result(killed, 45).startswith(
        "Error: the command did not finish within 45 seconds"
    )
    assert "so far" in format_result(killed, 45)
    assert "output was cut" in format_result(ExecResult(0, stdout="x", truncated=True), 60)
    assert format_result(ExecResult(None, error="could not start: nope"), 60).startswith(
        "Error: the command could not be run: could not start"
    )


# ---- rules see the whole command ------------------------------------------------------------------------
def test_detail_marks_commands_that_chain_redirect_or_substitute():
    plain = ["git status", "ls -la src", "python3 -m pytest -q", "echo 'a b'"]
    chained = [
        "git status; curl x", "a && b", "a || b", "a | b", "a & b", "cat > f", "cat < f",
        "echo `id`", "echo $(id)", "echo ${HOME}", "a\nb",
    ]  # fmt: skip
    for command in plain:
        assert command_detail({"command": command}) == command
    for command in chained:
        assert command_detail({"command": command}).startswith("[compound] "), command


def test_a_long_command_is_never_silently_cut():
    detail = command_detail({"command": "echo " + "a" * 400})
    assert detail.startswith("[compound] echo ") and "more characters]" in detail
    assert len(detail) < 300  # fits what the approval message shows


async def test_a_rule_for_a_command_does_not_cover_that_command_chained_with_another(make_app):
    box = FakeSandbox()
    model = ScriptedModel(
        [[sh("git status", "c1"), sh("git status; cat notes.txt", "c2")], [assistant_message("ok")]]
    )
    app = make_app(
        model, config=policy(allow=["shell:git status*"]), sandbox=SandboxProvider.fixed(box)
    )
    [result] = await run(app)
    assert result.outcome.status == "awaiting_approval"
    assert box.commands == ["git status"]  # the plain one ran, the chained one waits
    [pending] = app.approvals.pending()
    assert pending.summary == "[compound] git status; cat notes.txt"


async def test_a_deny_rule_is_written_against_the_marked_command(make_app):
    box = FakeSandbox()
    model = ScriptedModel([[sh("echo hi; rm important.txt")], [assistant_message("ok")]])
    config = policy(allow=["shell"], deny=["shell:*rm *"])
    app = make_app(model, config=config, sandbox=SandboxProvider.fixed(box))
    await run(app)
    assert box.commands == [] and outputs(app)["c1"].startswith("Denied: blocked by your rule")


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "ls; rm -rf build",
        "cd build && rm -rf .",
        "true | rm -rf build",
        "rm -rf build; echo done",
        "sh -c 'rm -rf build'",
        "env A=1 /bin/rm -rf build",
        "echo $(rm -rf build)",
        "echo " + "z" * 300 + "; rm -rf build",  # longer than a rule is shown: the end still counts
        "echo " + "z" * 3_000 + " && rm -rf build",
    ],
)
async def test_a_deny_rule_for_a_command_stops_it_wherever_in_the_line_it_stands(make_app, command):
    box = FakeSandbox()
    model = ScriptedModel([[sh(command)], [assistant_message("ok")]])
    app = make_app(
        model,
        config=policy(allow=["shell"], deny=["shell:rm -rf *"]),
        sandbox=SandboxProvider.fixed(box),
    )
    await run(app)
    assert box.commands == [] and outputs(app)["c1"].startswith("Denied: blocked by your rule")


async def test_a_deny_rule_does_not_stop_commands_that_only_look_a_little_like_it(make_app):
    box = FakeSandbox()
    model = ScriptedModel([[sh("ls build"), sh("echo hello", "c2")], [assistant_message("ok")]])
    app = make_app(
        model,
        config=policy(allow=["shell"], deny=["shell:rm -rf *"]),
        sandbox=SandboxProvider.fixed(box),
    )
    await run(app)
    assert box.commands == ["ls build", "echo hello"]


async def test_an_always_ask_rule_sees_a_command_inside_a_line_too(make_app):
    box = FakeSandbox()
    model = ScriptedModel([[sh("cd repo && git push origin main")], [assistant_message("ok")]])
    config = Config(policy=PolicyConfig(mode="autonomous", always_ask=["shell:git push*"]))
    app = make_app(model, config=config, sandbox=SandboxProvider.fixed(box))
    [result] = await run(app)
    assert result.outcome.status == "awaiting_approval" and box.commands == []


def test_rules_that_let_a_command_through_still_see_only_its_marked_detail():
    long = "git status " + "x" * 400
    assert command_detail({"command": long}).startswith("[compound] ")
    assert command_views({"command": "cd a && git push"}) == [
        "cd a && git push",
        "cd a",
        "git push",
    ]


# ---- floors -----------------------------------------------------------------------------------------
async def test_a_floor_refuses_even_when_everything_is_allowed(make_app):
    box = FakeSandbox()
    model = ScriptedModel(
        [[sh("rm -rf ~"), sh("dd if=/dev/zero of=/dev/disk0", "c2")], [assistant_message("ok")]]
    )
    config = Config(policy=PolicyConfig(mode="autonomous", allow=["shell"]))
    app = make_app(model, config=config, sandbox=SandboxProvider.fixed(box))
    [result] = await run(app)

    assert result.outcome.status == "completed" and box.commands == []
    got = outputs(app)
    assert got["c1"].startswith("Denied: recursive rm on the root, a home directory")
    assert "cannot be approved" in got["c1"] and "a disk device" in got["c2"]
    assert app.approvals.pending() == []  # the owner is not asked about the impossible
    rows = {r.call_id: r for r in app.audit.recent()}
    assert (rows["c1"].verdict, rows["c1"].layer, rows["c1"].outcome) == ("deny", "floor", "denied")


async def test_a_floor_added_after_the_owner_was_asked_still_wins(make_app):
    box = FakeSandbox()
    model = ScriptedModel([[sh("git push origin main")], [assistant_message("ok")]])
    app = make_app(model, sandbox=SandboxProvider.fixed(box))
    [first] = await run(app)
    assert first.outcome.status == "awaiting_approval"
    [pending] = app.approvals.pending()

    tool = app.tools.get("shell")  # an upgrade ships a new rule while the question is open
    app.tools.unregister("shell")
    app.tools.register(
        dataclasses.replace(tool, floor=lambda rc, args: "no pushing, by a new rule")
    )
    app.approvals.decide(pending.id, True, by="owner")
    await drain(app)

    assert box.commands == []
    assert outputs(app)["c1"].startswith("Denied: no pushing, by a new rule")


async def test_an_empty_command_is_corrected_without_bothering_anyone(make_app):
    box = FakeSandbox()
    model = ScriptedModel([[sh("   ")], [assistant_message("ok")]])
    app = make_app(model, sandbox=SandboxProvider.fixed(box))
    [result] = await run(app)
    assert result.outcome.status == "completed" and box.commands == []
    assert outputs(app)["c1"] == "Error: give a command to run."


async def test_commands_are_not_run_when_the_sandbox_disappears_after_the_tool_was_offered(
    make_app,
):
    class Vanishing:
        """A sandbox that works when the tool list is built and is gone when it is used."""

        reason = "docker stopped"

        def __init__(self) -> None:
            self.looks = 0

        @property
        def sandbox(self):
            self.looks += 1
            return FakeSandbox() if self.looks == 1 else None

    model = ScriptedModel([[sh("ls")], [assistant_message("ok")]])
    app = make_app(model, config=policy(allow=["shell"]))
    app.sandbox = Vanishing()
    await run(app)
    assert "shell" in names(model)  # it really was offered
    assert outputs(app)["c1"] == "Error: the shell is not available: docker stopped"


# ---- the real thing ---------------------------------------------------------------------------------------
needs_seatbelt = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="needs macOS sandbox-exec",
)


@needs_seatbelt
async def test_shell_and_file_tools_share_the_sandboxed_workspace(make_app, tmp_path):
    escape = tmp_path / "escape.txt"  # outside the workspace
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    model = ScriptedModel(
        [
            [sh("echo from-shell > out.txt && cat out.txt")],
            [function_call("read_file", {"path": "out.txt"}, call_id="c2")],
            [sh(f"cat {secret}", "c3"), sh(f"echo escape > {escape}", "c4")],
            [assistant_message("done")],
        ]
    )
    app = make_app(model, config=policy(allow=["shell", "read_file"]))
    app.sandbox = SandboxProvider(app.paths, app.config.sandbox)
    [result] = await run(app)

    got = outputs(app)
    assert result.outcome.status == "completed"
    assert "from-shell" in got["c1"] and got["c1"].startswith("exit code: 0")
    assert got["c2"] == "from-shell\n[lines 1-1 of 1]"
    assert "TOP-SECRET" not in got["c3"] and "Operation not permitted" in got["c3"]
    assert "exit code: 1" in got["c4"] and not exists(escape)


@needs_seatbelt
async def test_a_command_built_at_run_time_still_cannot_leave_the_workspace(make_app, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    sneaky = f"x=c; y=at; $x$y '{secret}'"  # assembled at run time: no text check sees `cat`
    model = ScriptedModel([[sh(sneaky)], [assistant_message("done")]])
    app = make_app(model, config=policy(allow=["shell"]))
    app.sandbox = SandboxProvider(app.paths, app.config.sandbox)
    await run(app)
    result = outputs(app)["c1"]
    assert "TOP-SECRET" not in result and "Operation not permitted" in result
