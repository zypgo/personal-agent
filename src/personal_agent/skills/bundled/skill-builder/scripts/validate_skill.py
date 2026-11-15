#!/usr/bin/env python3
"""Check a draft skill folder: python3 validate_skill.py <folder>. Standard library only."""

import re
import sys
from pathlib import Path

NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def front_matter(text: str) -> dict[str, str]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with a line that holds only '---'")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip().strip("\"'")
    raise ValueError("the front matter is not closed with a second '---' line")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_skill.py <skill folder>")
        return 2
    folder = Path(argv[1])
    problems: list[str] = []
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        print(f"error: {skill_md} does not exist")
        return 1
    text = skill_md.read_text(encoding="utf-8")
    try:
        fields = front_matter(text)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    name = fields.get("name", "")
    if not name:
        problems.append("missing `name`")
    elif not NAME.fullmatch(name) or len(name) > 64:
        problems.append("name must be lowercase letters, digits and single hyphens (at most 64)")
    elif name != folder.resolve().name:
        problems.append(f"name {name!r} should match the folder name {folder.resolve().name!r}")
    description = fields.get("description", "")
    if not description:
        problems.append("missing `description`")
    elif len(description) > 1024:
        problems.append(f"description is {len(description)} characters; the limit is 1024")
    elif len(description) < 40:
        problems.append("description is very short: say what the skill does and when to use it")
    if len(text.splitlines()) > 500:
        problems.append("SKILL.md is longer than 500 lines; move detail into references/")
    links = [p for p in folder.rglob("*") if p.is_symlink()]
    if links:
        problems.append("symbolic links are not installed: " + ", ".join(str(p) for p in links[:3]))
    for problem in problems:
        print(f"problem: {problem}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
