"""The `shell` tool: run a command in the sandbox and report what happened.

The tool only exists when a sandbox does (see `SandboxProvider`): without one we do not offer a
shell at all rather than quietly running commands with all of your access.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from agents import RunContextWrapper, function_tool

from personal_agent.harness.runctx import RunContext
from personal_agent.security.floors import check_command, simple_commands
from personal_agent.security.sandbox import ExecResult
from personal_agent.security.sanitize import clipped
from personal_agent.tools.base import FULL, SCHEDULED, Category, ToolDef, ToolRegistry

if TYPE_CHECKING:
    from personal_agent.app import App

SHELL_DESCRIPTION = """\
Run a shell command (/bin/sh) inside a sandbox and get its output and exit code.
- The working folder is your workspace. You can read the system's programs and write only inside \
the workspace. There is no network (use web_fetch or http_request for that) and none of the \
owner's other files or secrets are visible.
- Every call starts fresh: `cd` and exported variables do not carry over, so use relative paths \
and write results to files. Background processes are stopped when the command ends.
- A command is stopped after timeout_s seconds (default 60) and long output is cut.
- For reading and editing files prefer read_file, write_file and edit_file.
- Common tools (git, python3, grep, sed, awk, tar, jq when installed) are available; check with \
`which` before relying on anything else."""

RULE_CHARS = 260
"""How much of a command an `allow` rule is shown: a longer one is marked `[compound]` as well,
so that no rule written for its first words fits it."""
COMPOUND = re.compile(r"[;&|<>`\n]|\$[({]")
"""Anything that can chain, redirect or substitute: such a command is not what a rule written for
its first word (`shell:git status*`) was meant to allow."""
EXTRA_SECONDS = 15.0
"""How long the tool wrapper waits beyond the command's own timeout, so the sandbox can clean up."""


def _command(args: dict[str, Any]) -> str:
    return str(args.get("command", "")).strip()


def command_detail(args: dict[str, Any]) -> str:
    """The command as `allow` rules and the audit log see it: marked `[compound]` when it chains
    or redirects, and never silently cut."""
    command = _command(args)
    compound = bool(COMPOUND.search(command))
    if len(command) > RULE_CHARS:
        compound = True
        command = clipped(command, RULE_CHARS)
    return ("[compound] " if compound else "") + command


def command_review(args: dict[str, Any]) -> str:
    """The command as the owner reads it before approving: all of it (the one place that decides
    how much is too much is `guard.summarize`), with the same `[compound]` mark."""
    command = _command(args)
    return ("[compound] " if COMPOUND.search(command) else "") + command


def command_views(args: dict[str, Any]) -> list[str]:
    """What `deny` and `always_ask` rules are tried on besides the detail: the whole command, and
    each command inside it. A rule like `shell:rm -rf *` then also stops `cd build && rm -rf .`
    and a command so long that its detail was cut before the part the rule is about."""
    command = _command(args)
    return [command, *simple_commands(command)]


def requested_timeout(app: App, args: dict[str, Any]) -> float:
    cfg = app.config.sandbox
    try:
        asked = float(args.get("timeout_s") or cfg.default_timeout_s)
    except (TypeError, ValueError):
        asked = cfg.default_timeout_s
    return max(1.0, min(asked, cfg.max_timeout_s))


def format_result(result: ExecResult, timeout_s: float) -> str:
    if result.error:
        return f"Error: the command could not be run: {result.error}"
    parts: list[str] = []
    if result.timed_out:
        parts.append(
            f"Error: the command did not finish within {timeout_s:.0f} seconds and was stopped."
        )
    else:
        parts.append(f"exit code: {result.exit_code}")
    if result.stdout:
        parts.append(f"--- stdout ---\n{result.stdout.rstrip()}")
    if result.stderr:
        parts.append(f"--- stderr ---\n{result.stderr.rstrip()}")
    if not result.stdout and not result.stderr:
        parts.append("(no output)")
    if result.truncated:
        parts.append("[output was cut: it was longer than the limit]")
    return "\n".join(parts)


@function_tool(name_override="shell", description_override=SHELL_DESCRIPTION)
async def shell_tool(
    ctx: RunContextWrapper[RunContext], command: str, timeout_s: float | None = None
) -> str:
    app = ctx.context.app
    sandbox = app.sandbox.sandbox
    if sandbox is None:
        return f"Error: the shell is not available: {app.sandbox.reason}"
    limit = requested_timeout(app, {"timeout_s": timeout_s})
    result = await sandbox.execute(command, timeout_s=limit)
    return format_result(result, limit)


def _validate(rc: RunContext, args: dict[str, Any]) -> str | None:
    if not str(args.get("command", "")).strip():
        return "give a command to run."
    return None


def _floor(rc: RunContext, args: dict[str, Any]) -> str | None:
    return check_command(str(args.get("command", "")), home=rc.app.paths.home)


def _available(app: App) -> str | None:
    return None if app.sandbox.sandbox is not None else f"no sandbox: {app.sandbox.reason}"


def register_shell(registry: ToolRegistry) -> None:
    registry.register(
        ToolDef(
            name="shell",
            category=Category.EXEC,
            tool=shell_tool,
            profiles=frozenset({FULL, SCHEDULED}),
            detail=command_detail,
            review=command_review,
            views=command_views,
            validate=_validate,
            floor=_floor,
            available=_available,
            timeout=lambda app, args: requested_timeout(app, args) + EXTRA_SECONDS,
        )
    )
