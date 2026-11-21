"""The vault: a folder of Markdown notes, read, written, searched and undone.

The folder is what Obsidian opens, so it is the truth: a note you change in Obsidian is what the
assistant reads next. The product keeps to its own corners of it and leaves the rest alone.

    wiki/      what has been learned, as notes that link to each other (the assistant writes here)
    raw/       what came in from outside, kept as it came and never changed afterwards
    threads/   one note for each task handed to another assistant
    daily/     a line or two for each day

Every change the assistant makes is remembered, so it can be undone.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import re
import stat
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any

from personal_agent.clock import Clock
from personal_agent.db import Database
from personal_agent.memory.store import Actor
from personal_agent.security.redact import find_secrets, injection_hints, redact
from personal_agent.security.sanitize import scrub_surrogates
from personal_agent.vault import note as md
from personal_agent.vault.index import OUTSIDE_ORIGINS, Entry, Hit, VaultIndex, origin_of

if TYPE_CHECKING:
    from personal_agent.config import VaultConfig

log = logging.getLogger(__name__)

AREAS = ("wiki", "raw", "threads", "daily")
_ASSISTANT = (Actor.AGENT, Actor.DREAM)
INDEX_NOTE = "wiki/index.md"
LOG_NOTE = "wiki/log.md"
GENERATED = frozenset({INDEX_NOTE, LOG_NOTE})
_GENERATED_ANY_CASE = frozenset(path.casefold() for path in GENERATED)
"""On a Mac `wiki/Log.md` is the file `wiki/log.md`, so the notes the product writes are
protected by name in any case."""
MAX_READ_BYTES = 2_000_000
MAX_DEPTH = 6
_RESERVED = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')
_LINK_UNSAFE = re.compile(r"[\[\]#^]")
NOTES_HEADING = "笔记"
"""The part of a thread note that is free for the assistant's own words."""


class VaultError(ValueError):
    """The message is for the person (or the model) who asked, in plain words."""


class VaultConflict(VaultError):
    """The note changed after it was read."""


class VaultMissing(VaultError):
    """There is no such note."""


@dataclass(frozen=True)
class Note:
    path: str
    title: str
    text: str
    body: str
    front: dict[str, Any]
    rev: str
    size: int
    updated: float
    type: str
    status: str
    origin: str
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    outgoing: tuple[str, ...]
    missing: tuple[str, ...]
    backlinks: tuple[str, ...]
    private: bool

    @property
    def outside(self) -> bool:
        """The words were not written by the owner or by this assistant: they are data."""
        return self.origin in ("agent", "web", "tainted") or self.path.startswith("raw/")


@dataclass(frozen=True)
class Issue:
    kind: str
    path: str
    detail: str


@dataclass(frozen=True)
class Written:
    path: str
    rev: str
    created: bool
    version: int | None
    notice: str = ""


def revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def clean_path(rel: str, *, strict: bool = False) -> str:
    """A note's path inside the vault, in one form: `folder/name.md`, Unicode-normalised."""
    if not isinstance(rel, str):
        raise VaultError("路径要写成文字。")
    raw = unicodedata.normalize("NFC", scrub_surrogates(rel)).strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise VaultError("路径要从知识库里面算起，比如 wiki/张三.md。")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise VaultError("没有写路径。")
    if len(parts) > MAX_DEPTH:
        raise VaultError(f"文件夹最多套 {MAX_DEPTH - 1} 层。")
    for part in parts:
        if part == ".." or part.startswith("."):
            raise VaultError("路径里不能有 .. 或者以点开头的名字。")
        if _RESERVED.search(part):
            raise VaultError("名字里有不能用的符号。")
        if strict and _LINK_UNSAFE.search(part):
            raise VaultError("名字里不要用 [ ] # ^，它们在 [[链接]] 里有特殊意思。")
        if len(part.encode("utf-8")) > 200:
            raise VaultError("名字太长了。")
    if not parts[-1].casefold().endswith(".md"):
        parts[-1] += ".md"
    return "/".join(parts)


class Vault:
    def __init__(
        self,
        root: Path,
        db: Database,
        cfg: VaultConfig,
        clock: Clock,
        *,
        lock_path: Path | None = None,
        tz: tzinfo = UTC,
    ) -> None:
        self.root = root
        self.db = db
        self.cfg = cfg
        self.clock = clock
        self.tz = tz
        self._lock_path = lock_path
        self._mutex = threading.RLock()
        self._depth = 0
        self.index = VaultIndex(root, max_files=cfg.max_files, exclude=cfg.exclude)

    # ---- paths -----------------------------------------------------------------------
    def exists(self) -> bool:
        return self.root.is_dir()

    def _full(self, rel: str) -> Path:
        """The file for a clean path, refusing to pass through a link out of the folder."""
        here = self.root
        for part in rel.split("/"):
            here = here / part
            if here.is_symlink():
                raise VaultError("知识库里的快捷方式（符号链接）不会被打开。")
        return here

    def _check_open(self, rel: str) -> None:
        if self.index.excludes(rel):
            raise VaultError("这个文件夹被设为不让助手看。")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """One writer at a time, across threads and across processes (the page, the daemon and a
        command may all write). Taking it again from inside is fine."""
        with self._mutex:
            fd: int | None = None
            if self._depth == 0 and self._lock_path is not None:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if fd is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    # ---- reading ---------------------------------------------------------------------
    def _read_file(self, rel: str) -> str | None:
        full = self._full(rel)
        try:
            st = full.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode):
            raise VaultError("这不是一篇笔记。")
        if st.st_size > MAX_READ_BYTES:
            raise VaultError("这篇笔记太大了，请在 Obsidian 里打开。")
        return full.read_text(encoding="utf-8", errors="replace")

    def read_text(self, rel: str) -> str | None:
        return self._read_file(clean_path(rel))

    def read(self, rel: str, *, actor: Actor = Actor.AGENT) -> Note:
        path = clean_path(rel)
        self._check_open(path)
        text = self._read_file(path)
        if text is None:
            raise VaultMissing(f"没有找到 {path}。")
        self.index.refresh()
        parsed = md.parse(text)
        entry = self.index.entry(path)
        private = bool(entry and entry.private) or md.declares_private(parsed)
        if private and actor in _ASSISTANT:
            raise VaultError("这篇笔记标了私密，助手不读。")
        stem = path.rsplit("/", 1)[-1].removesuffix(".md")
        outgoing, missing = self.index.outgoing(path)
        st = self._full(path).stat()
        return Note(
            path=path,
            title=md.title_of(parsed, stem),
            text=text,
            body=parsed.body,
            front=parsed.front,
            rev=revision(text),
            size=st.st_size,
            updated=st.st_mtime,
            type=str(parsed.front.get("type") or ""),
            status=str(parsed.front.get("status") or ""),
            origin=entry.origin if entry else str(parsed.front.get("origin") or "owner"),
            tags=tuple(md.tags_of(parsed)),
            aliases=tuple(md.as_list(parsed.front.get("aliases"))),
            outgoing=tuple(outgoing),
            missing=tuple(missing),
            backlinks=tuple(self.index.backlinks(path)),
            private=private,
        )

    def search(
        self,
        query: str = "",
        *,
        folder: str = "",
        tag: str = "",
        kind: str = "",
        status: str = "",
        limit: int = 8,
        actor: Actor = Actor.AGENT,
    ) -> list[Hit]:
        def read(path: str) -> str | None:
            try:
                return self._read_file(path)
            except (VaultError, OSError):
                return None

        return self.index.search(
            query,
            folder=folder,
            tag=tag,
            kind=kind,
            status=status,
            limit=limit,
            now=self.clock.now(),
            include_private=actor not in _ASSISTANT,
            read=read,
        )

    def entries(self) -> dict[str, Entry]:
        return self.index.refresh()

    def listing(self, folder: str = "", *, actor: Actor = Actor.USER) -> dict[str, Any]:
        """The folders and notes directly inside `folder`, for browsing."""
        entries = self.index.refresh()
        base = unicodedata.normalize("NFC", folder).strip("/")
        prefix = base + "/" if base else ""
        folders: dict[str, int] = {}
        notes: list[Entry] = []
        for path, entry in entries.items():
            if not path.startswith(prefix):
                continue
            if entry.generated or (entry.private and actor in _ASSISTANT):
                continue
            rest = path[len(prefix) :]
            if "/" in rest:
                name = rest.split("/", 1)[0]
                folders[name] = folders.get(name, 0) + 1
            else:
                notes.append(entry)
        notes.sort(key=lambda e: (-e.mtime_ns, e.path))
        return {
            "folder": base,
            "folders": [{"name": n, "count": c} for n, c in sorted(folders.items())],
            "notes": [
                {
                    "path": e.path,
                    "title": e.title,
                    "summary": e.summary,
                    "type": e.type,
                    "status": e.status,
                    "updated": e.updated,
                    "outside": e.outside,
                    "private": e.private,
                }
                for e in notes
            ],
        }

    def stats(self) -> dict[str, Any]:
        entries = self.index.refresh()
        counts = {area: 0 for area in AREAS}
        other = 0
        for path, entry in entries.items():
            if entry.generated:
                continue
            top = path.split("/")[0]
            if top in counts and "/" in path:
                counts[top] += 1
            else:
                other += 1
        return {
            "root": str(self.root),
            "exists": self.exists(),
            "notes": sum(1 for e in entries.values() if not e.generated),
            "areas": counts,
            "other": other,
            "skipped": self.index.skipped,
        }

    # ---- writing ---------------------------------------------------------------------
    def _allowed(self, path: str, action: str, actor: Actor) -> None:
        """What the assistant may do where. The owner (the web page) and the product itself may
        write anywhere inside the folder."""
        if actor in (Actor.USER, Actor.SYSTEM):
            return
        top = path.split("/")[0]
        if "/" not in path or top not in AREAS:
            raise VaultError("助手只写 wiki/、raw/、threads/、daily/ 这几个文件夹。")
        if path.casefold() in _GENERATED_ANY_CASE:
            raise VaultError("这个文件是系统自动生成的，会被覆盖。想补充内容请写在别的笔记里。")
        if top == "raw" and action != "create":
            raise VaultError("raw/ 里的原始资料只能新增，不能修改。")
        if top == "daily" and action not in ("create", "append"):
            raise VaultError("日记只能新增或在后面追加。")
        if top == "threads" and action != "append":
            raise VaultError("任务笔记里只能在「笔记」部分追加。")
        if actor == Actor.DREAM and (
            top not in ("wiki", "daily") or action in ("replace", "section")
        ):
            raise VaultError(
                "夜里的整理只在 wiki/ 和 daily/ 里新增笔记，或在自己写的笔记后面追加。"
            )

    def _check_text(self, text: str, actor: Actor) -> str:
        if len(text.encode("utf-8")) > self.cfg.max_note_bytes:
            raise VaultError(f"笔记太长了（上限 {self.cfg.max_note_bytes // 1024} KB），请拆开写。")
        if actor in _ASSISTANT:
            found = find_secrets(text)
            if found:
                kinds = "、".join(sorted({s.kind for s in found}))
                raise VaultError(
                    f"这段文字里像是有密钥或口令（{kinds}），不会写进笔记。请去掉后再写。"
                )
            hints = injection_hints(text)
            if hints:
                return "文字里有像是在给模型下命令的句子，读回来时只会当作资料，不会当作指令。"
        return ""

    def _write_file(self, full: Path, text: str) -> None:
        full.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".qixu-", suffix=".tmp", dir=full.parent)
        try:
            mode = stat.S_IMODE(full.stat().st_mode) if full.exists() else 0o644
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, mode)
            os.replace(temp, full)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        with contextlib.suppress(OSError):
            dir_fd = os.open(full.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _record(
        self,
        path: str,
        action: str,
        actor: Actor,
        run_id: str | None,
        old: str | None,
        new: str | None,
    ) -> int | None:
        return self.db.insert(
            """INSERT INTO vault_versions
               (path, action, actor, run_id, old_sha, new_sha, old_text, new_size, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                path,
                action,
                str(actor),
                run_id,
                revision(old) if old is not None else None,
                revision(new) if new is not None else None,
                old,
                len(new.encode("utf-8")) if new is not None else 0,
                self.clock.now(),
            ),
        )

    def _save(
        self,
        path: str,
        action: str,
        actor: Actor,
        run_id: str | None,
        old: str | None,
        new: str,
        *,
        history: bool,
    ) -> Written:
        full = self._full(path)
        new = scrub_surrogates(new)  # whatever came in, it can be written as UTF-8
        version = self._record(path, action, actor, run_id, old, new) if history else None
        try:
            self._write_file(full, new)
        except BaseException:
            if version is not None:
                self.db.execute("DELETE FROM vault_versions WHERE id=?", (version,))
            raise
        return Written(path, revision(new), old is None, version)

    def write(
        self,
        rel: str,
        text: str,
        *,
        mode: str = "create",
        actor: Actor = Actor.AGENT,
        run_id: str | None = None,
        rev: str | None = None,
        heading: str = "",
        origin: str | None = None,
    ) -> Written:
        """Make or change one note.

        `create` fails if the note exists. `append` adds to the end (or to the end of the section
        called `heading`). `section` replaces that section. `replace` rewrites the whole note and
        needs `rev`, the revision that was read, so that a change made in Obsidian meanwhile is
        never lost silently."""
        if mode not in ("create", "append", "section", "replace"):
            raise VaultError("mode 只能是 create、append、section 或 replace。")
        path = clean_path(rel, strict=True)
        self._check_open(path)
        self._allowed(path, mode, actor)
        text, heading = scrub_surrogates(text), scrub_surrogates(heading)
        if not text.strip() and mode in ("create", "append"):
            raise VaultError("没有要写的内容。")
        notice = self._check_text(text, actor)
        with self._locked():
            old = self._read_file(path)
            if old is not None and actor in _ASSISTANT and md.declares_private(md.parse(old)):
                raise VaultError("这篇笔记标了私密，助手不碰。")
            if mode == "create":
                if old is not None:
                    raise VaultError(f"{path} 已经有了。要改它请用 append、section 或 replace。")
                new = text
            elif old is None:
                if mode == "replace" or mode == "section":
                    raise VaultMissing(f"没有找到 {path}。")
                if actor in _ASSISTANT and path.split("/")[0] == "threads":
                    raise VaultMissing(f"没有找到 {path}。")
                new = text
            elif actor == Actor.DREAM and origin_of(md.parse(old).front, path) == "owner":
                raise VaultError("夜里的整理不动你写的笔记。新建一篇，用 [[链接]] 指向它就行。")
            elif mode == "replace":
                if rev is None:
                    raise VaultError("整篇改写要带上读到的 rev，免得盖掉你在 Obsidian 里的修改。")
                if rev != revision(old):
                    raise VaultConflict("这篇笔记在你读过之后又被改过了。请重新读一遍再改。")
                new = self._keep_properties(old, text) if actor in _ASSISTANT else text
            else:
                if rev is not None and rev != revision(old):
                    raise VaultConflict("这篇笔记在你读过之后又被改过了。请重新读一遍再改。")
                new = self._edit(old, mode, text, heading or "", path, actor)
            if actor in _ASSISTANT:
                new = self._settle(new, old, path, origin)
            elif old is None and origin:
                new = self._label(new, origin)
            if new == old:
                return Written(path, revision(new), False, None, "内容没有变化。")
            self._check_text(new, Actor.SYSTEM)
            result = self._save(path, mode, actor, run_id, old, new, history=True)
            if path.startswith(("wiki/", "raw/")):
                try:
                    self.note_change(path, "新增" if old is None else "改了", actor)
                except (VaultError, OSError):
                    # The note is saved; a line that could not be added to the log must not make
                    # the caller think it was not (and write it a second time).
                    log.warning("could not add %s to the log", path, exc_info=True)
        return Written(result.path, result.rev, result.created, result.version, notice)

    @staticmethod
    def _keep_properties(old: str, text: str) -> str:
        """A rewrite that carries no properties of its own keeps the note's (tags, aliases, status
        are the owner's work, and a model that left them out did not mean to delete them)."""
        before, after = md.parse(old), md.parse(text)
        if before.front_raw is None or after.front_raw is not None:
            return text
        return f"---\n{before.front_raw}\n---\n{text.lstrip(chr(10))}"

    def _label(self, text: str, origin: str) -> str:
        """Add `origin` to a new note's properties when it does not say whose words it holds."""
        parsed = md.parse(text)
        if parsed.front_error is not None or "origin" in parsed.front:
            return text
        if parsed.front_raw is None:
            return md.render({"origin": origin, "created": self._iso()}, parsed.body)
        return md.render({**parsed.front, "origin": origin}, parsed.body)

    def _settle(self, new: str, old: str | None, path: str, origin: str | None) -> str:
        """Settle whose words a note holds, for what the assistant writes. It cannot do that by
        writing it into the properties: a new note is the assistant's (a raw note is data from
        outside), an old note keeps whose it was, and a note that now holds words from outside (the
        run had read a web page or an agent's report) says so, even when it was the owner's."""
        parsed = md.parse(new)
        if parsed.front_error is not None:
            raise VaultError("文件开头 --- 之间的属性写得不对，请改好再写。")
        if old is not None:
            wanted = origin_of(md.parse(old).front, path)
        else:
            wanted = "web" if path.startswith("raw/") else "assistant"
        if origin in OUTSIDE_ORIGINS and wanted not in OUTSIDE_ORIGINS:
            wanted = origin
        front = {k: v for k, v in parsed.front.items() if k != "generated"}
        if "origin" in front or wanted != "owner":
            front["origin"] = wanted
        if front == parsed.front:
            return new
        if parsed.front_raw is None and old is None:
            front.setdefault("created", self._iso())
        return md.render(front, parsed.body)

    def _edit(self, old: str, mode: str, text: str, heading: str, path: str, actor: Actor) -> str:
        parsed = md.parse(old)
        if path.startswith("threads/") and actor in _ASSISTANT:
            heading = NOTES_HEADING
        if mode == "append" and not heading:
            body = parsed.body.rstrip("\n") + "\n\n" + text.strip("\n") + "\n"
        elif mode == "append":
            body = md.append_section(parsed.body, heading, text)
        else:
            if not heading:
                raise VaultError("section 要说明改哪个标题（heading）。")
            current = md.read_section(parsed.body, heading)
            if current is not None and current.strip() == text.strip():
                return old
            body = md.replace_section(parsed.body, heading, text)
        if parsed.front_raw is None:
            return body
        return f"---\n{parsed.front_raw}\n---\n{body}"

    # ---- system writes: notes the product keeps up to date ---------------------------
    def put(self, rel: str, text: str, *, history: bool = False) -> Written:
        """Write a note the product generates (index, thread, day). No area rules, no undo history."""
        path = clean_path(rel)
        text = scrub_surrogates(text)
        with self._locked():
            old = self._read_file(path)
            if old == text:
                return Written(path, revision(text), False, None)
            return self._save(path, "generate", Actor.SYSTEM, None, old, text, history=history)

    def edit(
        self,
        rel: str,
        *,
        front: dict[str, Any] | None = None,
        replace: dict[str, str] | None = None,
        append: dict[str, str] | None = None,
    ) -> Written | None:
        """Keep the parts of a note that the product owns up to date and leave the rest as it is:
        properties, and sections by heading. Returns None when the note is missing."""
        path = clean_path(rel)
        with self._locked():
            old = self._read_file(path)
            if old is None:
                return None
            parsed = md.parse(old)
            if parsed.front_error is not None:
                return None
            body = parsed.body
            for heading, content in (replace or {}).items():
                body = md.replace_section(body, heading, content)
            for heading, content in (append or {}).items():
                body = md.append_section(body, heading, content)
            merged = {**parsed.front, **(front or {})}
            new = md.render(merged, body) if (merged or parsed.front_raw is not None) else body
            if new == old:
                return Written(path, revision(old), False, None)
            return self._save(path, "generate", Actor.SYSTEM, None, old, new, history=False)

    # ---- captures --------------------------------------------------------------------
    def capture(
        self,
        *,
        folder: str,
        title: str,
        body: str,
        key: str,
        front: dict[str, Any],
        day: str,
    ) -> tuple[str, bool]:
        """Keep something that came from outside, as it came: `raw/<folder>/<day>-<title>-<key>.md`.

        The same `key` always names the same note, so saving twice saves once. Credentials in the
        text are cut out. Returns the note's path and whether it was new."""
        title, body = scrub_surrogates(title), scrub_surrogates(body)
        slug = md.slugify(title, limit=36)
        short = re.sub(r"[^A-Za-z0-9]", "", key)[-8:] or revision(key)[:8]
        parts = [md.slugify(p, limit=40) for p in folder.split("/") if p]
        path = clean_path("/".join(["raw", *parts, f"{day}-{slug}-{short}"]), strict=True)
        with self._locked():
            if self._read_file(path) is not None:
                return path, False
            text = redact(body)
            limit = self.cfg.max_note_bytes - 2_048
            if len(text.encode("utf-8")) > limit:
                text = text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
                text += "\n\n（太长，后面的没有保存。）"
            front = {"title": title.strip()[:200], "type": "capture", "origin": "agent", **front}
            content = md.render(front, f"# {title.strip()[:200]}\n\n{text.strip()}\n")
            self._save(path, "capture", Actor.SYSTEM, None, None, content, history=False)
        return path, True

    # ---- the log and the notes that follow along -------------------------------------
    def note_change(self, path: str, what: str, actor: Actor) -> None:
        """One line in `wiki/log.md` for a change someone made, newest at the bottom."""
        if path in GENERATED:
            return
        who = {Actor.USER: "你", Actor.DREAM: "夜里整理"}.get(actor, "栖序")
        self.log_line(what, f"[[{path.removesuffix('.md')}]] ({who})")

    def log_line(self, what: str, detail: str) -> None:
        """One dated line in `wiki/log.md`."""
        self.append_log(f"## [{self._stamp()}] {what} | {detail}")

    def append_log(self, line: str) -> None:
        line = scrub_surrogates(line)
        with self._locked():
            old = self._read_file(LOG_NOTE)
            if old is None:
                head = md.render(
                    {"type": "log", "origin": "system", "generated": True},
                    "# 记录\n\n> 栖序在这里按时间记下知识库里发生的事，新的在最下面。这个文件会自动追加。\n\n",
                )
                old = head
            new = old.rstrip("\n") + "\n" + line.strip() + "\n"
            if len(new.encode("utf-8")) > self.cfg.max_note_bytes:
                lines = new.split("\n")
                header = lines[:6]
                new = "\n".join(header + lines[-400:]) + "\n"
            self._save(LOG_NOTE, "generate", Actor.SYSTEM, None, None, new, history=False)

    # ---- history and undo ------------------------------------------------------------
    def history(self, rel: str, *, limit: int = 20) -> list[dict[str, Any]]:
        path = clean_path(rel)
        rows = self.db.query(
            """SELECT id, action, actor, run_id, old_text, created_at FROM vault_versions
               WHERE path=? AND action != 'generate' ORDER BY id DESC LIMIT ?""",
            (path, max(1, min(limit, 100))),
        )
        return [
            {
                "id": row["id"],
                "action": row["action"],
                "actor": row["actor"],
                "created_at": row["created_at"],
                "before": row["old_text"],
                "restorable": row["old_text"] is not None,
            }
            for row in rows
        ]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """The latest changes to notes, newest first, each with the run that made it (so that a
        whole run can be undone with `revert_run`)."""
        rows = self.db.query(
            """SELECT id, path, action, actor, run_id, created_at FROM vault_versions
               WHERE action != 'generate' ORDER BY id DESC LIMIT ?""",
            (max(1, min(limit, 200)),),
        )
        return [dict(row) for row in rows]

    def restore(self, version_id: int, *, actor: Actor = Actor.USER) -> str:
        """Put a note back the way it was before one change."""
        row = self.db.one("SELECT * FROM vault_versions WHERE id=?", (version_id,))
        if row is None:
            raise VaultError("没有这个版本。")
        if row["old_text"] is None:
            raise VaultError("这一版是新建笔记，没有「之前」可以恢复。")
        path = str(row["path"])
        with self._locked():
            current = self._read_file(path)
            self._save(path, "restore", actor, None, current, str(row["old_text"]), history=True)
        return path

    def revert_run(self, run_id: str) -> tuple[list[str], list[str]]:
        """Undo everything one run changed. A note someone edited since is left as it is and
        reported. Returns `(undone, left alone)`."""
        rows = self.db.query(
            "SELECT * FROM vault_versions WHERE run_id=? AND action != 'revert' ORDER BY id DESC",
            (run_id,),
        )
        undone: list[str] = []
        skipped: list[str] = []
        with self._locked():
            for row in rows:
                path = str(row["path"])
                current = self._read_file(path)
                if (revision(current) if current is not None else None) != row["new_sha"]:
                    skipped.append(path)
                    continue
                if row["old_text"] is None:
                    if current is not None:
                        self._record(path, "revert", Actor.SYSTEM, None, current, None)
                        self._full(path).unlink()
                else:
                    self._save(
                        path,
                        "revert",
                        Actor.SYSTEM,
                        None,
                        current,
                        str(row["old_text"]),
                        history=True,
                    )
                undone.append(path)
        return undone, skipped

    def changes_by_run(self, run_id: str) -> list[dict[str, Any]]:
        return [
            {"path": row["path"], "action": row["action"]}
            for row in self.db.query(
                "SELECT path, action FROM vault_versions WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]

    def prune_history(self) -> int:
        days = self.cfg.history_days
        if days <= 0:
            return 0
        cutoff = self.clock.now() - days * 86400
        with self.db.tx():
            cursor = self.db.execute("DELETE FROM vault_versions WHERE created_at < ?", (cutoff,))
        return cursor.rowcount

    # ---- checking --------------------------------------------------------------------
    def lint(self) -> list[Issue]:
        """Things in the notes worth a look. It only reports; it changes nothing."""
        entries = self.index.refresh()
        issues: list[Issue] = []
        linked_from_wiki: set[str] = set()
        for path, entry in entries.items():
            if entry.generated:
                continue
            if entry.front_error:
                issues.append(Issue("properties", path, entry.front_error))
            if entry.truncated:
                issues.append(Issue("large", path, "太长，只检查了前面一部分"))
            found, missing = self.index.outgoing(path)
            for target in missing:
                issues.append(Issue("broken_link", path, f"[[{target}]] 找不到对应的笔记"))
            if path.startswith("wiki/"):
                linked_from_wiki.update(found)
        titles: dict[str, str] = {}
        for path, entry in sorted(entries.items()):
            if not path.startswith("wiki/") or path in GENERATED:
                continue
            key = entry.title.casefold()
            if key in titles:
                issues.append(Issue("duplicate", path, f"和 {titles[key]} 标题相同"))
            else:
                titles[key] = path
            if not self.index.backlinks(path):
                issues.append(Issue("orphan", path, "没有别的笔记链接到它"))
        for path, entry in sorted(entries.items()):
            if path.startswith("raw/") and path not in linked_from_wiki and not entry.private:
                issues.append(Issue("uncompiled", path, "还没有整理进 wiki/"))
        if self.index.skipped:
            issues.append(Issue("skipped", "", f"笔记太多，有 {self.index.skipped} 篇旧的没有检查"))
        return issues

    def secrets_in_notes(self) -> list[str]:
        found = []
        for path in self.index.refresh():
            try:
                text = self._read_file(path)
            except (VaultError, OSError):
                continue
            if text and find_secrets(text):
                found.append(path)
        return found

    def backlog(self, limit: int = 20) -> list[Entry]:
        """Captured material nothing in `wiki/` points to yet, oldest first."""
        entries = self.index.refresh()
        linked: set[str] = set()
        for path, entry in entries.items():
            if path.startswith("wiki/") and not entry.generated:
                linked.update(self.index.outgoing(path)[0])
        waiting = [
            e
            for p, e in entries.items()
            if p.startswith("raw/") and p not in linked and not e.private
        ]
        waiting.sort(key=lambda e: (e.mtime_ns, e.path))
        return waiting[:limit]

    # ---- setting up ------------------------------------------------------------------
    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _iso(self) -> str:
        return datetime.fromtimestamp(self.clock.now(), self.tz).isoformat(timespec="seconds")

    def _stamp(self) -> str:
        return datetime.fromtimestamp(self.clock.now(), self.tz).strftime("%Y-%m-%d %H:%M")
