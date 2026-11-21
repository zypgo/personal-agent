"""Finding things in a folder of notes: a search that reads Chinese, and the graph of `[[links]]`.

The folder is the truth and this is only a view of it, rebuilt from the files. Each call looks at
every file's size and modification time (cheap) and reads again only what changed, so a note you
edited in Obsidian a second ago is found. Nothing is stored anywhere else.

Chinese has no spaces, so text is indexed one character and two characters at a time. A query word
must have all its pairs in a note, and the words themselves must really occur there.
"""

from __future__ import annotations

import math
import os
import re
import stat
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from personal_agent.vault import note as md

OUTSIDE_ORIGINS = frozenset({"agent", "web", "tainted"})
"""Where a note's words came from, when the owner or this assistant did not write them."""

INDEX_BYTES = 64 * 1024
"""How much of each note is read for searching. The rest is still there when the note is opened."""

_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
_TOKEN = re.compile(rf"([{_CJK}]+)|([^\W_{_CJK}]+)")
_HAS_CJK = re.compile(f"[{_CJK}]")


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def tokens(text: str) -> list[str]:
    """Words, and for Chinese, characters and pairs of characters."""
    found: list[str] = []
    for match in _TOKEN.finditer(fold(text)):
        run = match.group(1)
        if run:
            found.extend(run)
            found.extend(run[i : i + 2] for i in range(len(run) - 1))
        else:
            found.append(match.group(2))
    return found


def _required(word: str) -> list[str]:
    """What a note must contain to match one query word."""
    need: list[str] = []
    for match in _TOKEN.finditer(fold(word)):
        run = match.group(1)
        if run:
            need.extend([run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)])
        else:
            need.append(match.group(2))
    return need


@dataclass
class Entry:
    path: str
    mtime_ns: int
    size: int
    title: str
    summary: str
    type: str
    status: str
    origin: str
    private: bool
    generated: bool
    """Written by the product (the index, the log). Its links do not count as links."""
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    links: tuple[md.Link, ...]
    headings: tuple[str, ...]
    terms: dict[str, int]
    title_terms: frozenset[str]
    label_terms: frozenset[str]
    """Tags, aliases and headings."""
    front_error: str | None
    truncated: bool

    @property
    def stem(self) -> str:
        return self.path.rsplit("/", 1)[-1].removesuffix(".md")

    @property
    def updated(self) -> float:
        return self.mtime_ns / 1e9

    @property
    def outside(self) -> bool:
        return self.origin in OUTSIDE_ORIGINS


@dataclass(frozen=True)
class Hit:
    path: str
    title: str
    type: str
    status: str
    origin: str
    tags: tuple[str, ...]
    updated: float
    score: float
    excerpt: str


def origin_of(front: dict[str, Any], path: str) -> str:
    """Whose words a note holds: what its properties say, else the folder's default."""
    value = front.get("origin")
    if isinstance(value, str) and value.strip():
        return value.strip().casefold()
    if path.startswith("raw/"):
        return "web"
    return "owner"


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "yes", "1", "是"}
    return bool(value)


def _entry(path: str, st: os.stat_result, data: bytes) -> Entry:
    truncated = len(data) > INDEX_BYTES
    text = data[:INDEX_BYTES].decode("utf-8", errors="replace")
    parsed = md.parse(text)
    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    title = md.title_of(parsed, stem)
    heads = tuple(h.text for h in md.headings(parsed.body))
    tags = tuple(md.tags_of(parsed))
    aliases = tuple(md.as_list(parsed.front.get("aliases")) + md.as_list(parsed.front.get("alias")))
    terms = Counter(tokens(parsed.body))
    return Entry(
        path=path,
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        title=title,
        summary=md.summary_of(parsed),
        type=str(parsed.front.get("type") or "").strip(),
        status=str(parsed.front.get("status") or "").strip(),
        origin=origin_of(parsed.front, path),
        private=_truthy(parsed.front.get("private")),
        generated=_truthy(parsed.front.get("generated")),
        tags=tags,
        aliases=aliases,
        links=tuple(md.links(parsed)),
        headings=heads,
        terms=dict(terms),
        title_terms=frozenset(tokens(title + " " + stem)),
        label_terms=frozenset(tokens(" ".join((*tags, *aliases, *heads)))),
        front_error=parsed.front_error,
        truncated=truncated,
    )


def relative(root: Path, full: Path) -> str:
    return unicodedata.normalize("NFC", full.relative_to(root).as_posix())


@dataclass
class _Names:
    by_stem: dict[str, list[str]] = field(default_factory=dict)
    by_path: dict[str, str] = field(default_factory=dict)
    by_alias: dict[str, list[str]] = field(default_factory=dict)


class VaultIndex:
    def __init__(
        self,
        root: Path,
        *,
        max_files: int = 5_000,
        exclude: Sequence[str] = (),
    ) -> None:
        self.root = root
        self.max_files = max_files
        self.exclude = frozenset(unicodedata.normalize("NFC", e).strip("/") for e in exclude)
        self._entries: dict[str, Entry] = {}
        self._names = _Names()
        self._backlinks: dict[str, list[str]] = {}
        self._df: Counter[str] = Counter()
        self.skipped = 0
        """Notes left out because the folder holds more than `max_files` of them."""

    # ---- scanning --------------------------------------------------------------------
    def refresh(self) -> dict[str, Entry]:
        if not self.root.is_dir():
            self._entries, self._names, self._backlinks, self.skipped = {}, _Names(), {}, 0
            self._df = Counter()
            return self._entries
        found: list[tuple[int, str, Path, os.stat_result]] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            here = Path(dirpath)
            rel_dir = "" if here == self.root else relative(self.root, here)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not d.startswith(".")
                and unicodedata.normalize("NFC", f"{rel_dir}/{d}".lstrip("/")) not in self.exclude
            )
            for name in filenames:
                if name.startswith(".") or not name.endswith(".md"):
                    continue
                full = here / name
                try:
                    st = full.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                found.append((st.st_mtime_ns, relative(self.root, full), full, st))
        found.sort(key=lambda item: (-item[0], item[1]))
        self.skipped = max(0, len(found) - self.max_files)
        keep = found[: self.max_files]
        fresh: dict[str, Entry] = {}
        changed = False
        for _, rel, full, st in keep:
            old = self._entries.get(rel)
            if old is not None and old.mtime_ns == st.st_mtime_ns and old.size == st.st_size:
                fresh[rel] = old
                continue
            try:
                with full.open("rb") as handle:
                    data = handle.read(INDEX_BYTES + 1)
            except OSError:
                continue
            fresh[rel] = _entry(rel, st, data)
            changed = True
        if set(fresh) != set(self._entries):
            changed = True
        self._entries = fresh
        if changed:
            self._rebuild_maps()
        return self._entries

    def _rebuild_maps(self) -> None:
        names = _Names()
        for path, entry in sorted(self._entries.items()):
            names.by_stem.setdefault(fold(entry.stem), []).append(path)
            names.by_path[fold(path.removesuffix(".md"))] = path
            for alias in entry.aliases:
                names.by_alias.setdefault(fold(alias), []).append(path)
        for paths in names.by_stem.values():
            paths.sort(key=lambda p: (p.count("/"), p))
        self._names = names
        df: Counter[str] = Counter()
        for entry in self._entries.values():
            df.update(entry.terms.keys())
        self._df = df
        back: dict[str, list[str]] = {}
        for path, entry in self._entries.items():
            if entry.generated:
                continue
            seen: set[str] = set()
            for link in entry.links:
                target = self.resolve(link.target, path)
                if target and target != path and target not in seen:
                    seen.add(target)
                    back.setdefault(target, []).append(path)
        for sources in back.values():
            sources.sort()
        self._backlinks = back

    # ---- links -----------------------------------------------------------------------
    def resolve(self, target: str, source: str = "") -> str | None:
        """The note a link points at: by name anywhere in the folder, or by path."""
        name = fold(target.strip()).removesuffix(".md")
        if not name:
            return None
        if "/" in name:
            direct = self._names.by_path.get(name)
            if direct:
                return direct
            tail = "/" + name
            for key, path in self._names.by_path.items():
                if key.endswith(tail):
                    return path
            return None
        found = self._names.by_stem.get(name)
        if found:
            return found[0]
        by_alias = self._names.by_alias.get(name)
        return by_alias[0] if by_alias else None

    def entry(self, path: str) -> Entry | None:
        return self._entries.get(path)

    def backlinks(self, path: str) -> list[str]:
        return list(self._backlinks.get(path, []))

    def outgoing(self, path: str) -> tuple[list[str], list[str]]:
        """`(notes this one links to, link targets that match no note)`."""
        entry = self._entries.get(path)
        if entry is None:
            return [], []
        found: list[str] = []
        missing: list[str] = []
        for link in entry.links:
            if _is_attachment(link.target):
                continue
            target = self.resolve(link.target, path)
            if target is None:
                if link.target not in missing:
                    missing.append(link.target)
            elif target not in found and target != path:
                found.append(target)
        return found, missing

    # ---- searching -------------------------------------------------------------------
    def search(
        self,
        query: str = "",
        *,
        folder: str = "",
        tag: str = "",
        kind: str = "",
        status: str = "",
        limit: int = 8,
        now: float | None = None,
        include_private: bool = False,
        read: Any = None,
    ) -> list[Hit]:
        """Notes that match every word of the query, best first. Without a query: the newest.

        `read(path)` returns a note's text (for excerpts and for checking Chinese words)."""
        entries = self.refresh()
        folder = unicodedata.normalize("NFC", folder).strip("/")
        pool = [
            e
            for e in entries.values()
            if not e.generated
            and (include_private or not e.private)
            and (not folder or e.path == folder or e.path.startswith(folder + "/"))
            and (not tag or _has_tag(e, tag))
            and (not kind or e.type.casefold() == kind.casefold())
            and (not status or e.status.casefold() == status.casefold())
        ]
        words = [w for w in query.split() if w]
        needs = [(w, _required(w)) for w in words]
        needs = [(w, n) for w, n in needs if n]
        if not needs:
            pool.sort(key=lambda e: (-e.mtime_ns, e.path))
            return [self._hit(e, 0.0, "", read) for e in pool[: max(1, limit)]]
        total = max(1, len(entries))
        df = self._df
        scored: list[tuple[float, Entry]] = []
        for e in pool:
            score = 0.0
            for _word, need in needs:
                if any(
                    t not in e.terms and t not in e.title_terms and t not in e.label_terms
                    for t in need
                ):
                    score = -1.0
                    break
                idf = math.log(1 + total / (1 + min(max(df[t], 1) for t in need)))
                tf = min(e.terms.get(t, 0) for t in need)
                score += idf * (tf / (tf + 1.5))
                if all(t in e.title_terms for t in need):
                    score += 2.0 * idf
                if all(t in e.label_terms for t in need):
                    score += 1.2 * idf
            if score < 0:
                continue
            if now is not None:
                age_days = max(0.0, (now - e.updated) / 86400)
                score *= 1 + 0.25 * math.exp(-age_days / 60)
            scored.append((score, e))
        scored.sort(key=lambda item: (-item[0], -item[1].mtime_ns, item[1].path))
        hits: list[Hit] = []
        for score, e in scored:
            text = read(e.path) if read is not None else None
            if text is not None:
                folded = fold(text) + " " + fold(e.title) + " " + fold(" ".join(e.aliases))
                if any(_HAS_CJK.search(w) and fold(w) not in folded for w, _ in needs):
                    continue
            hits.append(self._hit(e, score, query, read, text))
            if len(hits) >= max(1, limit):
                break
        return hits

    def _hit(self, e: Entry, score: float, query: str, read: Any, text: str | None = None) -> Hit:
        if text is None and read is not None and query:
            text = read(e.path)
        excerpt = _excerpt(text, query) if text and query else e.summary
        return Hit(e.path, e.title, e.type, e.status, e.origin, e.tags, e.updated, score, excerpt)


def _is_attachment(target: str) -> bool:
    name = target.rsplit("/", 1)[-1]
    return "." in name and not name.casefold().endswith(".md")


def _has_tag(entry: Entry, tag: str) -> bool:
    wanted = tag.lstrip("#").casefold()
    return any(t.casefold() == wanted or t.casefold().startswith(wanted + "/") for t in entry.tags)


def _excerpt(text: str, query: str, *, before: int = 50, after: int = 110) -> str:
    body = md.parse(text).body
    flat = re.sub(r"[ \t]+", " ", body)
    folded = flat.casefold()
    position = -1
    for word in query.split():
        found = folded.find(word.casefold())
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return md.summary_of(md.parse(text))
    start = max(0, position - before)
    end = min(len(flat), position + after)
    piece = re.sub(r"\s*\n\s*", " ", flat[start:end]).strip()
    return ("…" if start else "") + piece + ("…" if end < len(flat) else "")
