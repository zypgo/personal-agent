"""Mutations for 0.9.0: the knowledge base, the task threads, the way the reply is paced.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/v09_knowledge_threads.py
Format and meaning: scripts/mutation/run.py.
"""

# ruff: noqa

VS = "src/personal_agent/vault/store.py"
VT = "src/personal_agent/tools/vault.py"
NA = "src/personal_agent/orchestration/narration.py"
TH = "src/personal_agent/orchestration/threads.py"
PA = "src/personal_agent/channels/pacing.py"

TV = ["tests/test_vault_store.py", "tests/test_vault_note.py", "tests/test_dream_vault.py"]
TT = ["tests/test_vault_tools.py"]
TK = ["tests/test_threads.py", "tests/test_daemon_threads.py", "tests/test_thread_api.py"]
TP = ["tests/test_pacing.py"]

M = [
    # ---- what a path may name ------------------------------------------------------------
    (
        "vs path: a name can climb out of the folder",
        VS,
        '        if part == ".." or part.startswith("."):\n',
        "        if False:\n",
        TV,
    ),
    (
        "vs path: an absolute path is accepted",
        VS,
        '    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):\n',
        "    if False:\n",
        TV,
    ),
    (
        "vs path: a link out of the folder is followed",
        VS,
        "            if here.is_symlink():\n",
        "            if False:\n",
        TV,
    ),
    (
        "vs path: a name can break the [[link]] it sits in",
        VS,
        "        if strict and _LINK_UNSAFE.search(part):\n",
        "        if False:\n",
        TV,
    ),
    # ---- what the assistant may see ------------------------------------------------------
    (
        "vs read: a folder the owner shut can be read",
        VS,
        '        if top in {unicodedata.normalize("NFC", e).strip("/") for e in self.cfg.exclude}:\n',
        "        if False:\n",
        TV,
    ),
    (
        "vs read: a private note can be read",
        VS,
        "        if private and actor in _ASSISTANT:\n",
        "        if False:\n",
        TV,
    ),
    (
        "vs search: a private note is found",
        VS,
        "            include_private=actor not in _ASSISTANT,\n",
        "            include_private=True,\n",
        TV,
    ),
    (
        "vs listing: a private note is listed",
        VS,
        "            if entry.generated or (entry.private and actor in _ASSISTANT):\n",
        "            if entry.generated:\n",
        TV,
    ),
    (
        "vt search: what came from outside is trusted",
        VT,
        "    outside = any(_flag(h.origin, h.path) for h in hits)\n",
        "    outside = False\n",
        TT,
    ),
    (
        "vt read: what came from outside is trusted",
        VT,
        "    outside = note.outside\n",
        "    outside = False\n",
        TT,
    ),
    # ---- what the assistant may write ----------------------------------------------------
    (
        "vs write: the assistant writes in any folder",
        VS,
        '        if "/" not in path or top not in AREAS:\n',
        "        if False:\n",
        TV,
    ),
    (
        "vs write: the assistant writes over the index",
        VS,
        '        if path in GENERATED:\n            raise VaultError("这个文件是系统自动生成的',
        '        if False:\n            raise VaultError("这个文件是系统自动生成的',
        TV,
    ),
    (
        "vs write: an original can be changed",
        VS,
        '        if top == "raw" and action != "create":\n',
        "        if False:\n",
        TV,
    ),
    (
        "vs write: a day's note can be rewritten",
        VS,
        '        if top == "daily" and action not in ("create", "append"):\n',
        "        if False:\n",
        TV,
    ),
    (
        "vs write: a task's note can be rewritten",
        VS,
        '        if top == "threads" and action != "append":\n',
        "        if False:\n",
        TV,
    ),
    (
        "vs write: the assistant adds to a task's note outside its notes part",
        VS,
        '        if path.startswith("threads/") and actor in _ASSISTANT:\n            heading = NOTES_HEADING',
        "        if False:\n            heading = NOTES_HEADING",
        TV,
    ),
    (
        "vs dream: the night writes outside wiki and daily",
        VS,
        '            top not in ("wiki", "daily") or action in ("replace", "section")\n',
        '            action in ("replace", "section")\n',
        TV,
    ),
    (
        "vs dream: the night rewrites notes",
        VS,
        '            top not in ("wiki", "daily") or action in ("replace", "section")\n',
        '            top not in ("wiki", "daily")\n',
        TV,
    ),
    (
        "vs dream: the night adds to the owner's notes",
        VS,
        '            elif actor == Actor.DREAM and origin_of(md.parse(old).front, path) == "owner":\n',
        "            elif False:\n",
        TV,
    ),
    (
        "vs text: a key is written into a note",
        VS,
        "            found = find_secrets(text)\n            if found:\n",
        "            found = find_secrets(text)\n            if False:\n",
        TV,
    ),
    (
        "vs text: a note can be any size",
        VS,
        '        if len(text.encode("utf-8")) > self.cfg.max_note_bytes:\n            raise VaultError(f"笔记太长了',
        '        if False:\n            raise VaultError(f"笔记太长了',
        TV,
    ),
    # ---- not losing what the owner wrote -------------------------------------------------
    (
        "vs rewrite: it needs no revision",
        VS,
        '                if rev is None:\n                    raise VaultError("整篇改写要带上',
        '                if False:\n                    raise VaultError("整篇改写要带上',
        TV,
    ),
    (
        "vs rewrite: a stale revision is accepted",
        VS,
        "                if rev != revision(old):\n",
        "                if False:\n",
        TV,
    ),
    (
        "vs append: a stale revision is accepted",
        VS,
        "                if rev is not None and rev != revision(old):\n",
        "                if False:\n",
        TV,
    ),
    (
        "vs rewrite: the owner's properties are dropped",
        VS,
        "        if before.front_raw is None or after.front_raw is not None:\n            return text\n",
        "        return text\n",
        TV,
    ),
    (
        "vs write: the file is written in place",
        VS,
        "            os.replace(temp, full)\n",
        '            full.write_bytes(text.encode("utf-8"))\n',
        TV,
    ),
    (
        "vs origin: words from outside leave a note looking clean",
        VS,
        "        if origin in OUTSIDE_ORIGINS and wanted not in OUTSIDE_ORIGINS:\n",
        "        if False:\n",
        TV,
    ),
    (
        "vs origin: a new note of the assistant's is called the owner's",
        VS,
        '            wanted = "web" if path.startswith("raw/") else "assistant"\n',
        '            wanted = "owner"\n',
        TV,
    ),
    # ---- what an agent handed in ---------------------------------------------------------
    (
        "vs capture: saving twice saves twice",
        VS,
        "            if self._read_file(path) is not None:\n                return path, False\n",
        "            if False:\n                return path, False\n",
        TV,
    ),
    (
        "vs capture: a key stays in what is kept",
        VS,
        "            text = redact(body)\n",
        "            text = body\n",
        TV,
    ),
    # ---- history and undo ----------------------------------------------------------------
    (
        "vs restore: a new note is 'restored' to nothing",
        VS,
        '        if row["old_text"] is None:\n            raise VaultError("这一版是新建笔记',
        '        if False:\n            raise VaultError("这一版是新建笔记',
        TV,
    ),
    (
        "vs revert: a note edited since is put back anyway",
        VS,
        '                if (revision(current) if current is not None else None) != row["new_sha"]:\n',
        "                if False:\n",
        TV,
    ),
    (
        "vs history: nothing is ever dropped",
        VS,
        "        cutoff = self.clock.now() - days * 86400\n",
        "        cutoff = 0\n",
        TV,
    ),
    (
        "vs backlog: a private original is listed as waiting",
        VS,
        '            if p.startswith("raw/") and p not in linked and not e.private\n',
        '            if p.startswith("raw/") and p not in linked\n',
        TV,
    ),
    # ---- the line between "needs you" and noise --------------------------------------------
    (
        "na patrol: a restart counts as time the agent was quiet",
        NA,
        "            waited = now - max(f.since, baseline)\n",
        "            waited = now - f.since\n",
        TK,
    ),
    (
        "na patrol: a sleep counts as time the agent was stuck",
        NA,
        "            idle = now - max(f.activity, baseline)\n",
        "            idle = now - f.activity\n",
        TK,
    ),
    (
        "na patrol: a snoozed task is nudged",
        NA,
        "        if f.snoozed_until > now:\n            continue\n",
        "        if False:\n            continue\n",
        TK,
    ),
    (
        "na patrol: a put-away task is nudged",
        NA,
        "        if f.archived:\n            continue\n",
        "        if False:\n            continue\n",
        TK,
    ),
    (
        "na patrol: a finished task is nudged for being quiet",
        NA,
        "                put_away.append(f.task_id)\n            continue\n",
        "                put_away.append(f.task_id)\n",
        TK,
    ),
    (
        "na patrol: a task is put away the moment it ends",
        NA,
        "now - f.ended_at >= cfg.archive_after_hours * 3600",
        "now - f.ended_at >= 0",
        TK,
    ),
    (
        "na patrol: the second reminder comes at the first",
        NA,
        "            if waited >= cfg.remind_again_hours * 3600:\n",
        "            if waited >= cfg.remind_minutes * 60:\n",
        TK,
    ),
    (
        "na patrol: the second reminder is the first one again",
        NA,
        '                nudges.append(Nudge(f.task_id, f"{base}:1:{int(f.since)}", text, line))\n',
        '                nudges.append(Nudge(f.task_id, f"{base}:2:{int(f.since)}", text, line))\n',
        TK,
    ),
    (
        "na bundle: three at once come one by one",
        NA,
        "BUNDLE_AT = 3\n",
        "BUNDLE_AT = 99\n",
        TK,
    ),
    (
        "na bundle: a long list is not cut",
        NA,
        "_BUNDLE_LINES = 6\n",
        "_BUNDLE_LINES = 600\n",
        TK,
    ),
    (
        "na name: a Latin name sticks to the Chinese after it",
        NA,
        '    after = " " if name[-1].isascii() and name[-1].isalnum() else ""\n',
        '    after = ""\n',
        TK,
    ),
    # ---- the keeper -----------------------------------------------------------------------
    (
        "th tick: a wake-up is not noticed",
        TH,
        "        if now < self._beat or now - self._beat > SLEEP_S:\n",
        "        if False:\n",
        TK,
    ),
    (
        "th patrol: what a bundle already said is said again",
        TH,
        "        nudges = [n for n in nudges if self.db.kv_get(SAID + n.key) is None]\n",
        "        nudges = list(nudges)\n",
        TK,
    ),
    (
        "th nudge: a bundle does not remember what it said",
        TH,
        '            self.db.kv_set(SAID + key, "1", now=self._now())\n',
        "            pass\n",
        TK,
    ),
    (
        "th follow: the first look reads out the whole past",
        TH,
        '            top = self.db.scalar("SELECT COALESCE(MAX(id), 0) FROM orchestration_events")\n',
        "            top = 0\n",
        TK,
    ),
    (
        "th speak: the switch for telling the owner does nothing",
        TH,
        '        if not self.cfg.notify:\n            return 0\n        names = self._agents(task["id"])\n',
        '        names = self._agents(task["id"])\n',
        TK,
    ),
    (
        "th tell: the same news is sent twice",
        TH,
        '            kind="notice",\n            dedupe_key=key,\n',
        '            kind="notice",\n            dedupe_key=None,\n',
        TK,
    ),
    (
        "th archive: a task that is still going is put away",
        TH,
        '        if task is None or task["state"] not in TASK_TERMINAL:\n            return False\n        self.adopt(task_id)\n',
        "        if task is None:\n            return False\n        self.adopt(task_id)\n",
        TK,
    ),
    # ---- the pace of a reply --------------------------------------------------------------
    (
        "pa chatty: the switch for bubbles does nothing",
        PA,
        '        cfg.bubbles\n        and message.kind == "message"\n',
        '        message.kind == "message"\n',
        TP,
    ),
    (
        "pa chatty: a notice or an approval is paced",
        PA,
        '        and message.kind == "message"\n',
        "",
        TP,
    ),
    (
        "pa chatty: a message nobody is waiting for is paced",
        PA,
        '        and bool(message.payload.get("interactive"))\n',
        "",
        TP,
    ),
    (
        "pa chatty: an urgent message is paced",
        PA,
        '        and message.urgency != "urgent"\n',
        "",
        TP,
    ),
    (
        "pa chatty: a message with a file is paced",
        PA,
        '        and not message.payload.get("artifacts")\n',
        "",
        TP,
    ),
    (
        "pa chatty: a long reply is cut up",
        PA,
        "        and len(message.text) <= MAX_CHARS\n    )",
        "    )",
        TP,
    ),
    (
        "pa bubbles: code is cut in two",
        PA,
        "    if not body or _FENCE in body or _STRUCTURE.search(body):\n",
        "    if not body or _STRUCTURE.search(body):\n",
        TP,
    ),
    (
        "pa bubbles: a list is cut in two",
        PA,
        "    if not body or _FENCE in body or _STRUCTURE.search(body):\n",
        "    if not body or _FENCE in body:\n",
        TP,
    ),
    (
        "pa bubbles: what is over the limit is dropped",
        PA,
        '        parts = [*parts[: limit - 1], "\\n\\n".join(parts[limit - 1 :])]\n',
        "        parts = parts[:limit]\n",
        TP,
    ),
    (
        "pa pause: a long message waits for ever",
        PA,
        "    return min(MAX_PAUSE_S, MIN_PAUSE_S + PER_CHAR_S * len(text))\n",
        "    return MIN_PAUSE_S + PER_CHAR_S * len(text)\n",
        TP,
    ),
]
