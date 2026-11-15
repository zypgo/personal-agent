"""Pins: what the owner has reviewed of each MCP tool.

A tool's name, description and parameters are text the model reads and believes. A server that
is fine today can ship a different description tomorrow (an update, a compromised package), so
a tool is offered only while its definition is the one the owner looked at. Anything new or
changed waits for `pa mcp trust`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from personal_agent.clock import Clock, SystemClock
from personal_agent.db import Database, dumps, loads

Definition = dict[str, Any]


class Review(StrEnum):
    TRUSTED = "trusted"
    """The definition is the one the owner reviewed."""
    UNREVIEWED = "unreviewed"
    """Nothing of this server has been reviewed yet."""
    NEW = "new"
    """The server has reviewed tools, but not this one."""
    CHANGED = "changed"
    """Reviewed once, and its definition is different now."""


def definition_of(tool: Any) -> Definition:
    """The parts of a listed tool that the model sees or that decide how it is treated.

    Keys follow the protocol's wire names, not the SDK's attribute names, so a pin survives an
    SDK upgrade."""
    annotations = getattr(tool, "annotations", None)
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    return {
        "name": tool.name,
        "title": getattr(tool, "title", None) or None,
        "description": getattr(tool, "description", None) or "",
        "inputSchema": schema,
        "annotations": (
            annotations.model_dump(mode="json", exclude_none=True, by_alias=True)
            if annotations is not None
            else {}
        ),
    }


def digest_of(definition: Mapping[str, Any]) -> str:
    text = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Pin:
    server: str
    tool: str
    digest: str
    definition: Definition
    pinned_at: float
    pinned_by: str


class McpPins:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()

    def for_server(self, server: str) -> dict[str, Pin]:
        rows = self.db.query("SELECT * FROM mcp_pins WHERE server = ? ORDER BY tool", (server,))
        return {
            r["tool"]: Pin(
                server=r["server"],
                tool=r["tool"],
                digest=r["digest"],
                definition=loads(r["definition"], {}),
                pinned_at=r["pinned_at"],
                pinned_by=r["pinned_by"],
            )
            for r in rows
        }

    def count(self, server: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM mcp_pins WHERE server = ?", (server,), 0))

    def fingerprint(self, server: str) -> str:
        """Changes whenever the reviewed set changes, so a running agent notices `pa mcp trust`
        (made in another process) without waiting for the next listing."""
        rows = self.db.query("SELECT tool, digest FROM mcp_pins WHERE server = ?", (server,))
        text = "\n".join(sorted(f"{r['tool']}\0{r['digest']}" for r in rows))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def review(self, pins: Mapping[str, Pin], definition: Mapping[str, Any]) -> Review:
        """How one listed tool stands against what is pinned (`pins` = `for_server`)."""
        if not pins:
            return Review.UNREVIEWED
        pin = pins.get(definition["name"])
        if pin is None:
            return Review.NEW
        return Review.TRUSTED if pin.digest == digest_of(definition) else Review.CHANGED

    def replace(self, server: str, definitions: Iterable[Mapping[str, Any]], *, by: str) -> int:
        """Make exactly these definitions the reviewed ones for the server."""
        rows = [
            (server, d["name"], digest_of(d), dumps(d), self.clock.now(), by) for d in definitions
        ]
        with self.db.tx():
            self.db.execute("DELETE FROM mcp_pins WHERE server = ?", (server,))
            self.db.executemany(
                "INSERT INTO mcp_pins(server, tool, digest, definition, pinned_at, pinned_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def forget(self, server: str) -> int:
        with self.db.tx():
            return self.db.execute("DELETE FROM mcp_pins WHERE server = ?", (server,)).rowcount
