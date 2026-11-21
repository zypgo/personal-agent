"""Mutations for 0.9.3: Codex running on your own computer, answered from the web page.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/v093_native_host.py
Format and meaning: scripts/mutation/run.py.

Every entry breaks one line of the three things that make that work: saying at once when Codex
cannot be found, how a stopped Codex is recognised on macOS, and what the page asks and tells
Codex when it wants to run something. The listed tests must then fail. Names start with the area
(`start:`, `stop:`, `ask:`, `answer:`).
"""

# ruff: noqa

# Left out on purpose:
# - the 40,000-character limit in `server_request`: unchanged, and the test agent cannot send a
#   request that large.
# - the early "cancelled, so do not ask" return in `server_request`: without it the late check
#   still refuses to grant the answer, so the only difference is one needless question, and
#   whether the cancel lands before the request arrives depends on the Python version's event
#   loop. The test agent cannot make that order certain.

PROC = "src/personal_agent/agent_adapters/native_process.py"
CODEX = "src/personal_agent/agent_adapters/codex.py"
HOST = "src/personal_agent/agent_adapters/host.py"
CLI = "src/personal_agent/commands/agents.py"

T = ["tests/test_v4_native_host.py"]

M = [
    # ---- saying so when the program cannot be found -------------------------------------------
    (
        "start: a missing program is reported before a task can be taken",
        CLI,
        "if program is not None and shutil.which(program) is None:",
        "if False:",
        T,
    ),
    (
        "start: the program given with --executable is the one looked for",
        CLI,
        "program = program_for(provider, executable)",
        "program = program_for(provider)",
        T,
    ),
    (
        "start: the message says how to point at the program",
        CLI,
        '"or add --executable followed by its full path.",',
        '"or try again.",',
        T,
    ),
    (
        "start: a provider that starts no other program is not asked for one",
        HOST,
        "return (executable or DEFAULT_PROGRAMS[name]) if name in DEFAULT_PROGRAMS else None",
        'return executable or DEFAULT_PROGRAMS.get(name, "codex")',
        T,
    ),
    (
        "start: Codex is looked for as codex",
        HOST,
        'DEFAULT_PROGRAMS = {"codex": "codex",',
        'DEFAULT_PROGRAMS = {"codex": "codex-cli",',
        T,
    ),
    (
        "start: Cursor is looked for as agent",
        HOST,
        '"cursor": "agent"}',
        '"cursor": "cursor"}',
        T,
    ),
    # ---- recognising that Codex has stopped (macOS) -----------------------------------------
    (
        "stop: a process that exited but was not collected is still named",
        PROC,
        "            if status != psutil.STATUS_ZOMBIE:\n                raise\n",
        "            if True:\n                raise\n",
        T,
    ),
    (
        "stop: a running process whose group cannot be read still gets no name",
        PROC,
        "            if status != psutil.STATUS_ZOMBIE:\n                raise\n",
        "            if False:\n                raise\n",
        T,
    ),
    # ---- what the person is asked ------------------------------------------------------------
    (
        "ask: a command is shown whole",
        CODEX,
        '["Codex 想运行这条命令：", command, *(["", *facts] if facts else [])]',
        '["Codex 想运行这条命令：", command[:200], *(["", *facts] if facts else [])]',
        T,
    ),
    (
        "ask: the place the command runs in is named",
        CODEX,
        '    ("cwd", "在这个目录里"),\n',
        "",
        T,
    ),
    (
        "ask: the reason is named",
        CODEX,
        '    ("reason", "原因"),\n    ("networkApprovalContext", "需要联网"),\n',
        '    ("networkApprovalContext", "需要联网"),\n',
        T,
    ),
    (
        "ask: a request to use the network says where to",
        CODEX,
        '    ("networkApprovalContext", "需要联网"),\n',
        "",
        T,
    ),
    (
        "ask: the range a file change wants to write to is named",
        CODEX,
        '_FILE_FACTS = (("grantRoot", "想要写入的范围"), ("reason", "原因"))',
        '_FILE_FACTS = (("reason", "原因"),)',
        T,
    ),
    (
        "ask: what only identifies the request is left out",
        CODEX,
        '        "turnId",\n    }\n)',
        "    }\n)",
        T,
    ),
    (
        "ask: what Codex adds that this code does not know is still shown",
        CODEX,
        "    if extras:\n        text +=",
        "    if False:\n        text +=",
        T,
    ),
    (
        "ask: an empty extra is not shown",
        CODEX,
        "if key not in _WIRE_FIELDS and key not in shown and value is not None",
        "if key not in _WIRE_FIELDS and key not in shown",
        T,
    ),
    (
        "ask: typing into a terminal is not described as running a command",
        CODEX,
        'if params.get("kind", "command") != "command" or not isinstance(command, str):',
        "if not isinstance(command, str):",
        T,
    ),
    (
        "ask: a blank command is shown as sent",
        CODEX,
        "        if not command.strip():\n            return raw\n",
        "",
        T,
    ),
    (
        "ask: a request of another kind is shown as sent",
        CODEX,
        "    else:\n        return raw\n    extras = {",
        '    else:\n        text, shown = "Codex 想修改文件。", set()\n    extras = {',
        T,
    ),
    # ---- what Codex is told ------------------------------------------------------------------
    (
        "answer: allow lets it run",
        CODEX,
        'APPROVAL_ANSWERS = {"允许": "accept",',
        'APPROVAL_ANSWERS = {"允许": "decline",',
        T,
    ),
    (
        "answer: decline only skips this one step",
        CODEX,
        '"拒绝": "decline",',
        '"拒绝": "cancel",',
        T,
    ),
    (
        "answer: stop ends the whole attempt",
        CODEX,
        '"拒绝并停止": "cancel"}',
        '"拒绝并停止": "decline"}',
        T,
    ),
    (
        "answer: no answer, or one that was not offered, stops the attempt",
        CODEX,
        'return {"decision": decision if decision and not cancel.is_set() else "cancel"}',
        'return {"decision": decision if decision and not cancel.is_set() else "decline"}',
        T,
    ),
    (
        "answer: an answer given after the attempt was cancelled is not granted",
        CODEX,
        'return {"decision": decision if decision and not cancel.is_set() else "cancel"}',
        'return {"decision": decision or "cancel"}',
        T,
    ),
]
