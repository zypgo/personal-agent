"""What the browser is shown of the knowledge base: notes, listings, history and checks.

Pure shaping of what the vault returns, kept apart from the request handling so that the same
views serve the page and the command line.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import quote

from personal_agent.vault.index import OUTSIDE_ORIGINS, Hit
from personal_agent.vault.store import GENERATED, Note, Vault

HISTORY_TEXT_CHARS = 20_000
LIST_LIMIT = 40
ISSUE_LIMIT = 200


def is_generated(vault: Vault, path: str) -> bool:
    """Notes the product writes again by itself: whatever is typed into them is lost."""
    entry = vault.entries().get(path)
    return path in GENERATED or bool(entry and entry.generated)


def obsidian_link(vault: Vault, path: str) -> str:
    return "obsidian://open?path=" + quote(str(vault.root / path), safe="")


def _linked(vault: Vault, paths: tuple[str, ...]) -> list[dict[str, str]]:
    entries = vault.entries()
    return [{"path": p, "title": entries[p].title if p in entries else p} for p in paths]


def note_view(vault: Vault, note: Note, *, local: bool) -> dict[str, Any]:
    """`local` is true when the browser may be told where the folder is on this machine."""
    return {
        "path": note.path,
        "title": note.title,
        "text": note.text,
        "rev": note.rev,
        "type": note.type,
        "status": note.status,
        "origin": note.origin,
        "outside": note.outside,
        "generated": is_generated(vault, note.path),
        "tags": list(note.tags),
        "aliases": list(note.aliases),
        "outgoing": _linked(vault, note.outgoing),
        "missing": list(note.missing),
        "backlinks": _linked(vault, note.backlinks),
        "updated": note.updated,
        "size": note.size,
        "obsidian": obsidian_link(vault, note.path) if local else None,
    }


def hit_view(hit: Hit) -> dict[str, Any]:
    return {
        "path": hit.path,
        "title": hit.title,
        "summary": hit.excerpt,
        "type": hit.type,
        "status": hit.status,
        "updated": hit.updated,
        "outside": hit.origin in OUTSIDE_ORIGINS or hit.path.startswith("raw/"),
        "tags": list(hit.tags),
    }


def notes_view(
    vault: Vault, *, folder: str, query: str, tag: str, kind: str, status: str
) -> dict[str, Any]:
    from personal_agent.memory.store import Actor

    if query or tag or kind or status:
        hits = vault.search(
            query,
            folder=folder,
            tag=tag,
            kind=kind,
            status=status,
            limit=LIST_LIMIT,
            actor=Actor.USER,
        )
        return {
            "folder": folder,
            "query": query,
            "folders": [],
            "notes": [hit_view(h) for h in hits],
        }
    listing = vault.listing(folder, actor=Actor.USER)
    return {
        "folder": listing["folder"],
        "query": "",
        "folders": listing["folders"],
        "notes": listing["notes"],
    }


def status_view(vault: Vault, *, max_note_bytes: int, local: bool) -> dict[str, Any]:
    stats = vault.stats()
    counts = Counter(issue.kind for issue in vault.lint())
    return {
        "enabled": True,
        "exists": stats["exists"],
        "root": stats["root"] if local else None,
        "notes": stats["notes"],
        "areas": stats["areas"],
        "other": stats["other"],
        "skipped": stats["skipped"],
        "backlog": len(vault.backlog(500)),
        "issues": dict(counts),
        "max_note_bytes": max_note_bytes,
    }


def history_view(vault: Vault, path: str) -> dict[str, Any]:
    versions = []
    for row in vault.history(path, limit=20):
        before = row["before"]
        text = before if isinstance(before, str) else ""
        versions.append(
            {
                "id": row["id"],
                "action": row["action"],
                "actor": row["actor"],
                "created_at": row["created_at"],
                "restorable": row["restorable"],
                "text": text[:HISTORY_TEXT_CHARS],
                "truncated": len(text) > HISTORY_TEXT_CHARS,
            }
        )
    return {"path": path, "versions": versions}


def issues_view(vault: Vault) -> dict[str, Any]:
    issues = vault.lint()
    return {
        "issues": [
            {"kind": i.kind, "path": i.path, "detail": i.detail} for i in issues[:ISSUE_LIMIT]
        ],
        "counts": dict(Counter(i.kind for i in issues)),
    }
