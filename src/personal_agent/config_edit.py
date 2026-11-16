"""Small, validated edits to the owner's configuration, preserving comments and permissions."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import ValidationError
from tomlkit.exceptions import TOMLKitError

from personal_agent.config import Config
from personal_agent.validation import problems


class ConfigEditError(Exception):
    """The configuration was not changed."""


def set_section(path: Path, name: str, values: dict[str, Any]) -> None:
    """Validate the whole resulting document before atomically replacing the file."""
    temporary: str | None = None
    try:
        before = path.read_bytes() if path.exists() else None
        doc = (
            tomlkit.parse(before.decode("utf-8-sig")) if before is not None else tomlkit.document()
        )
        section = doc.get(name)
        if section is None:
            section = tomlkit.table()
            doc[name] = section
        for key, value in values.items():
            section[key] = value
        Config.model_validate(doc.unwrap())
        body = tomlkit.dumps(doc)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777 if path.exists() else 0o600)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if (path.read_bytes() if path.exists() else None) != before:
            raise ConfigEditError(
                "config.toml changed while it was being edited; run the command again"
            )
        os.replace(temporary, path)
    except ValidationError as exc:
        raise ConfigEditError(
            "; ".join(f"{where}: {what}" for where, what in problems(exc))
        ) from exc
    except (OSError, UnicodeError, TOMLKitError, TypeError) as exc:
        raise ConfigEditError(f"cannot update {path.name}: {exc}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
