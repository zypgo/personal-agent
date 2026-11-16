"""Telling a person what is wrong with something they wrote (a config file, a scenario, an edit).

Pydantic says where and why, and a validator's message may repeat what was typed ("got 'sk-...'").
Somebody who pastes a key into the wrong setting must not see it again on the terminal, in a log
or in a chat message, so whatever looks like a credential is hidden from every line, including
the name of the setting when the credential was used as a key.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from typing import Any

from pydantic import BaseModel, ValidationError

from personal_agent.security.redact import find_secrets, redact

HIDDEN = "[REDACTED]"  # what `redact` writes in place of a secret
BLOB_CHARS = (
    24  # a run of this many characters with no space in it is more likely a token than prose
)


def problems(exc: ValidationError, *, skip: Collection[str] = ()) -> list[tuple[str, str]]:
    """`(where, what)` for each thing wrong. `where` is the dotted path of the setting ("" at the
    top level) without the parts named in `skip`."""
    found = []
    for error in exc.errors(include_url=False, include_context=False):
        where = ".".join(
            HIDDEN if _looks_like_credential(str(part)) else str(part)
            for part in error["loc"]
            if part not in skip
        )
        what = str(error["msg"]).removeprefix("Value error, ")
        found.append((where, hide_credentials(what, error.get("input"))))
    return found


def hide_credentials(message: str, typed: Any = None) -> str:
    """`message` without anything that looks like a credential: by its shape, or because it is
    a long unbroken piece of what was `typed` (a key whose maker we do not know)."""
    blobs = sorted({text for text in _texts(typed) if _looks_like_credential(text)}, key=len)
    return redact(message, reversed(blobs))  # longest first, so a part never splits a whole


def _looks_like_credential(text: str) -> bool:
    return bool(find_secrets(text)) or (
        len(text) >= BLOB_CHARS and not any(ch.isspace() for ch in text)
    )


def _texts(value: Any) -> Iterator[str]:
    """Every piece of text somebody typed into `value`, however deeply it is nested.

    A loop over a stack, not a recursion: a file that is nothing but brackets must end in a
    message about the file, not in a `RecursionError`."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, BaseModel):
            pending.append(item.model_dump())
        elif isinstance(item, dict):
            for key, inner in item.items():
                pending.append(inner)
                pending.append(key)
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
