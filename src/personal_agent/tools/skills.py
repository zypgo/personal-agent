"""The `skill` tool: loads the full instructions of an installed skill (tier 2 of progressive disclosure).

The model sees the catalog of skills in its instructions; calling this tool is how it reads the one
that matches the task. Reading is harmless (the skills are the owner's own files), so the tool is a
plain read. What the skill then asks the model to *do* goes through the same policy as everything
else: a skill can neither grant a permission nor lift an approval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents import RunContextWrapper, function_tool

from personal_agent.harness.runctx import RunContext
from personal_agent.skills.store import Skill
from personal_agent.tools.base import FULL, SCHEDULED, Category, ToolDef, ToolRegistry

if TYPE_CHECKING:
    from personal_agent.app import App

DESCRIPTION = """\
Load the full instructions of one of the owner's installed skills. Your instructions list the \
skills and say what each is for. When a task matches one, call this with its name before you \
start, then follow what it returns. The skill's folder may hold scripts and reference files; the \
answer lists them and says how to reach them."""


def shell_dir(app: App, skill: Skill) -> str | None:
    """Where the skill's folder is inside the shell sandbox, or None if there is no shell."""
    if not app.config.tools.shell:
        return None
    sandbox = app.sandbox.sandbox
    if sandbox is None:
        return None
    if sandbox.name == "docker":
        return f"/skills/{skill.directory.name}"
    return str(skill.directory)


@function_tool(name_override="skill", description_override=DESCRIPTION)
async def skill_tool(ctx: RunContextWrapper[RunContext], name: str) -> str:
    app = ctx.context.app
    scan = app.skills.scan()
    wanted = name.strip().removeprefix("$")
    skill = scan.by_name().get(wanted)
    if skill is None or skill.disable_model_invocation:
        available = ", ".join(s.name for s in app.skills.offered(scan)) or "none"
        return f"Error: there is no skill named {wanted!r}. Installed skills: {available}."
    try:
        return app.skills.instructions(skill, shell_dir=shell_dir(app, skill))
    except Exception as exc:
        return f"Error: the skill {skill.name!r} could not be read ({type(exc).__name__})."


def _unavailable(app: App) -> str | None:
    if not app.skills.offered():
        return "no skills are installed"
    return None


def register_skills(registry: ToolRegistry) -> None:
    registry.register(
        ToolDef(
            name="skill",
            category=Category.READ,
            tool=skill_tool,
            # Not for runs woken by outside content, nor for memory maintenance: capability
            # narrowing, the same as for the file tools.
            profiles=frozenset({FULL, SCHEDULED}),
            detail=lambda a: str(a.get("name", "")),
            available=_unavailable,
        )
    )
