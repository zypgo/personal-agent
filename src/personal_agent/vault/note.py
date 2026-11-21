"""Reading and writing Obsidian-style Markdown: properties, headings, `[[links]]` and `#tags`.

A note is a text file. This module never needs the file system, so it can be tested on strings.
Three rules keep it safe to run on notes other people (and other agents) wrote:

- The properties block is read with `yaml.safe_load` under a budget of nodes and depth, so a
  hostile block cannot grow when it is walked.
- Links and tags are looked for outside code blocks, code spans and `%%comments%%`, as Obsidian does.
- A note's own formatting is left alone. Only notes the product generates are re-rendered; for the
  rest, the product edits one section or appends.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import yaml

MAX_FRONT_CHARS = 8_000
MAX_FRONT_NODES = 2_000
MAX_FRONT_DEPTH = 6
_FRONT_LINES = 200


@dataclass(frozen=True)
class Parsed:
    front: dict[str, Any]
    """The note's properties (empty when there are none or they cannot be read)."""
    front_raw: str | None
    """The properties block exactly as written, without the `---` lines."""
    front_error: str | None
    body: str


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0


def _plain(value: Any, budget: _Budget, depth: int = 0) -> Any:
    budget.nodes += 1
    if budget.nodes > MAX_FRONT_NODES or depth > MAX_FRONT_DEPTH:
        raise ValueError("too much nesting")
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v, budget, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(v, budget, depth + 1) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def split_front(text: str) -> tuple[str | None, str]:
    """`(properties block or None, the rest)`. The block must open on the very first line."""
    if text.startswith("\ufeff"):
        text = text[1:]
    first, _, rest = text.partition("\n")
    if first.rstrip() != "---":
        return None, text
    lines = rest.split("\n")
    for index, line in enumerate(lines[:_FRONT_LINES]):
        if line.rstrip() in ("---", "..."):
            raw = "\n".join(lines[:index]).rstrip("\r")
            body = "\n".join(lines[index + 1 :])
            return raw, body
    return None, text


def parse(text: str) -> Parsed:
    raw, body = split_front(text)
    front: dict[str, Any] = {}
    error: str | None = None
    if raw is not None:
        if len(raw) > MAX_FRONT_CHARS:
            error = "属性区太长，没有读取"
        elif raw.strip():
            try:
                data = yaml.safe_load(raw)
                if data is None:
                    data = {}
                if isinstance(data, dict):
                    front = _plain(data, _Budget())
                else:
                    error = "属性区不是「名称: 值」的格式"
            except (yaml.YAMLError, ValueError, RecursionError):
                error = "属性区读不懂"
    return Parsed(front, raw, error, body)


class _Properties(yaml.SafeDumper):
    """Properties the way Obsidian shows them: one `key: value` to a line (never `{key: value}`),
    short lists of words on one line."""


def _one_per_line(dumper: yaml.SafeDumper, data: dict[Any, Any]) -> yaml.MappingNode:
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=False)


_Properties.add_representer(dict, _one_per_line)


def render(front: dict[str, Any], body: str) -> str:
    """A note from properties and text. Keys keep their order; lists of words stay on one line."""
    if not front:
        return body
    block = yaml.dump(
        front,
        Dumper=_Properties,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=None,
        width=1_000,
    ).rstrip("\n")
    return f"---\n{block}\n---\n{body.lstrip(chr(10))}"


def as_list(value: Any) -> list[str]:
    """A property that may be a list or one string ("a, b") as a list of words."""
    if value is None:
        return []
    if isinstance(value, list):
        found = [
            str(v).strip() for v in value if isinstance(v, str | int | float) and str(v).strip()
        ]
    elif isinstance(value, str):
        found = [part.strip() for part in re.split(r"[,\n]", value) if part.strip()]
    else:
        found = []
    return found


# ---- code-aware scanning -------------------------------------------------------------
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_COMMENT = re.compile(r"%%.*?%%", re.S)


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def mask_code(body: str) -> str:
    """The text with code blocks, code spans and `%%comments%%` replaced by spaces, so that what
    is looked for (links, tags) is not found inside them. Positions and line numbers stay."""
    out: list[str] = []
    fence: str | None = None
    for line in body.split("\n"):
        marker = _FENCE.match(line)
        if fence is None:
            if marker:
                fence = marker.group(1)
                out.append(" " * len(line))
            else:
                out.append(line)
        else:
            out.append(" " * len(line))
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
    text = "\n".join(out)
    text = _COMMENT.sub(_blank, text)
    return _INLINE_CODE.sub(_blank, text)


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int
    """Line number inside the body, from 0."""


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def headings(body: str) -> list[Heading]:
    found = []
    for number, line in enumerate(mask_code(body).split("\n")):
        match = _HEADING.match(line)
        if match:
            found.append(Heading(len(match.group(1)), match.group(2).strip(), number))
    return found


@dataclass(frozen=True)
class Link:
    target: str
    """What stands before `#` and `|`: a note name, or a path without `.md`."""
    heading: str
    alias: str
    embed: bool


_WIKILINK = re.compile(r"(!?)\[\[([^\[\]|#^\n]+?)(?:#([^\[\]|\n]*?))?(?:\|([^\[\]\n]*?))?\]\]")


def links(parsed: Parsed) -> list[Link]:
    """Every `[[link]]` and `![[embed]]`, in order, properties included (Obsidian counts them)."""
    sources = [mask_code(parsed.body)]
    if parsed.front_raw:
        sources.append(parsed.front_raw)
    found = []
    for source in sources:
        for match in _WIKILINK.finditer(source):
            target = unicodedata.normalize("NFC", match.group(2).strip())
            if target:
                found.append(
                    Link(
                        target,
                        (match.group(3) or "").strip(),
                        (match.group(4) or "").strip(),
                        bool(match.group(1)),
                    )
                )
    return found


_TAG = re.compile(r"(?:(?<=^)|(?<=[\s(\[{,;，；。（]))#([\w/\-]+)", re.M)


def inline_tags(body: str) -> list[str]:
    found: list[str] = []
    for match in _TAG.finditer(mask_code(body)):
        tag = match.group(1).strip("/-")
        if tag and not tag.isdigit() and tag not in found:
            found.append(tag)
    return found


def tags_of(parsed: Parsed) -> list[str]:
    found = []
    for raw in as_list(parsed.front.get("tags")) + as_list(parsed.front.get("tag")):
        tag = raw.lstrip("#").strip()
        if tag and tag not in found:
            found.append(tag)
    for tag in inline_tags(parsed.body):
        if tag not in found:
            found.append(tag)
    return found


# ---- names ---------------------------------------------------------------------------
_BAD_NAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f\x7f]')


def slugify(text: str, *, limit: int = 40) -> str:
    """A file-name-friendly version of a title that keeps Chinese as it is."""
    text = unicodedata.normalize("NFC", text)
    text = _BAD_NAME.sub(" ", text)
    text = re.sub(r"\s+", "-", text.strip())
    text = text.strip("-.")
    if len(text) > limit:
        text = text[:limit].rstrip("-.")
    return text or "笔记"


def title_of(parsed: Parsed, stem: str) -> str:
    title = parsed.front.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:200]
    for heading in headings(parsed.body):
        if heading.level == 1:
            return _plain_text(heading.text)[:200]
    return stem


_MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
_WIKI_SHOWN = re.compile(r"\[\[(?:[^\]|\n]*\|)?([^\]\n]*)\]\]")


def _plain_text(text: str) -> str:
    text = _WIKI_SHOWN.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = re.sub(r"[*_`~]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def summary_of(parsed: Parsed, *, chars: int = 80) -> str:
    for key in ("summary", "description"):
        value = parsed.front.get(key)
        if isinstance(value, str) and value.strip():
            return _plain_text(value)[:chars]
    in_fence = False
    for line in parsed.body.split("\n"):
        stripped = line.strip()
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if stripped.startswith(("#", ">", "|", "---", "***", "<!--", "%%")):
            continue
        text = re.sub(r"^([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?", "", stripped)
        text = _plain_text(text)
        if text:
            return text if len(text) <= chars else text[: chars - 1] + "…"
    return ""


# ---- sections ------------------------------------------------------------------------
def _section_bounds(body: str, heading: str) -> tuple[int, int, int] | None:
    """`(first line, line after the last, level)` of the section under `heading`."""
    wanted = _plain_text(heading).casefold()
    lines = body.split("\n")
    marks = headings(body)
    for index, mark in enumerate(marks):
        if _plain_text(mark.text).casefold() != wanted:
            continue
        end = len(lines)
        for later in marks[index + 1 :]:
            if later.level <= mark.level:
                end = later.line
                break
        return mark.line, end, mark.level
    return None


def has_section(body: str, heading: str) -> bool:
    return _section_bounds(body, heading) is not None


def read_section(body: str, heading: str) -> str | None:
    bounds = _section_bounds(body, heading)
    if bounds is None:
        return None
    start, end, _ = bounds
    return "\n".join(body.split("\n")[start + 1 : end]).strip("\n")


def replace_section(body: str, heading: str, content: str, *, level: int = 2) -> str:
    """The section's text replaced (its heading kept); added at the end when there is none."""
    content = content.strip("\n")
    lines = body.split("\n")
    bounds = _section_bounds(body, heading)
    if bounds is None:
        head = "#" * level + " " + heading
        tail = body.rstrip("\n")
        sep = "\n\n" if tail else ""
        return f"{tail}{sep}{head}\n\n{content}\n" if content else f"{tail}{sep}{head}\n"
    start, end, _ = bounds
    new = [lines[start], "", *content.split("\n"), ""] if content else [lines[start], ""]
    return "\n".join([*lines[:start], *new, *lines[end:]]).rstrip("\n") + "\n"


def append_section(body: str, heading: str, content: str, *, level: int = 2) -> str:
    """`content` added at the end of the section; the section is created when it is missing."""
    content = content.strip("\n")
    bounds = _section_bounds(body, heading)
    if bounds is None:
        return replace_section(body, heading, content, level=level)
    lines = body.split("\n")
    start, end, _ = bounds
    existing = "\n".join(lines[start + 1 : end]).strip("\n")
    merged = f"{existing}\n{content}" if existing else content
    return replace_section(body, heading, merged, level=level)
