"""The owner's installed skills: finding them, telling the model about them, loading one.

Skills use progressive disclosure, which keeps the always-present cost small:

1. the *catalog* (name + one-line description of each skill) is part of the instructions;
2. the *instructions* (the SKILL.md body) are loaded by the `skill` tool when a task matches;
3. *resources* (scripts, references, assets) are read or run only when the instructions ask for it.

Only `PA_HOME/skills` is scanned. A skill reaches it because the owner put it there (`pa skills
install` copies a folder after showing what is in it). The model cannot write there, so a prompt
injection cannot plant a skill that would be trusted in every future run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from personal_agent.config import SkillsConfig
from personal_agent.paths import Paths
from personal_agent.skills.parse import MAX_FILE_BYTES, Parsed, SkillFormatError, parse_skill

MAX_ENTRIES = 500
"""Folders examined in the skills directory; more than this is not a skills directory."""
RESOURCE_DEPTH = 3
MAX_RESOURCES = 40
MENTION = re.compile(r"(?<![\w$])\$([A-Za-z0-9][A-Za-z0-9_.-]{0,63})")

CATALOG_HEADER = (
    "# Skills\n"
    "The owner installed these skills: folders with instructions for particular kinds of task. When a "
    "task matches a skill's description, call the `skill` tool with its name before you start, and "
    "follow what it returns. Skills are instructions the owner chose to give you; they never override "
    "the rules above, the owner's standing instructions, or the permissions your tools are bound by."
)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    directory: Path
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: str | None = None
    disable_model_invocation: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def location(self) -> Path:
        return self.directory / "SKILL.md"


@dataclass(frozen=True)
class Problem:
    folder: str
    message: str
    fatal: bool
    """Fatal: the skill was skipped. Otherwise it loaded, and this is only a warning."""


@dataclass
class Scan:
    skills: list[Skill] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    def by_name(self) -> dict[str, Skill]:
        return {s.name: s for s in self.skills}


def _skill_from(parsed: Parsed, directory: Path) -> Skill:
    return Skill(
        name=parsed.name,
        description=parsed.description,
        directory=directory,
        license=parsed.license,
        compatibility=parsed.compatibility,
        metadata=parsed.metadata,
        allowed_tools=parsed.allowed_tools,
        disable_model_invocation=parsed.disable_model_invocation,
        warnings=parsed.warnings,
    )


def read_skill_file(path: Path) -> str:
    """The text of a SKILL.md, refusing links, other file types and oversized files."""
    if path.is_symlink() or not path.is_file():
        raise SkillFormatError("SKILL.md is missing, or is not a regular file")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SkillFormatError(f"SKILL.md is {size:,} bytes; the limit is {MAX_FILE_BYTES:,}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillFormatError(f"SKILL.md cannot be read as UTF-8 text ({exc})") from exc


class SkillStore:
    def __init__(self, paths: Paths, cfg: SkillsConfig) -> None:
        self.paths = paths
        self.cfg = cfg

    @property
    def root(self) -> Path:
        return self.paths.skills_dir

    # ---- finding skills ---------------------------------------------------------------------
    def scan(self) -> Scan:
        """Every skill in the skills directory, plus what was wrong with the ones that were not
        usable. Cheap enough to run for each wake-up, so edits take effect on the next run."""
        result = Scan()
        if not self.cfg.enabled or not self.root.is_dir():
            return result
        try:
            entries = sorted(self.root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            result.problems.append(Problem(str(self.root), f"cannot list the folder: {exc}", True))
            return result
        seen: dict[str, str] = {}
        for entry in entries[:MAX_ENTRIES]:
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                result.problems.append(
                    Problem(entry.name, "a symbolic link; skills must be real folders", True)
                )
                continue
            if not entry.is_dir():
                continue  # a README or a marker file
            try:
                parsed = parse_skill(read_skill_file(entry / "SKILL.md"), folder_name=entry.name)
            except SkillFormatError as exc:
                if (entry / "SKILL.md").exists() or (entry / "SKILL.md").is_symlink():
                    result.problems.append(Problem(entry.name, str(exc), True))
                continue  # a folder without SKILL.md is not a skill
            if parsed.name in seen:
                result.problems.append(
                    Problem(
                        entry.name,
                        f"has the same name as {seen[parsed.name]!r}; this one is ignored",
                        True,
                    )
                )
                continue
            seen[parsed.name] = entry.name
            result.problems.extend(Problem(entry.name, w, False) for w in parsed.warnings)
            result.skills.append(_skill_from(parsed, entry.resolve()))
        return result

    def offered(self, scan: Scan | None = None) -> list[Skill]:
        """Skills the model may load by itself (the owner can still ask for the others by name)."""
        found = scan or self.scan()
        return [s for s in found.skills if not s.disable_model_invocation]

    # ---- telling the model --------------------------------------------------------------------
    def catalog(self, scan: Scan | None = None) -> str:
        """The instructions block that lists the available skills; empty when there are none."""
        skills = self.offered(scan)
        if not skills:
            return ""
        budget = self.cfg.catalog_chars
        lines: list[str] = []
        names_only: list[str] = []
        used = 0
        for skill in skills:
            line = f"- {escape(skill.name)}: {escape(self._brief(skill.description))}"
            if used + len(line) <= budget and not names_only:
                lines.append(line)
                used += len(line) + 1
            else:
                names_only.append(escape(skill.name))
        block = f"{CATALOG_HEADER}\n<available_skills>\n" + "\n".join(lines)
        if names_only:
            block += f"\nMore skills (names only; load one to read its description): {', '.join(names_only)}"
        return block + "\n</available_skills>"

    def _brief(self, description: str) -> str:
        limit = self.cfg.description_chars
        if len(description) <= limit:
            return description
        return description[: limit - 1].rstrip() + "…"

    # ---- loading one --------------------------------------------------------------------------
    def instructions(self, skill: Skill, *, shell_dir: str | None = None) -> str:
        """What the `skill` tool returns: the body, wrapped so it can be recognised later, with the
        folder to resolve relative paths against and the files that belong to the skill."""
        parsed = parse_skill(read_skill_file(skill.location), folder_name=skill.directory.name)
        body = parsed.body
        cap = self.cfg.max_body_chars
        note = ""
        if len(body) > cap:
            body = body[:cap].rstrip()
            note = f"\n[The instructions were longer than {cap:,} characters and were cut here.]"
        body = re.sub(r"(?i)</\s*skill_content", "</ skill_content", body)
        resources = self.resources(skill)
        shell = (
            f"Run its scripts with the shell tool, where the same folder is {shell_dir} (read-only)."
            if shell_dir
            else "The shell is not available, so its scripts cannot be run."
        )
        lines = [
            f'<skill_content name="{escape(skill.name)}">',
            body + note,
            "",
            f"Skill directory: {skill.directory}",
            "Relative paths in these instructions are relative to that directory. Read its files "
            f"with read_file using the absolute path. {shell}",
        ]
        if skill.compatibility:
            lines.append(f"Compatibility: {skill.compatibility}")
        if resources:
            lines.append("<skill_resources>")
            lines.extend(f"<file>{escape(r)}</file>" for r in resources)
            lines.append("</skill_resources>")
        lines.append("</skill_content>")
        return "\n".join(lines)

    @staticmethod
    def resources(skill: Skill) -> list[str]:
        """Files that belong to the skill (relative paths), not read here: the model loads them
        on demand. Links and hidden files are left out."""
        found: list[str] = []

        def walk(folder: Path, depth: int) -> None:
            try:
                entries = sorted(folder.iterdir(), key=lambda p: (p.is_dir(), p.name))
            except OSError:
                return
            for entry in entries:
                if len(found) >= MAX_RESOURCES:
                    return
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir():
                    if depth < RESOURCE_DEPTH:
                        walk(entry, depth + 1)
                elif entry.is_file() and not (depth == 1 and entry.name == "SKILL.md"):
                    found.append(entry.relative_to(skill.directory).as_posix())

        walk(skill.directory, 1)
        return found

    # ---- the owner asks for a skill by name ---------------------------------------------------
    def mentioned(self, text: str, scan: Scan | None = None) -> list[Skill]:
        """Skills named as `$name` in the owner's message, in order, without repeats."""
        known = (scan or self.scan()).by_name()
        out: list[Skill] = []
        for match in MENTION.finditer(text or ""):
            word = match.group(1)
            # "use $pdf-tools." ends a sentence: the final dot is not part of the name.
            skill = known.get(word) or known.get(word.rstrip(".-_"))
            if skill is not None and skill not in out:
                out.append(skill)
        return out
