"""The old 资料 documents, brought into the knowledge base.

Before the knowledge base, a project kept flat Markdown documents in the assistant's memory.
They are copied into `wiki/` once, and what is saved to them afterwards is copied across too, so
the knowledge base is the one place to look. The originals are left where they were.
"""

from __future__ import annotations

import logging

from personal_agent.clock import Clock
from personal_agent.db import Database
from personal_agent.memory.store import Actor
from personal_agent.vault.store import Vault, VaultError, revision

log = logging.getLogger(__name__)

FLAG = "vault.legacy_import"
PREFIX = "/memories/state/knowledge/"


def _text(value: object) -> str:
    return bytes(value).decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def bring_over(db: Database, vault: Vault, clock: Clock) -> list[str]:
    """Copy the old documents into `wiki/`, once. Returns the notes that were made.

    A note that is already there is left alone, and so is a document that does not fit (a name
    the knowledge base would not take, or a size over the limit); the log says which."""
    if db.kv_get(FLAG) is not None:
        return []
    rows = db.query(
        "SELECT path, content FROM memory_documents WHERE path LIKE ? AND content IS NOT NULL"
        " ORDER BY path",
        (PREFIX + "%",),
    )
    made: list[str] = []
    left: list[str] = []
    for row in rows:
        name = str(row["path"])[len(PREFIX) :]
        target = f"wiki/{name}"
        try:
            if vault.read_text(target) is not None:
                left.append(name)
                continue
            vault.write(target, _text(row["content"]), mode="create", actor=Actor.USER)
        except (VaultError, OSError) as exc:
            log.warning("could not bring %s into the knowledge base: %s", name, exc)
            left.append(name)
            continue
        made.append(target)
    if made or left:
        detail = f"旧的「资料」{len(made)} 篇搬进了 wiki/"
        if left:
            detail += f"，{len(left)} 篇没有搬（已经有同名的笔记，或者太大）：" + "、".join(
                left[:8]
            )
        vault.log_line("搬家", detail)
    db.kv_set(FLAG, str(len(made)), now=clock.now())
    return made


def mirror(vault: Vault, name: str, text: str) -> None:
    """A document saved the old way is saved in `wiki/` too. Never fails the save."""
    target = f"wiki/{name}"
    try:
        current = vault.read_text(target)
        if current is None:
            vault.write(target, text, mode="create", actor=Actor.USER)
        elif current != text:
            vault.write(target, text, mode="replace", actor=Actor.USER, rev=revision(current))
    except (VaultError, OSError) as exc:
        log.warning("could not copy %s into the knowledge base: %s", name, exc)
