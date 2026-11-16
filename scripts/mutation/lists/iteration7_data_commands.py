"""Mutations for what the owner sees and does with the database: `pa db ...`, the daemon's daily
upkeep and how a command opens a damaged or newer database.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/iteration7_data_commands.py
Format and meaning: scripts/mutation/run.py.
"""

# ruff: noqa

CM = "src/personal_agent/commands/db.py"
CO = "src/personal_agent/commands/common.py"
DA = "src/personal_agent/daemon.py"
AP = "src/personal_agent/app.py"

CT = ["tests/test_cli_db.py"]
DT = ["tests/test_daemon.py"]
AT = ["tests/test_daemon.py", "tests/test_cli_db.py"]

M = [
    # ---- opening the database for a command --------------------------------------------------------
    (
        "open: a damaged file is a traceback",
        CO,
        "    except sqlite3.DatabaseError as exc:",
        "    except ZeroDivisionError as exc:",
        CT,
    ),
    (
        "open: a newer file is a failure, not misuse",
        CO,
        "    except DatabaseTooNew as exc:\n        fail(str(exc), code=USAGE)",
        "    except DatabaseTooNew as exc:\n        fail(str(exc))",
        CT,
    ),
    # ---- pa db ---------------------------------------------------------------------------------------
    (
        "pa db: nothing says what is removable",
        CM,
        'rows.add_row(escape(name), f"{count:,}", f"{waiting:,}" if waiting else "")',
        'rows.add_row(escape(name), f"{count:,}", "")',
        CT,
    ),
    (
        "pa db: the bracket is markup",
        CM,
        'note = escape("(config.toml [retention]; what was said and remembered is never removed)")',
        'note = "(config.toml [retention]; what was said and remembered is never removed)"',
        CT,
    ),
    (
        "pa db: 0 days, not for ever",
        CM,
        "{'for ever' if v == 0 else f'{v} days'}",
        "{f'{v} days'}",
        CT,
    ),
    (
        "pa db: a failed round looks fine",
        CM,
        'what = f"removed {last.total:,}" if not last.error else f"stopped: {escape(last.error)}"',
        'what = f"removed {last.total:,}"',
        CT,
    ),
    (
        "pa db: upkeep off is not said",
        CM,
        "    if not keep.auto:\n        say(",
        "    if False:\n        say(",
        CT,
    ),
    (
        "pa db: never run is not said",
        CM,
        "    elif last is None:\n        say(",
        "    elif False:\n        say(",
        CT,
    ),
    # ---- pa db prune ----------------------------------------------------------------------------------
    (
        "prune: a dry run removes",
        CM,
        '            counts = agent.janitor.eligible()\n            verb = "Would remove"',
        '            counts = agent.janitor.maintain().removed\n            verb = "Would remove"',
        CT,
    ),
    (
        "prune: an error is not reported",
        CM,
        "            if tidy.error:\n                _say_counts(",
        "            if False:\n                _say_counts(",
        CT,
    ),
    (
        "prune: keep-for-ever windows still run",
        CM,
        "        if all(agent.config.retention.days(rule.window) == 0 for rule in RULES):",
        "        if False:",
        CT,
    ),
    (
        "prune: kinds without rows listed",
        CM,
        '        if count:\n            say(f"  {count:>8,}  {name}: {what[name]}")',
        '        if True:\n            say(f"  {count:>8,}  {name}: {what[name]}")',
        CT,
    ),
    (
        "prune: nothing-to-do not said",
        CM,
        '    if not total:\n        say("Nothing is old enough to remove.")\n        return',
        '    if False:\n        say("Nothing is old enough to remove.")\n        return',
        CT,
    ),
    # ---- pa db check ----------------------------------------------------------------------------------
    (
        "check: problems are not failures",
        CM,
        "    if found:\n        for line in found[:20]:",
        "    if False:\n        for line in found[:20]:",
        CT,
    ),
    (
        "check: a missing file is a traceback",
        CM,
        "    if not path.exists():\n        fail(",
        "    if False:\n        fail(",
        CT,
    ),
    (
        "check: the file is opened for writing",
        CM,
        'f"file:{path}?mode=ro", uri=True)\n        try:\n            found = problems(conn, full=full)',
        'f"file:{path}", uri=True)\n        try:\n            found = problems(conn, full=full)',
        CT,
    ),
    (
        "check: --full is ignored",
        CM,
        "found = problems(conn, full=full)",
        "found = problems(conn)",
        CT,
    ),
    # ---- pa db backup ----------------------------------------------------------------------------------
    (
        "backup: an existing file is overwritten",
        CM,
        "        if target.exists():\n            fail(",
        "        if False:\n            fail(",
        CT,
    ),
    (
        "backup: the copy is not checked",
        CM,
        "                found = problems(copy)",
        "                found = []",
        CT,
    ),
    (
        "backup: a bad copy is kept",
        CM,
        '            target.unlink(missing_ok=True)\n            fail("The copy did not pass its check',
        '            fail("The copy did not pass its check',
        CT,
    ),
    ("backup: the folder anyone may enter", CM, "mkdir(mode=0o700,", "mkdir(mode=0o755,", CT),
    (
        "backup: a folder is taken as a file",
        CM,
        "target = where / stamp if where.is_dir() else where",
        "target = where",
        CT,
    ),
    (
        "backup: the parent need not exist",
        CM,
        "        if not target.parent.is_dir():\n            fail(",
        "        if False:\n            fail(",
        CT,
    ),
    (
        "backup: someone else's file removed",
        CM,
        "        except (OSError, sqlite3.Error) as exc:  # a copy that failed half way is already removed\n",
        "        except (OSError, sqlite3.Error) as exc:  # a copy that failed half way is already removed\n            target.unlink(missing_ok=True)\n",
        CT,
    ),
    # ---- pa db vacuum ------------------------------------------------------------------------------------
    (
        "vacuum: runs while the agent does",
        CM,
        "        if not lock.acquire():",
        "        if False:",
        CT,
    ),
    (
        "vacuum: keeps the lock",
        CM,
        "        finally:\n            lock.release()",
        "        finally:\n            pass",
        CT,
    ),
    (
        "vacuum: no check for room",
        CM,
        "            if free < before.total_bytes * 1.2:",
        "            if False:",
        CT,
    ),
    (
        "vacuum: the file keeps its size",
        CM,
        '                agent.db.checkpoint("TRUNCATE")',
        "                pass",
        CT,
    ),
    (
        "vacuum: a busy file is a traceback",
        CM,
        "            except sqlite3.OperationalError as exc:\n                fail(",
        "            except ZeroDivisionError as exc:\n                fail(",
        CT,
    ),
    # ---- the daemon ----------------------------------------------------------------------------------------
    (
        "daemon: upkeep at every look",
        DA,
        "                    if self.app.janitor.due():",
        "                    if True:",
        DT,
    ),
    (
        "daemon: upkeep cannot be stopped",
        DA,
        "self.app.janitor.maintain, stop=lambda: self._stopping",
        "self.app.janitor.maintain",
        DT,
    ),
    (
        "daemon: shutdown does not wait for it",
        DA,
        '.startswith(("worker-", "upkeep"))',
        '.startswith(("worker-",))',
        DT,
    ),
    (
        "daemon: removed rows not counted",
        DA,
        "                        self.stats.rows_pruned += tidy.total",
        "                        pass",
        DT,
    ),
    (
        "daemon: a failed upkeep is not counted",
        DA,
        '                    self.stats.errors += 1\n                    log.exception("database upkeep failed")',
        '                    log.exception("database upkeep failed")',
        DT,
    ),
    (
        "daemon: no upkeep loop",
        DA,
        '                asyncio.create_task(self._upkeep_loop(), name="upkeep"),\n',
        "",
        DT,
    ),
    (
        "daemon: the upkeep dies with its first failure",
        DA,
        '                except Exception:\n                    self.stats.errors += 1\n                    log.exception("database upkeep failed")',
        '                except ZeroDivisionError:\n                    self.stats.errors += 1\n                    log.exception("database upkeep failed")',
        DT,
    ),
    # ---- the wiring ----------------------------------------------------------------------------------------
    (
        "app: the janitor has other settings",
        AP,
        "janitor=Janitor(database, cfg.retention, clock),",
        "janitor=Janitor(database, type(cfg.retention)(), clock),",
        AT,
    ),
]
