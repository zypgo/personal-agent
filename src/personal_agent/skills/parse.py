"""Reading one `SKILL.md`: YAML front matter plus a Markdown body, as the Agent Skills format defines.

The reader is lenient on purpose. Skills written for other tools often carry front matter that is
slightly off (an unquoted colon, a name that differs from its folder), and refusing them would make
the ecosystem unusable. So: problems that make a skill unusable (no readable front matter, no
description, a name that could break the prompt) skip it; cosmetic ones are recorded as warnings
and the skill still loads. The warnings are shown by `pa skills list` and `pa doctor`.

Format: https://agentskills.io/specification (read from there; nothing is copied).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from personal_agent.parsing import load_yaml
from personal_agent.security.sanitize import strip_invisible

MAX_FILE_BYTES = 262_144
"""A SKILL.md larger than this is not read: the format asks for under 500 lines."""
NAME_MAX = 64
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500

SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
"""What the loader accepts as a name: nothing that could be mistaken for markup or a path."""
STRICT_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
"""What the specification asks for: lowercase letters, digits and single hyphens."""

_FENCE = re.compile(r"^---[ \t]*$")
_PLAIN_ENTRY = re.compile(r"^([A-Za-z][\w-]*):[ \t]+(.+?)[ \t]*$")
_QUOTED = re.compile(r"""^(".*"|'.*'|[|>][+-]?\d*)$""")


class SkillFormatError(Exception):
    """This SKILL.md cannot be used; the message says why."""


@dataclass(frozen=True)
class Parsed:
    name: str
    description: str
    body: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: str | None = None
    disable_model_invocation: bool = False
    warnings: tuple[str, ...] = ()


def split_front_matter(text: str) -> tuple[str, str]:
    """(front matter, body). Raises SkillFormatError when the file has no front matter."""
    lines = text.lstrip("\ufeff").splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or not _FENCE.match(lines[index]):
        raise SkillFormatError("SKILL.md must start with a '---' line that opens the front matter")
    for end in range(index + 1, len(lines)):
        if _FENCE.match(lines[end]):
            return "\n".join(lines[index + 1 : end]), "\n".join(lines[end + 1 :]).strip()
    raise SkillFormatError("the front matter is never closed with a second '---' line")


def _quote_plain_values(front: str) -> str:
    """Wrap plain `key: value` entries whose value holds a colon in quotes.

    `description: Use this when: the user asks` is not valid YAML but other tools accept it."""
    out: list[str] = []
    for line in front.splitlines():
        match = _PLAIN_ENTRY.match(line)
        if match and ": " in match.group(2) and not _QUOTED.match(match.group(2)):
            value = match.group(2).replace("\\", "\\\\").replace('"', '\\"')
            line = f'{match.group(1)}: "{value}"'
        out.append(line)
    return "\n".join(out)


def _load_yaml(front: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        data = load_yaml(front)
    except yaml.YAMLError:
        try:
            data = load_yaml(_quote_plain_values(front))
        except yaml.YAMLError as exc:
            raise SkillFormatError(
                f"the front matter is not valid YAML ({_one_line(exc)})"
            ) from exc
        warnings.append(
            "the front matter was not valid YAML as written (an unquoted colon?); read leniently"
        )
    if not isinstance(data, dict):
        raise SkillFormatError(
            "the front matter must be a YAML mapping (name: ..., description: ...)"
        )
    return data, warnings


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())[:160]


def _text(value: Any) -> str | None:
    """A front matter value as one line of text; a list reads as its items, a table as nothing
    (it has no text to give)."""
    if value is None or isinstance(value, dict):
        return None
    if isinstance(value, list):
        return " ".join(filter(None, (_text(item) for item in value))) or None
    return " ".join(str(value).split())


def parse_skill(text: str, *, folder_name: str | None = None) -> Parsed:
    """Parse the content of a SKILL.md. `folder_name` enables the "name matches folder" check."""
    normalized = text.replace("\r\n", "\n")
    text = strip_invisible(normalized)
    front, body = split_front_matter(text)
    data, warnings = _load_yaml(front)
    if text != normalized:
        warnings.append(
            "the file had hidden characters (invisible text, text-direction overrides or control "
            "codes); they were removed, so the agent reads only what you can see"
        )

    name = _text(data.get("name"))
    if not name:
        raise SkillFormatError("the front matter has no `name`")
    if not SAFE_NAME.fullmatch(name):
        raise SkillFormatError(
            f"the name {name!r} is not usable: use letters, digits, '-', '_' or '.' (at most {NAME_MAX})"
        )
    if not STRICT_NAME.fullmatch(name):
        warnings.append(f"name {name!r} should be lowercase letters, digits and single hyphens")
    if folder_name is not None and name != folder_name:
        warnings.append(f"name {name!r} differs from its folder {folder_name!r}")

    description = _text(data.get("description"))
    if not description:
        raise SkillFormatError(
            "the front matter has no `description`, which the model needs to choose the skill"
        )
    if len(description) > DESCRIPTION_MAX:
        warnings.append(
            f"description is {len(description)} characters; the format allows {DESCRIPTION_MAX}"
        )
        description = description[:DESCRIPTION_MAX]

    compatibility = _text(data.get("compatibility"))
    if compatibility and len(compatibility) > COMPATIBILITY_MAX:
        warnings.append(f"compatibility is longer than {COMPATIBILITY_MAX} characters")
        compatibility = compatibility[:COMPATIBILITY_MAX]

    raw_meta = data.get("metadata")
    metadata: dict[str, str] = {}
    if isinstance(raw_meta, dict):
        metadata = {str(k): _text(v) or "" for k, v in raw_meta.items()}
    elif raw_meta is not None:
        warnings.append("metadata should be a mapping of text to text; ignored")

    allowed = _text(
        data.get("allowed-tools") if "allowed-tools" in data else data.get("allowed_tools")
    )
    if allowed:
        warnings.append(
            "allowed-tools is ignored: a skill never grants permissions, your policy decides"
        )

    disable = data.get("disable-model-invocation", data.get("disable_model_invocation", False))
    return Parsed(
        name=name,
        description=description,
        body=body,
        license=_text(data.get("license")),
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=allowed,
        disable_model_invocation=disable is True,
        warnings=tuple(warnings),
    )
