"""Turning what an MCP server says into what the model is shown, and back.

Everything here is text the server wrote, so it is shortened, stripped of control characters and
(for results) fenced as outside content by the caller. Nothing is trusted because the server
asserts it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from personal_agent.security.sanitize import strip_invisible

MAX_TOOL_NAME = 64
"""OpenAI function names are at most 64 characters of letters, digits, underscore and hyphen."""
DESCRIPTION_CHARS = 1_200
SCHEMA_DESCRIPTION_CHARS = 400
_NOT_NAME = re.compile(r"[^A-Za-z0-9_-]")


def safe_part(text: str) -> str:
    return _NOT_NAME.sub("_", text) or "_"


def public_name(server: str, tool: str, taken: set[str]) -> str:
    """`mcp__<server>__<tool>`: unique, within the 64-character limit, only legal characters."""
    name = f"mcp__{server}__{safe_part(tool)}"
    needs_suffix = len(name) > MAX_TOOL_NAME or name in taken or safe_part(tool) != tool
    if not needs_suffix:
        return name
    # A hash of the real name keeps two tools that clean up to the same text apart, and keeps
    # a name stable between runs (approvals and rules refer to it).
    digest = hashlib.sha256(f"{server}\0{tool}".encode()).hexdigest()[:8]
    stem = name[: MAX_TOOL_NAME - 9].rstrip("_-")
    return f"{stem}_{digest}"


def clean_text(text: str, limit: int) -> str:
    """Server text the model will read as a description: nothing the owner cannot see."""
    text = strip_invisible(text or "", formatting=True).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def describe(tool_description: str | None, title: str | None) -> str:
    text = clean_text(tool_description or "", DESCRIPTION_CHARS)
    if not text and title:
        text = clean_text(title, 200)
    return text or "(this tool has no description)"


def _shorten_schema_text(node: Any) -> Any:
    """Parameter descriptions are model-visible server text too; keep them short."""
    if isinstance(node, dict):
        return {
            key: (
                clean_text(value, SCHEMA_DESCRIPTION_CHARS)
                if key == "description" and isinstance(value, str)
                else _shorten_schema_text(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_shorten_schema_text(item) for item in node]
    return node


def parameters_schema(input_schema: dict[str, Any] | None) -> dict[str, Any]:
    """A schema the OpenAI API accepts for a function: an object with `properties`.

    MCP schemas are used as the server wrote them (not forced into "strict" mode, which would
    make every optional parameter required and nullable and change what the server receives)."""
    schema = _shorten_schema_text(dict(input_schema or {}))
    schema.pop("$schema", None)
    if schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    return schema


def model_view(definition: dict[str, Any]) -> list[str]:
    """What the model is told about a tool, line by line, for the owner to read and to compare.

    This is built from the same functions that build the tool the model gets, so the review is of
    the real thing, not of a summary."""
    lines = ["description:"]
    lines += [
        f"  {line}"
        for line in describe(definition.get("description"), definition.get("title")).splitlines()
    ]
    schema = parameters_schema(definition.get("inputSchema"))
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if properties:
        lines.append("parameters:")
    for key, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("type")
        kind = "/".join(map(str, kind)) if isinstance(kind, list) else (kind or "any")
        flags = f"{kind}, required" if key in required else str(kind)
        note = spec.get("description")
        lines.append(f"  {key} ({flags})" + (f": {' '.join(str(note).split())}" if note else ""))
        for extra in ("enum", "default"):
            if extra in spec:
                lines.append(f"    {extra}: {json.dumps(spec[extra], ensure_ascii=False)}")
    claims = definition.get("annotations") or {}
    if claims:
        shown = ", ".join(f"{k}={json.dumps(v)}" for k, v in sorted(claims.items()))
        lines.append(f"the server says about itself (not checked): {shown}")
    return lines


def matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


@dataclass(frozen=True)
class Rendered:
    text: str
    is_error: bool
    omitted: tuple[str, ...] = ()
    """What was left out (images, audio, binary resources), for the owner's diagnostics."""


def _kilobytes(base64_text: str) -> str:
    return f"{max(1, len(base64_text) * 3 // 4 // 1024)} KB"


def _item_text(item: Any, omitted: list[str]) -> str | None:
    kind = getattr(item, "type", "")
    if kind == "text":
        return str(item.text)
    if kind == "image":
        omitted.append("image")
        return f"[an image was returned and left out: {item.mime_type}, {_kilobytes(item.data)}]"
    if kind == "audio":
        omitted.append("audio")
        return f"[audio was returned and left out: {item.mime_type}, {_kilobytes(item.data)}]"
    if kind == "resource":
        resource = item.resource
        text = getattr(resource, "text", None)
        if text is not None:
            return f"[resource {resource.uri}]\n{text}"
        omitted.append("resource")
        mime = getattr(resource, "mime_type", None) or "unknown type"
        return f"[a binary resource was returned and left out: {resource.uri}, {mime}]"
    if kind == "resource_link":
        suffix = f" ({item.description})" if getattr(item, "description", None) else ""
        return f"[link: {item.uri}{suffix}]"
    omitted.append(str(kind) or "unknown")
    return f"[content of type {kind or 'unknown'!r} was left out]"


def render_result(result: Any, *, room: int) -> Rendered:
    """A tool result as plain text of at most about `room` characters."""
    omitted: list[str] = []
    parts = [t for item in (result.content or []) if (t := _item_text(item, omitted)) is not None]
    structured = getattr(result, "structured_content", None)
    has_real_text = any(getattr(item, "type", "") == "text" for item in (result.content or []))
    if structured and not has_real_text:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))
    text = strip_invisible("\n".join(parts)).strip() or "(the tool returned nothing)"
    if len(text) > room:
        text = f"{text[:room]}\n[... {len(text) - room} more characters omitted]"
    return Rendered(text, bool(getattr(result, "is_error", False)), tuple(omitted))
