"""Editing the `[[mcp.servers]]` entries of config.toml without disturbing the rest of the file.

`pa mcp add` and `pa mcp remove` change the owner's configuration, so they are careful: comments
and layout stay as they were, the result must still be a valid configuration (checked before
anything is written), and the file is replaced atomically.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import ValidationError
from tomlkit.exceptions import TOMLKitError

from personal_agent.config import Config


class ConfigEditError(Exception):
    """The change was not made; the message says why."""


def _read(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, TOMLKitError) as exc:
        raise ConfigEditError(f"cannot read {path}: {exc}") from exc


def _problems(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"] if part != "servers")
        message = str(error["msg"]).removeprefix("Value error, ")
        lines.append(f"{where}: {message}" if where else message)
    return "\n".join(lines)


def _check(doc: tomlkit.TOMLDocument) -> None:
    try:
        Config.model_validate(doc.unwrap())
    except ValidationError as exc:
        raise ConfigEditError(_problems(exc)) from exc


def _write(path: Path, doc: tomlkit.TOMLDocument) -> None:
    text = tomlkit.dumps(doc)
    try:
        tomllib.loads(text)  # never write a file that would not load
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditError(f"the edited file would not be valid TOML: {exc}") from exc
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ConfigEditError(f"cannot write {path}: {exc}") from exc


def _value(value: Any) -> Any:
    if isinstance(value, dict):
        table = tomlkit.inline_table()
        table.update({str(k): _value(v) for k, v in value.items()})
        return table
    return value


def _servers(doc: tomlkit.TOMLDocument) -> Any:
    mcp = doc.get("mcp")
    return mcp.get("servers") if mcp is not None else None


def add_server(path: Path, entry: dict[str, Any]) -> None:
    """Append an `[[mcp.servers]]` entry. Refuses a duplicate name or an invalid result."""
    doc = _read(path)
    mcp = doc.get("mcp")
    if mcp is None:
        mcp = tomlkit.table()
        doc["mcp"] = mcp
    servers = mcp.get("servers")
    if servers is None:
        servers = tomlkit.aot()
        mcp["servers"] = servers
    name = entry["name"]
    if any(str(existing.get("name", "")).lower() == str(name).lower() for existing in servers):
        raise ConfigEditError(f"an MCP server called {name!r} is already configured")
    table = tomlkit.table()
    for key, value in entry.items():
        table.add(key, _value(value))
    servers.append(table)
    _check(doc)
    _write(path, doc)


def remove_server(path: Path, name: str) -> bool:
    """Delete the entry with this name. False when there was none."""
    doc = _read(path)
    servers = _servers(doc)
    if servers is None:
        return False
    for index, existing in enumerate(servers):
        if existing.get("name") == name:
            del servers[index]
            break
    else:
        return False
    if len(servers) == 0:
        mcp = doc["mcp"]
        del mcp["servers"]
        if len(mcp) == 0:
            del doc["mcp"]
    _check(doc)
    _write(path, doc)
    return True
