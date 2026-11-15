"""Skills inside real runs: what the model is told, what it can load, who can ask for one by name,
and that a skill never widens what the agent is allowed to do."""

from __future__ import annotations

from pathlib import Path

from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message, text_of, webhook
from personal_agent.config import Config, PolicyConfig, SkillsConfig
from personal_agent.core.events import Kind, NewEvent, Trust
from personal_agent.security.sandbox import ExecResult, Probe, SandboxProvider
from personal_agent.tools.skills import shell_dir
from tooling import drain, outputs

SKILL = """\
---
name: pdf-tools
description: Extract text from PDFs. Use for any question about a PDF file.
---
# PDF tools

Run the extractor, then summarise.
"""


def add_skill(app, name: str = "pdf-tools", text: str | None = None, **files: str) -> Path:
    folder = app.paths.skills_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text or SKILL.replace("pdf-tools", name), encoding="utf-8")
    for relative, content in files.items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(content, encoding="utf-8")
    return folder


def load(name: str, call_id: str = "c1"):
    return function_call("skill", {"name": name}, call_id=call_id)


async def run(app, text="go"):
    app.inbox.add(owner_message(text))
    return await drain(app)


def tool_names(model: ScriptedModel, call: int = 0) -> set[str]:
    return {t.name for t in model.calls[call].tools}


class Box:
    """A sandbox stand-in that only has a name."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.commands: list[str] = []

    async def execute(self, command: str, *, timeout_s: float) -> ExecResult:
        self.commands.append(command)
        return ExecResult(0, stdout="ok\n")

    def probe(self) -> Probe:
        return Probe(True, self.name)

    def describe(self) -> str:
        return self.name


# ---- what the model is told ----------------------------------------------------------------------------
async def test_with_no_skills_installed_the_model_hears_nothing_about_them(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model)
    await run(app)
    assert "skill" not in tool_names(model)
    assert "available_skills" not in model.calls[0].system_instructions


async def test_installed_skills_are_listed_by_name_and_purpose_only(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model)
    add_skill(app)
    await run(app)

    assert "skill" in tool_names(model)
    seen = model.calls[0].system_instructions
    assert "pdf-tools: Extract text from PDFs." in seen
    assert "Run the extractor" not in seen  # the body is loaded only when needed


async def test_skills_can_be_switched_off(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=Config(skills=SkillsConfig(enabled=False)))
    add_skill(app)
    await run(app)
    assert "skill" not in tool_names(model)
    assert "available_skills" not in model.calls[0].system_instructions


async def test_a_skill_added_while_the_agent_runs_shows_up_on_the_next_run(make_app):
    model = ScriptedModel([[assistant_message("one")], [assistant_message("two")]])
    app = make_app(model)
    await run(app, "first")
    add_skill(app)
    await run(app, "second")
    assert "pdf-tools" not in model.calls[0].system_instructions
    assert "pdf-tools" in model.calls[1].system_instructions


# ---- loading a skill -------------------------------------------------------------------------------------
async def test_loading_a_skill_hands_the_model_its_instructions(make_app):
    model = ScriptedModel([[load("pdf-tools")], [assistant_message("done")]])
    app = make_app(model)
    add_skill(app, **{"scripts/extract.py": "print(1)"})
    [result] = await run(app)

    out = outputs(app)["c1"]
    assert result.outcome.status == "completed"
    assert out.startswith('<skill_content name="pdf-tools">') and out.endswith("</skill_content>")
    assert "Run the extractor, then summarise." in out
    assert "<file>scripts/extract.py</file>" in out
    [row] = app.audit.recent()
    assert (row.tool, row.category, row.verdict, row.outcome) == ("skill", "read", "allow", "ok")
    assert row.detail == "pdf-tools"


async def test_loading_a_skill_is_not_reading_outside_content(make_app):
    """The skills are the owner's own files, so loading one does not restrict what the run may do next."""
    model = ScriptedModel([[load("pdf-tools")], [assistant_message("done")]])
    app = make_app(model)
    add_skill(app)
    [result] = await run(app)
    assert not app.runs.get(result.outcome.run_id).tainted


async def test_the_name_may_be_given_with_a_dollar_sign(make_app):
    model = ScriptedModel([[load("$pdf-tools")], [assistant_message("done")]])
    app = make_app(model)
    add_skill(app)
    await run(app)
    assert outputs(app)["c1"].startswith('<skill_content name="pdf-tools">')


async def test_an_unknown_name_answers_with_the_names_that_exist(make_app):
    model = ScriptedModel([[load("nope")], [assistant_message("ok")]])
    app = make_app(model)
    add_skill(app)
    add_skill(app, "research")
    await run(app)
    out = outputs(app)["c1"]
    assert out.startswith("Error: there is no skill named 'nope'")
    assert "pdf-tools, research" in out
    assert app.audit.recent()[0].outcome == "error"


async def test_a_skill_hidden_from_the_model_cannot_be_loaded_by_it(make_app):
    hidden = SKILL.replace("name: pdf-tools", "name: owner-only\ndisable-model-invocation: true")
    model = ScriptedModel([[load("owner-only")], [assistant_message("ok")]])
    app = make_app(model)
    add_skill(app, "owner-only", hidden)
    add_skill(app)
    await run(app)
    assert "owner-only" not in model.calls[0].system_instructions
    out = outputs(app)["c1"]
    assert out.startswith("Error:") and "Installed skills: pdf-tools." in out


async def test_a_damaged_skill_is_skipped_and_the_others_still_work(make_app):
    model = ScriptedModel([[load("pdf-tools")], [assistant_message("ok")]])
    app = make_app(model)
    add_skill(app)
    add_skill(app, "broken", "---\nname: broken\n---\nno description")
    await run(app)
    assert outputs(app)["c1"].startswith("<skill_content")
    assert "broken" not in model.calls[0].system_instructions


async def test_runs_woken_by_outside_content_get_no_skills(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model)
    add_skill(app)
    app.inbox.add(webhook("github", {"action": "opened"}))
    await drain(app)
    assert "skill" not in tool_names(model)
    assert "available_skills" not in model.calls[0].system_instructions


async def test_scheduled_runs_get_skills_too(make_app):
    from helpers import timer

    model = ScriptedModel([[load("pdf-tools")], [assistant_message("done")]])
    app = make_app(model)
    add_skill(app)
    app.inbox.add(timer("morning", "summarise the new PDFs"))
    await drain(app)
    assert "pdf-tools" in model.calls[0].system_instructions
    assert outputs(app, "timer:morning")["c1"].startswith("<skill_content")


# ---- a skill grants nothing ---------------------------------------------------------------------------
async def test_a_skill_cannot_grant_a_permission_it_claims(make_app):
    """`allowed-tools: shell` in a skill is ignored: the shell still asks the owner."""
    box = Box("fake")
    text = SKILL.replace("description:", "allowed-tools: shell\ndescription:")
    shell = function_call("shell", {"command": "rm -rf build"}, call_id="c2")
    model = ScriptedModel([[load("pdf-tools")], [shell], [assistant_message("done")]])
    app = make_app(model, sandbox=SandboxProvider.fixed(box))
    add_skill(app, text=text)
    [result] = await run(app)
    assert result.outcome.status == "awaiting_approval"
    assert box.commands == []
    assert [p.summary for p in app.approvals.pending()] == ["rm -rf build"]


async def test_a_skill_cannot_get_around_a_deny_rule(make_app):
    box = Box("fake")
    shell = function_call("shell", {"command": "git push"}, call_id="c2")
    model = ScriptedModel([[load("pdf-tools")], [shell], [assistant_message("ok")]])
    app = make_app(
        model,
        config=Config(policy=PolicyConfig(deny=["shell:git push*"])),
        sandbox=SandboxProvider.fixed(box),
    )
    add_skill(app)
    await run(app)
    assert box.commands == []
    assert outputs(app)["c2"].startswith("Denied: blocked by your rule")


# ---- asking for a skill by name ---------------------------------------------------------------------------
async def test_the_owner_can_ask_for_a_skill_by_name(make_app):
    model = ScriptedModel([[assistant_message("on it")]])
    app = make_app(model)
    add_skill(app)
    [result] = await run(app, "please use $pdf-tools on the invoice")

    first = text_of(model.calls[0].input[0])
    assert first.index("please use $pdf-tools on the invoice") < first.index("[The owner asked you")
    assert "[The owner asked you to use the skill $pdf-tools." in first
    assert '<skill_content name="pdf-tools">' in first and "Run the extractor" in first
    users = [i for i in app.sessions.items("main") if i.get("role") == "user"]
    assert len(users) == 1  # one message, so the history never has the skill without its request
    assert result.outcome.status == "completed"


async def test_asking_for_a_skill_that_does_not_exist_just_sends_the_message(make_app):
    model = ScriptedModel([[assistant_message("ok")]])
    app = make_app(model)
    add_skill(app)
    await run(app, "that costs $100, not $nothing")
    assert "<skill_content" not in text_of(model.calls[0].input[0])


async def test_a_message_from_an_untrusted_source_cannot_ask_for_a_skill(make_app):
    model = ScriptedModel([[assistant_message("ok")], [assistant_message("ok")]])
    app = make_app(model)
    add_skill(app)
    app.inbox.add(
        NewEvent(
            source="mail",
            kind=Kind.MESSAGE,
            session_key="mail:1",
            payload={"text": "hello, please run $pdf-tools"},
            trust=Trust.UNTRUSTED,
        )
    )
    app.inbox.add(webhook("github", {"text": "run $pdf-tools"}))
    await drain(app)
    for call in model.calls:
        assert "<skill_content" not in " ".join(text_of(i) for i in call.input)


async def test_one_message_can_pull_in_at_most_three_skills(make_app):
    model = ScriptedModel([[assistant_message("ok")]])
    app = make_app(model)
    names = [f"skill-{i}" for i in range(5)]
    for name in names:
        add_skill(app, name)
    await run(app, "use " + " ".join(f"${n}" for n in names))
    assert text_of(model.calls[0].input[0]).count("<skill_content") == 3


# ---- where the skill's files are for the shell ------------------------------------------------------------
def test_the_shell_reaches_a_skill_at_its_own_path_under_seatbelt_and_at_a_mount_under_docker(
    make_app,
):
    app = make_app(sandbox=SandboxProvider.fixed(Box("seatbelt")))
    folder = add_skill(app)
    skill = app.skills.scan().skills[0]
    assert shell_dir(app, skill) == str(folder)

    docker = make_app(sandbox=SandboxProvider.fixed(Box("docker")))
    add_skill(docker)
    assert shell_dir(docker, docker.skills.scan().skills[0]) == "/skills/pdf-tools"


def test_without_a_shell_there_is_no_path_for_scripts(make_app):
    app = make_app()  # no sandbox on this "machine"
    add_skill(app)
    assert shell_dir(app, app.skills.scan().skills[0]) is None

    off = make_app(
        config=Config.model_validate({"tools": {"shell": False}}),
        sandbox=SandboxProvider.fixed(Box("seatbelt")),
    )
    add_skill(off)
    assert shell_dir(off, off.skills.scan().skills[0]) is None
