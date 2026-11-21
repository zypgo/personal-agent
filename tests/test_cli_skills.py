"""`pa skills`, the starter skills that `pa init` adds, and what `pa doctor` says about skills."""

from __future__ import annotations

from pathlib import Path

from helpers import invoke
from personal_agent.doctor import capability_checks
from personal_agent.paths import Paths

SKILL = """\
---
name: pdf-tools
description: Extract text from PDFs. Use for any question about a PDF file.
license: MIT
---
# PDF tools

Run the extractor, then summarise.
"""


def make_source(root: Path, name: str = "pdf-tools", text: str = SKILL, **files: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    for relative, content in files.items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(content, encoding="utf-8")
    return folder


def flat(output: str) -> str:
    return " ".join(output.split())


# ---- init -------------------------------------------------------------------------------------------
def test_init_adds_the_starter_skills_once(home):
    first = invoke("init")
    assert first.exit_code == 0, first.output
    assert "added starter skills: " in flat(first.output)
    assert (home / "skills" / "skill-builder" / "SKILL.md").is_file()
    assert (home / "skills" / "research-brief" / "SKILL.md").is_file()

    second = invoke("init")
    assert second.exit_code == 0 and "added starter skills" not in second.output


# ---- list / show --------------------------------------------------------------------------------
def test_with_no_skills_the_list_says_how_to_add_some(wired):
    wired()
    result = invoke("skills")
    assert result.exit_code == 0 and "No skills installed" in flat(result.output)
    assert "pa init" in result.output


def test_the_list_shows_skills_and_the_ones_that_could_not_be_used(wired, tmp_path):
    app = wired()
    make_source(app.paths.skills_dir, "pdf-tools")
    make_source(app.paths.skills_dir, "broken", "---\nname: broken\n---\nbody")
    hidden = SKILL.replace("name: pdf-tools", "name: owner-only\ndisable-model-invocation: true")
    make_source(app.paths.skills_dir, "owner-only", hidden)

    for command in (("skills",), ("skills", "list")):
        out = flat(invoke(*command).output)
        assert "pdf-tools yes Extract text from PDFs." in out
        assert "owner-only no (owner only: $name)" in out
        assert "skipped broken: the front matter has no `description`" in out


def test_show_prints_the_instructions_and_the_files(wired):
    app = wired()
    make_source(app.paths.skills_dir, "pdf-tools", **{"scripts/extract.py": "print(1)"})
    result = invoke("skills", "show", "pdf-tools")
    out = flat(result.output)
    assert result.exit_code == 0
    assert "pdf-tools" in out and "license: MIT" in out
    assert "files: scripts/extract.py" in out
    assert "Run the extractor, then summarise." in out
    assert "name: pdf-tools" not in out  # the front matter is not repeated


def test_show_for_an_unknown_name_fails(wired):
    wired()
    result = invoke("skills", "show", "nope")
    assert result.exit_code == 1 and "No installed skill named nope" in result.output


def test_text_from_a_skill_is_not_read_as_terminal_markup(wired):
    app = wired()
    text = "---\nname: odd\ndescription: '[bold red]shout[/] [link=http://x]click[/link]'\n---\n[/] body"
    make_source(app.paths.skills_dir, "odd", text)
    result = invoke("skills", "list")
    assert result.exit_code == 0
    assert "[bold red]shout[/]" in flat(result.output)
    assert invoke("skills", "show", "odd").exit_code == 0


# ---- validate -----------------------------------------------------------------------------------
def test_validate_accepts_a_good_folder_and_names_the_scripts(tmp_path):
    source = make_source(tmp_path / "src", **{"scripts/run.py": "print(1)"})
    result = invoke("skills", "validate", str(source))
    out = flat(result.output)
    assert result.exit_code == 0 and "This folder is a valid skill." in out
    assert "Contains scripts" in out and "scripts/run.py" in out


def test_validate_rejects_a_bad_folder(tmp_path):
    source = make_source(tmp_path / "src", "bad", "---\nname: bad\n---\nbody")
    result = invoke("skills", "validate", str(source))
    assert result.exit_code == 1 and "no `description`" in flat(result.output)
    assert invoke("skills", "validate", str(tmp_path / "missing")).exit_code == 1


# ---- install / remove -------------------------------------------------------------------------------
def test_install_shows_the_review_and_asks_first(wired, tmp_path):
    app = wired()
    source = make_source(tmp_path / "src", **{"scripts/run.py": "print(1)"})

    declined = invoke("skills", "install", str(source), input="n\n")
    assert declined.exit_code == 1 and "Not installed." in declined.output
    assert not (app.paths.skills_dir / "pdf-tools").exists()

    accepted = invoke("skills", "install", str(source), input="y\n")
    out = flat(accepted.output)
    assert accepted.exit_code == 0, accepted.output
    assert "Contains scripts" in out and "Install it?" in out
    assert "Installed pdf-tools" in out
    assert (app.paths.skills_dir / "pdf-tools" / "scripts" / "run.py").is_file()


def test_install_with_yes_skips_the_question_and_force_replaces(wired, tmp_path):
    app = wired()
    first = make_source(tmp_path / "one", **{"old.txt": "old"})
    assert invoke("skills", "install", str(first), "--yes").exit_code == 0

    second = make_source(tmp_path / "two", text=SKILL.replace("Run the extractor", "Run it"))
    refused = invoke("skills", "install", str(second), "--yes")
    assert refused.exit_code == 1 and "already installed" in flat(refused.output)
    assert (app.paths.skills_dir / "pdf-tools" / "old.txt").exists()

    replaced = invoke("skills", "install", str(second), "--yes", "--force")
    assert replaced.exit_code == 0
    assert not (app.paths.skills_dir / "pdf-tools" / "old.txt").exists()
    assert "Run it" in (app.paths.skills_dir / "pdf-tools" / "SKILL.md").read_text()


def test_install_refuses_a_bad_folder_and_a_bad_address(wired, tmp_path):
    app = wired()
    bad = make_source(tmp_path / "src", "bad", "no front matter")
    refused = invoke("skills", "install", str(bad), "--yes")
    assert refused.exit_code == 1 and "cannot be installed" in refused.output
    assert list(app.paths.skills_dir.iterdir()) == []

    for address in ("http://example.com/skills.git", "https://user:pw@example.com/skills.git"):
        result = invoke("skills", "install", address, "--yes")
        assert result.exit_code == 1 and "https" in flat(result.output)
    assert list(app.paths.skills_dir.iterdir()) == []


def test_remove_asks_first_then_deletes(wired, tmp_path):
    app = wired()
    make_source(app.paths.skills_dir, "pdf-tools")

    kept = invoke("skills", "remove", "pdf-tools", input="n\n")
    assert kept.exit_code == 1 and "Kept." in kept.output
    assert (app.paths.skills_dir / "pdf-tools").exists()

    gone = invoke("skills", "remove", "pdf-tools", "--yes")
    assert gone.exit_code == 0 and not (app.paths.skills_dir / "pdf-tools").exists()
    again = invoke("skills", "remove", "pdf-tools", "--yes")
    assert again.exit_code == 1 and "no installed skill" in flat(again.output)


# ---- doctor ----------------------------------------------------------------------------------------
def checks(home: Path, toml: str = "[tools]\nshell = false\n"):
    paths = Paths.resolve(home)
    paths.ensure()
    paths.config_file.write_text(toml)
    return {c.name: c for c in capability_checks(paths)}


def test_doctor_hints_at_init_when_nothing_is_installed(tmp_path):
    check = checks(tmp_path / "h")["skills"]
    assert check.status == "ok" and check.detail == "none installed" and "pa init" in check.fix


def test_doctor_counts_skills_and_flags_the_unusable(tmp_path):
    home = tmp_path / "h"
    make_source(home / "skills", "pdf-tools")
    assert checks(home)["skills"].detail == "1 installed"

    make_source(home / "skills", "broken", "---\nname: broken\n---\nbody")
    check = checks(home)["skills"]
    assert check.status == "warn" and "1 skipped" in check.detail and "broken" in check.detail


def test_doctor_says_when_skills_are_switched_off(tmp_path):
    check = checks(tmp_path / "h", "[tools]\nshell = false\n[skills]\nenabled = false\n")["skills"]
    assert check.status == "ok" and "switched off" in check.detail
