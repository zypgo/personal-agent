"""Mutations for iteration 5: a new conversation per wake-up, past messages and the task index.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/iteration5_fresh_sessions.py
Format and meaning: scripts/mutation/run.py.
"""

# ruff: noqa

RU = "src/personal_agent/harness/runner.py"
TR = "src/personal_agent/core/transcript.py"
IN = "src/personal_agent/harness/instructions.py"
TK = "src/personal_agent/memory/tasks.py"
SE = "src/personal_agent/core/sessions.py"
ST = "src/personal_agent/memory/store.py"
JO = "src/personal_agent/memory/journal.py"

TF = ["tests/test_fresh_sessions.py", "tests/test_recent_messages.py", "tests/test_task_index.py"]
TT = ["tests/test_recent_messages.py", "tests/test_task_index.py", "tests/test_instructions.py"]
TTJ = [*TT, "tests/test_memory_journal.py"]

M = [
    # ---- who starts a new conversation ---------------------------------------------------
    (
        "ru new: approval answer starts over",
        RU,
        "(Kind.MESSAGE, Kind.APPROVAL_RESPONSE)",
        "(Kind.MESSAGE,)",
        TF,
    ),
    (
        "ru new: message does not continue",
        RU,
        "        if any(e.kind in (Kind.MESSAGE, Kind.APPROVAL_RESPONSE) for e in batch.events):\n            return False\n        return batch.session_key.startswith(cls.OWN_SESSION_PREFIXES)",
        "        return batch.session_key.startswith(cls.OWN_SESSION_PREFIXES)",
        TF,
    ),
    (
        "ru new: everything starts over",
        RU,
        "        return batch.session_key.startswith(cls.OWN_SESSION_PREFIXES)",
        "        return True",
        TF,
    ),
    (
        "ru new: nothing starts over",
        RU,
        "        return batch.session_key.startswith(cls.OWN_SESSION_PREFIXES)",
        "        return False",
        TF,
    ),
    (
        "ru new: timers share",
        RU,
        'OWN_SESSION_PREFIXES = ("timer:", "webhook:", "system:")',
        'OWN_SESSION_PREFIXES = ("webhook:", "system:")',
        TF,
    ),
    (
        "ru new: webhooks share",
        RU,
        'OWN_SESSION_PREFIXES = ("timer:", "webhook:", "system:")',
        'OWN_SESSION_PREFIXES = ("timer:", "system:")',
        TF,
    ),
    (
        "ru new: system events share",
        RU,
        'OWN_SESSION_PREFIXES = ("timer:", "webhook:", "system:")',
        'OWN_SESSION_PREFIXES = ("timer:", "webhook:")',
        TF,
    ),
    (
        "ru new: retry wipes its own work",
        RU,
        "        if (\n            len(fresh) == len(batch.events)\n            and self.starts_new_session(batch)",
        "        if (\n            self.starts_new_session(batch)",
        TF,
    ),
    (
        "ru new: waiting approval wiped",
        RU,
        "            and not app.approvals.suspended_call_ids(key)\n            and not app.sessions.is_blank(key)\n        ):",
        "            and not app.sessions.is_blank(key)\n        ):",
        TF,
    ),
    (
        "ru new: blank still marked",
        RU,
        "            and not app.sessions.is_blank(key)\n        ):\n            app.sessions.clear(key)",
        "        ):\n            app.sessions.clear(key)",
        TF,
    ),
    (
        "ru new: never cleared",
        RU,
        "            app.sessions.clear(key)\n        elif len(fresh) == len(batch.events):",
        "            pass\n        elif len(fresh) == len(batch.events):",
        TF,
    ),
    (
        "ru new: compaction on a retry",
        RU,
        "        elif len(fresh) == len(batch.events):",
        "        else:",
        ["tests/test_fresh_sessions.py", "tests/test_compaction.py"],
    ),
    # ---- what the owner was last told ----------------------------------------------------
    (
        "ru recent: recited to the owner",
        RU,
        "        if any(e.kind == Kind.MESSAGE for e in batch.events):\n            return []\n        if not self.app.sessions.is_blank(batch.session_key):",
        "        if not self.app.sessions.is_blank(batch.session_key):",
        TF,
    ),
    (
        "ru recent: given to ongoing work",
        RU,
        "        if not self.app.sessions.is_blank(batch.session_key):\n            return []\n        return recent_messages_block(",
        "        return recent_messages_block(",
        TF,
    ),
    (
        "ru recent: one too many",
        RU,
        "self.app.transcript.recent_messages(cfg.recent_messages),",
        "self.app.transcript.recent_messages(cfg.recent_messages + 1),",
        TF,
    ),
    (
        "ru recent: length ignored",
        RU,
        "            self.app.tz,\n            cfg.recent_message_chars,",
        "            self.app.tz,\n            100000,",
        TF,
    ),
    (
        "ru recent: never given",
        RU,
        "            recent=self._recently_sent(batch),",
        "            recent=[],",
        TF,
    ),
    ("ru tasks: never listed", RU, "tasks=app.memory.load_task_index(),", 'tasks="",', TF),
    (
        "tr recent: what the owner said",
        TR,
        "WHERE direction = 'out' AND kind = 'message'",
        "WHERE direction = 'in' AND kind = 'message'",
        TF,
    ),
    (
        "tr recent: notices too",
        TR,
        "WHERE direction = 'out' AND kind = 'message'",
        "WHERE direction = 'out'",
        TF,
    ),
    (
        "tr recent: the oldest ones",
        TR,
        'ORDER BY id DESC LIMIT ?""",\n            (limit,),\n        )\n        return [_turn(r) for r in reversed(rows)]',
        'ORDER BY id ASC LIMIT ?""",\n            (limit,),\n        )\n        return [_turn(r) for r in reversed(rows)]',
        TF,
    ),
    (
        "tr recent: newest first",
        TR,
        "            (limit,),\n        )\n        return [_turn(r) for r in reversed(rows)]\n\n    def last_id",
        "            (limit,),\n        )\n        return [_turn(r) for r in rows]\n\n    def last_id",
        TF,
    ),
    (
        "in recent: block with nothing in it",
        IN,
        '    if not turns:\n        return []\n    lines = [\n        "<recently_sent>",',
        '    lines = [\n        "<recently_sent>",',
        TT,
    ),
    (
        "in recent: not one line",
        IN,
        'text = " ".join(strip_invisible(turn.text).split())',
        "text = strip_invisible(turn.text)",
        TT,
    ),
    (
        "in recent: hidden characters",
        IN,
        'text = " ".join(strip_invisible(turn.text).split())',
        'text = " ".join(turn.text.split())',
        TT,
    ),
    (
        "in recent: boundary cut",
        IN,
        'text = text if len(text) <= chars else text[: chars - 1] + "…"',
        'text = text if len(text) < chars else text[: chars - 1] + "…"',
        TT,
    ),
    ("in recent: one over", IN, 'text[: chars - 1] + "…"', 'text[:chars] + "…"', TT),
    (
        "in recent: fence can be closed",
        IN,
        '        text = re.sub(r"(?i)</\\s*(recently_sent|context)", r"</ \\1", text)\n',
        "",
        TT,
    ),
    ("in recent: context can be closed", IN, "(recently_sent|context)", "(recently_sent)", TT),
    ("in recent: not put in the context", IN, "        *recent,\n", "", TT),
    (
        "in tasks: not shown",
        IN,
        "    if tasks.strip():\n        parts.append(",
        "    if False:\n        parts.append(",
        TT,
    ),
    ("in tasks: not fenced", IN, 'fenced("open_tasks", tasks.strip())', "tasks.strip()", TT),
    # ---- the task list --------------------------------------------------------------------
    (
        "me tasks: symlinked folder followed",
        ST,
        "if self.cfg.task_index_lines <= 0 or directory.is_symlink():",
        "if self.cfg.task_index_lines <= 0:",
        TT,
    ),
    (
        "me tasks: switch ignored",
        TK,
        '    if limit <= 0:\n        return ""',
        '    if False:\n        return ""',
        TT,
    ),
    (
        "me tasks: hidden files listed",
        JO,
        '                    for name in [*dirs, *files]\n                    if not name.startswith(".") and not (Path(parent) / name).is_symlink()',
        "                    for name in [*dirs, *files]\n                    if not (Path(parent) / name).is_symlink()",
        TTJ,
    ),
    (
        "me tasks: any extension",
        ST,
        'if kind != "file" or path.suffix != ".md":',
        'if kind != "file":',
        TT,
    ),
    # No mutation here for "a symlinked note is followed": it is refused in two layers (the listing in
    # `MemoryJournal.entries` skips it, and `MemoryJournal.disk` raises for it), so deleting either
    # check alone changes nothing a test can observe (an equivalent mutant). The behaviour is pinned
    # by `test_only_markdown_files_directly_in_the_folder_count`.
    (
        "me tasks: folders listed",
        ST,
        'if kind != "file" or path.suffix != ".md":',
        'if path.suffix != ".md":',
        TT,
    ),
    ("me tasks: whole file read", TK, "head = note.text[:4096]", "head = note.text", TT),
    (
        "me tasks: bad bytes raise",
        ST,
        '.decode("utf-8", errors="replace")',
        '.decode("utf-8")',
        TT,
    ),
    (
        "me tasks: finished ones listed",
        TK,
        "        if _FINISHED_RE.match(status):\n            finished += 1\n            continue",
        "        if False:\n            finished += 1\n            continue",
        TT,
    ),
    ("me tasks: finished by prefix", TK, 'r")\\b")', 'r")")', TT),
    (
        "me tasks: oldest first",
        TK,
        "found.sort(key=lambda item: (-item[0], item[1]))",
        "found.sort(key=lambda item: (item[0], item[1]))",
        TT,
    ),
    (
        "me tasks: one over the cap",
        TK,
        "lines = [line for _, line in found[:limit]]",
        "lines = [line for _, line in found[: limit + 1]]",
        TT,
    ),
    (
        "me tasks: more at the cap",
        TK,
        "    if len(found) > limit:",
        "    if len(found) >= limit:",
        TT,
    ),
    (
        "me tasks: finished not counted",
        TK,
        "    if finished:\n        lines.append(",
        "    if False:\n        lines.append(",
        TT,
    ),
    (
        "me tasks: nothing open still prints",
        TK,
        '    if not found:\n        return ""\n    found.sort(',
        "    found.sort(",
        TT,
    ),
    ("me tasks: no name fallback", TK, 'or note.name.removesuffix(".md")', 'or ""', TT),
    (
        "me tasks: empty status shown",
        TK,
        '(f" [status: {status}]" if status else "")',
        'f" [status: {status}]"',
        TT,
    ),
    (
        "me tasks: long status",
        TK,
        "status = _label(_STATUS_LINE.search(head), 100)",
        "status = _label(_STATUS_LINE.search(head), 10000)",
        TT,
    ),
    (
        "me tasks: long title",
        TK,
        "title = _label(_TITLE_LINE.search(head), 80)",
        "title = _label(_TITLE_LINE.search(head), 8000)",
        TT,
    ),
]
