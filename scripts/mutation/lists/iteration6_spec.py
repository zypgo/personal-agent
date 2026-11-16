"""Mutations for the scenario format (`evals/spec.py`).

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/iteration6_spec.py
Format and meaning: scripts/mutation/run.py.
"""

# ruff: noqa

SP_ = "src/personal_agent/evals/spec.py"
CK_ = "src/personal_agent/evals/checks.py"
BN_ = "src/personal_agent/evals/bench.py"
RN_ = "src/personal_agent/evals/runner.py"
RP_ = "src/personal_agent/evals/report.py"
CL_ = "src/personal_agent/commands/evals.py"

SPT = ["tests/test_eval_spec.py", "tests/test_eval_builtin.py"]
CKT = ["tests/test_eval_checks.py", "tests/test_eval_builtin.py"]
BNT = ["tests/test_eval_bench.py", "tests/test_eval_runner.py"]
RNT = ["tests/test_eval_runner.py", "tests/test_eval_builtin.py", "tests/test_cli_eval.py"]
RPT = ["tests/test_eval_report.py", "tests/test_cli_eval.py"]
CLT = ["tests/test_cli_eval.py"]

M = [
    # ---- spec: paths ------------------------------------------------------------------------------
    (
        "sp rel: blank ok",
        SP_,
        "        not path.strip()\n        or",
        "        False\n        or",
        SPT,
    ),
    ("sp rel: backslash ok", SP_, '        or "\\\\" in path\n', "", SPT),
    ("sp rel: nul ok", SP_, '        or "\\x00" in path\n', "", SPT),
    ("sp rel: absolute ok", SP_, "        or pure.is_absolute()\n", "", SPT),
    ("sp rel: dotdot ok", SP_, '        or ".." in pure.parts\n', "", SPT),
    (
        "sp rel: length off by one",
        SP_,
        "        or len(path) > 200\n",
        "        or len(path) >= 200\n",
        SPT,
    ),
    ("sp rel: no length limit", SP_, "        or len(path) > 200\n", "", SPT),
    ("sp rel: not normalised", SP_, "    return str(pure)\n", "    return path\n", SPT),
    ("sp regex: no length limit", SP_, "    if len(pattern) > 300:", "    if False:", SPT),
    (
        "sp regex: length off by one",
        SP_,
        "    if len(pattern) > 300:",
        "    if len(pattern) >= 300:",
        SPT,
    ),
    ("sp regex: not compiled", SP_, "        re.compile(pattern)\n", "        pass\n", SPT),
    (
        "sp regex: none refused",
        SP_,
        "    if pattern is None:\n        return None",
        "    if pattern is None:\n        return ''",
        SPT,
    ),
    # ---- spec: seed ----------------------------------------------------------------------------------
    (
        "sp seed: paths unchecked",
        SP_,
        '@field_validator("preferences", "memory", "files")',
        '@field_validator("memory", "files")',
        SPT,
    ),
    (
        "sp seed: prefs unchecked",
        SP_,
        '@field_validator("preferences", "memory", "files")',
        '@field_validator("memory", "files")',
        SPT,
    ),
    (
        "sp seed: files unchecked",
        SP_,
        '@field_validator("preferences", "memory", "files")',
        '@field_validator("preferences", "memory")',
        SPT,
    ),
    (
        "sp seed: memory unchecked",
        SP_,
        '@field_validator("preferences", "memory", "files")',
        '@field_validator("preferences", "files")',
        SPT,
    ),
    (
        "sp seed: nested task",
        SP_,
        '            if "/" in name or not name.endswith(".md") or name.startswith("."):',
        '            if not name.endswith(".md") or name.startswith("."):',
        SPT,
    ),
    (
        "sp seed: any extension task",
        SP_,
        '            if "/" in name or not name.endswith(".md") or name.startswith("."):',
        '            if "/" in name or name.startswith("."):',
        SPT,
    ),
    (
        "sp seed: hidden task",
        SP_,
        '            if "/" in name or not name.endswith(".md") or name.startswith("."):',
        '            if "/" in name or not name.endswith(".md"):',
        SPT,
    ),
    (
        "sp seed: prefs place",
        SP_,
        'f"preferences/{name}": text for name, text in self.preferences.items()',
        'f"prefs/{name}": text for name, text in self.preferences.items()',
        SPT,
    ),
    (
        "sp seed: state place",
        SP_,
        'f"state/{name}": text for name, text in self.memory.items()',
        'f"{name}": text for name, text in self.memory.items()',
        SPT,
    ),
    (
        "sp seed: tasks place",
        SP_,
        'f"state/tasks/{name}": text for name, text in self.tasks.items()',
        'f"state/{name}": text for name, text in self.tasks.items()',
        SPT,
    ),
    # ---- spec: names -------------------------------------------------------------------------------------
    (
        "sp name: longer",
        SP_,
        '_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")',
        '_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")',
        SPT,
    ),
    (
        "sp name: uppercase",
        SP_,
        '_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")',
        '_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")',
        SPT,
    ),
    (
        "sp name: dash first",
        SP_,
        '_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")',
        '_NAME = re.compile(r"^[a-z0-9_-]{1,63}$")',
        SPT,
    ),
    (
        "sp name: no end anchor",
        SP_,
        '_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")',
        '_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}")',
        SPT,
    ),
    (
        "sp timer name: longer",
        SP_,
        '_TIMER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")',
        '_TIMER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")',
        SPT,
    ),
    (
        "sp timer name: no dots",
        SP_,
        '_TIMER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")',
        '_TIMER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")',
        SPT,
    ),
    (
        "sp timer name: slash",
        SP_,
        '_TIMER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")',
        '_TIMER_NAME = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,62}$")',
        SPT,
    ),
    (
        "sp timer: name unchecked",
        SP_,
        """        if not _TIMER_NAME.match(value):
            raise ValueError("a schedule's name is lowercase letters, digits, dots, dashes")""",
        """        if False:
            raise ValueError("a schedule's name is lowercase letters, digits, dots, dashes")""",
        SPT,
    ),
    (
        "sp webhook: name unchecked",
        SP_,
        """        if not _TIMER_NAME.match(value):
            raise ValueError("a webhook's name is lowercase letters, digits, dots, dashes")""",
        """        if False:
            raise ValueError("a webhook's name is lowercase letters, digits, dots, dashes")""",
        SPT,
    ),
    (
        "sp step: id unchecked",
        SP_,
        """    def _id(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("an id is lowercase letters, digits, dashes, underscores")
        return value

    @field_validator("ends")""",
        """    def _id(cls, value: str) -> str:
        return value

    @field_validator("ends")""",
        SPT,
    ),
    # ---- spec: steps ------------------------------------------------------------------------------------
    ("sp ends: unknown ok", SP_, "        if unknown or not value:", "        if not value:", SPT),
    ("sp ends: empty ok", SP_, "        if unknown or not value:", "        if unknown:", SPT),
    (
        "sp ends: no failed",
        SP_,
        'RUN_ENDS = ("completed", "silent", "failed", "cancelled", "paused_budget")',
        'RUN_ENDS = ("completed", "silent", "cancelled", "paused_budget")',
        SPT,
    ),
    (
        "sp ends: no paused",
        SP_,
        'RUN_ENDS = ("completed", "silent", "failed", "cancelled", "paused_budget")',
        'RUN_ENDS = ("completed", "silent", "failed", "cancelled")',
        SPT,
    ),
    (
        "sp ends: default",
        SP_,
        'default_factory=lambda: ["completed", "silent"]',
        'default_factory=lambda: ["completed"]',
        SPT,
    ),
    (
        "sp event: none is fine",
        SP_,
        '        if len(given) != 1:\n            raise ValueError("a step has exactly one',
        '        if len(given) > 1:\n            raise ValueError("a step has exactly one',
        SPT,
    ),
    (
        "sp event: two is fine",
        SP_,
        '        if len(given) != 1:\n            raise ValueError("a step has exactly one',
        '        if len(given) < 1:\n            raise ValueError("a step has exactly one',
        SPT,
    ),
    (
        "sp event: session on timer",
        SP_,
        "        if self.session is not None and self.message is None:",
        "        if False:",
        SPT,
    ),
    (
        "sp event: session unchecked",
        SP_,
        "        if self.session is not None and not _TIMER_NAME.match(self.session):",
        "        if False:",
        SPT,
    ),
    (
        "sp kind: timer",
        SP_,
        '        return "timer" if self.timer is not None else "webhook"',
        '        return "webhook" if self.timer is not None else "timer"',
        SPT,
    ),
    (
        "sp key: timer",
        SP_,
        '            return f"timer:{self.timer.name}"',
        '            return f"{self.timer.name}"',
        SPT,
    ),
    (
        "sp key: webhook",
        SP_,
        '            return f"webhook:{self.webhook.name}"',
        '            return f"{self.webhook.name}"',
        SPT,
    ),
    (
        "sp key: default session",
        SP_,
        '        return self.session or "main"',
        '        return self.session or "default"',
        SPT,
    ),
    (
        "sp key: session ignored",
        SP_,
        '        return self.session or "main"',
        '        return "main"',
        SPT,
    ),
    # ---- spec: checks ------------------------------------------------------------------------------------
    (
        "sp check: id unchecked",
        SP_,
        """        if value is not None and not _NAME.match(value):""",
        """        if False:""",
        SPT,
    ),
    (
        "sp check: id uppercase ok",
        SP_,
        """        if value is not None and not _NAME.match(value):""",
        """        if value is not None and not _NAME.match(value.lower()):""",
        SPT,
    ),
    (
        "sp says: nothing sought",
        SP_,
        "        if not (self.any_of or self.all_of or self.pattern):",
        "        if False:",
        SPT,
    ),
    (
        "sp says: pattern unchecked",
        SP_,
        """    def _valid_pattern(cls, value: str | None) -> str | None:
        return _regex(value)

    @model_validator(mode="after")
    def _something_to_look_for(self) -> Says:""",
        """    def _valid_pattern(cls, value: str | None) -> str | None:
        return value

    @model_validator(mode="after")
    def _something_to_look_for(self) -> Says:""",
        SPT,
    ),
    (
        "sp never: nothing sought",
        SP_,
        "        if not (self.any_of or self.pattern):",
        "        if False:",
        SPT,
    ),
    (
        "sp never: pattern unchecked",
        SP_,
        """    def _valid_pattern(cls, value: str | None) -> str | None:
        return _regex(value)

    @model_validator(mode="after")
    def _something_to_look_for(self) -> NeverSays:""",
        """    def _valid_pattern(cls, value: str | None) -> str | None:
        return value

    @model_validator(mode="after")
    def _something_to_look_for(self) -> NeverSays:""",
        SPT,
    ),
    ("sp max: negative ok", SP_, "    limit: int = Field(ge=0)", "    limit: int", SPT),
    (
        "sp tools used: none ok",
        SP_,
        """    kind: Literal["tools_used"]
    step: str | None = None
    tools: list[str] = Field(min_length=1)""",
        """    kind: Literal["tools_used"]
    step: str | None = None
    tools: list[str]""",
        SPT,
    ),
    (
        "sp tools not used: none ok",
        SP_,
        """    kind: Literal["tools_not_used"]
    step: str | None = None
    tools: list[str] = Field(min_length=1)""",
        """    kind: Literal["tools_not_used"]
    step: str | None = None
    tools: list[str]""",
        SPT,
    ),
    (
        "sp memory has: path unchecked",
        SP_,
        """    def _plain_path(cls, value: str) -> str:
        return _relative(value)""",
        """    def _plain_path(cls, value: str) -> str:
        return value""",
        SPT,
    ),
    (
        "sp untouched: paths unchecked",
        SP_,
        "        return [_relative(p) for p in value]",
        "        return value",
        SPT,
    ),
    (
        "sp untouched: none ok",
        SP_,
        "    paths: list[str] = Field(min_length=1)",
        "    paths: list[str]",
        SPT,
    ),
    ("sp spend: zero ok", SP_, "    usd: float = Field(gt=0)", "    usd: float = Field(ge=0)", SPT),
    # ---- spec: actions and scripts ---------------------------------------------------------------------------
    (
        "sp action: nothing ok",
        SP_,
        '        if len(given) != 1:\n            raise ValueError("an action is exactly',
        '        if len(given) > 1:\n            raise ValueError("an action is exactly',
        SPT,
    ),
    (
        "sp action: two ok",
        SP_,
        '        if len(given) != 1:\n            raise ValueError("an action is exactly',
        '        if len(given) < 1:\n            raise ValueError("an action is exactly',
        SPT,
    ),
    (
        "sp action: default urgency",
        SP_,
        '    urgency: Literal["urgent", "normal", "low"] = "normal"',
        '    urgency: Literal["urgent", "normal", "low"] = "low"',
        SPT,
    ),
    (
        "sp bad: no fails ok",
        SP_,
        "    fails: list[str] = Field(min_length=1)",
        "    fails: list[str]",
        SPT,
    ),
    # ---- spec: the scenario ----------------------------------------------------------------------------------
    (
        "sp scenario: id unchecked",
        SP_,
        """    def _id(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("an id is lowercase letters, digits, dashes, underscores")
        return value

    @field_validator("tags")""",
        """    def _id(cls, value: str) -> str:
        return value

    @field_validator("tags")""",
        SPT,
    ),
    (
        "sp scenario: tags unchecked",
        SP_,
        "        if any(not _NAME.match(t) for t in value):",
        "        if False:",
        SPT,
    ),
    (
        "sp label: counter from zero",
        SP_,
        "            name, count = base, 1",
        "            name, count = base, 0",
        SPT,
    ),
    (
        "sp label: counter stuck",
        SP_,
        '                count += 1\n                name = f"{base}-{count}"',
        '                name = f"{base}-2"',
        SPT,
    ),
    (
        "sp label: taken not remembered",
        SP_,
        "            taken.add(name)\n            out.append((name, check))",
        "            out.append((name, check))",
        SPT,
    ),
    (
        "sp label: step in name",
        SP_,
        '            base = f"{check.kind}-{step}" if step else check.kind',
        "            base = check.kind",
        SPT,
    ),
    (
        "sp label: given ids ignored",
        SP_,
        "        taken = {c.id for c in self.checks if c.id}",
        "        taken = set()",
        SPT,
    ),
    (
        "sp label: given id changed",
        SP_,
        "            if check.id:\n                out.append((check.id, check))\n                continue",
        "            if False:\n                out.append((check.id, check))\n                continue",
        SPT,
    ),
    (
        "sp ids: ends first",
        SP_,
        '        return [f"ends-{s.id}" for s in self.steps] + [name for name, _ in self.labelled_checks()]',
        "        return [name for name, _ in self.labelled_checks()]",
        SPT,
    ),
    (
        "sp consistent: step ids",
        SP_,
        '        if len(set(step_ids)) != len(step_ids):\n            raise ValueError("step ids must be unique")',
        "        pass",
        SPT,
    ),
    (
        "sp consistent: check steps",
        SP_,
        "            if step is not None and step not in step_ids:",
        "            if False:",
        SPT,
    ),
    (
        "sp consistent: given ids",
        SP_,
        '        if len(set(given)) != len(given):\n            raise ValueError("check ids must be unique")',
        "        pass",
        SPT,
    ),
    (
        "sp consistent: implicit names",
        SP_,
        '        if set(given) & {f"ends-{s.id}" for s in self.steps}:',
        "        if False:",
        SPT,
    ),
    (
        "sp consistent: untouched seeded",
        SP_,
        '                if missing:\n                    raise ValueError(\n                        f"`memory_untouched` names',
        '                if False:\n                    raise ValueError(\n                        f"`memory_untouched` names',
        SPT,
    ),
    (
        "sp consistent: script steps",
        SP_,
        "            unknown = [k for k in script if k not in step_ids]\n            if unknown:",
        "            unknown = []\n            if unknown:",
        SPT,
    ),
    (
        "sp consistent: good script included",
        SP_,
        '        scripts = [("good", mock.good)] + [(b.name, b.script) for b in mock.bad]',
        "        scripts = [(b.name, b.script) for b in mock.bad]",
        SPT,
    ),
    (
        "sp consistent: bad scripts included",
        SP_,
        '        scripts = [("good", mock.good)] + [(b.name, b.script) for b in mock.bad]',
        '        scripts = [("good", mock.good)]',
        SPT,
    ),
    (
        "sp consistent: fails refs",
        SP_,
        "            unknown = [f for f in bad.fails if f not in valid]\n            if unknown:",
        "            unknown = []\n            if unknown:",
        SPT,
    ),
    (
        "sp consistent: ends refs",
        SP_,
        "        valid = set(self.check_ids())",
        "        valid = {name for name, _ in self.labelled_checks()}",
        SPT,
    ),
    (
        "sp consistent: bad names",
        SP_,
        "        if len(set(names)) != len(names):",
        "        if False:",
        SPT,
    ),
    # ---- spec: loading ------------------------------------------------------------------------------------------
    (
        "sp problems: all shown",
        SP_,
        "for where, what in found[:6]]",
        "for where, what in found]",
        SPT,
    ),
    (
        "sp problems: fewer shown",
        SP_,
        "for where, what in found[:6]]",
        "for where, what in found[:2]]",
        SPT,
    ),
    (
        "sp problems: more always",
        SP_,
        '(f"; and {more} more" if more > 0 else "")',
        '(f"; and {more} more")',
        SPT,
    ),
    (
        "sp problems: where",
        SP_,
        "f\"{where or '(top level)'}: {what}\"",
        'f"(top level): {what}"',
        SPT,
    ),
    (
        "sp parse: any yaml",
        SP_,
        '    if not isinstance(data, dict):\n        raise ScenarioError(f"{source}: a scenario is a mapping',
        '    if False:\n        raise ScenarioError(f"{source}: a scenario is a mapping',
        SPT,
    ),
    (
        "sp parse: source lost",
        SP_,
        '        raise ScenarioError(f"{source}: {_problems(exc)}") from exc',
        '        raise ScenarioError(f"{_problems(exc)}") from exc',
        SPT,
    ),
    (
        "sp load: size unchecked",
        SP_,
        "        if path.stat().st_size > MAX_FILE_BYTES:",
        "        if False:",
        SPT,
    ),
    (
        "sp load: size off by one",
        SP_,
        "        if path.stat().st_size > MAX_FILE_BYTES:",
        "        if path.stat().st_size >= MAX_FILE_BYTES:",
        SPT,
    ),
    (
        "sp load: not utf8",
        SP_,
        '        text = path.read_text(encoding="utf-8")',
        '        text = path.read_text(encoding="latin-1")',
        SPT,
    ),
    (
        "sp folder: links followed",
        SP_,
        "        if path.is_symlink() or not path.is_file():",
        "        if not path.is_file():",
        SPT,
    ),
    (
        "sp folder: folders read",
        SP_,
        "        if path.is_symlink() or not path.is_file():",
        "        if path.is_symlink():",
        SPT,
    ),
    (
        "sp folder: no yml",
        SP_,
        '    for path in sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml")):',
        '    for path in sorted(folder.glob("*.yaml")):',
        SPT,
    ),
    (
        "sp folder: unsorted",
        SP_,
        '    for path in sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml")):',
        '    for path in list(folder.glob("*.yaml")) + list(folder.glob("*.yml")):',
        SPT,
    ),
    (
        "sp folder: duplicate replaces",
        SP_,
        "        if scenario.id in catalog.scenarios:\n            catalog.problems.append(",
        "        if False:\n            catalog.problems.append(",
        SPT,
    ),
    (
        "sp folder: label",
        SP_,
        '        catalog.origin[scenario.id] = label if label == "built-in" else f"{label}/{path.name}"',
        "        catalog.origin[scenario.id] = label",
        SPT,
    ),
    (
        "sp catalog: no user folder",
        SP_,
        '    if paths is not None:\n        _read_folder(user_dir(paths), "evals", catalog)',
        '    if False:\n        _read_folder(user_dir(paths), "evals", catalog)',
        SPT,
    ),
    (
        "sp catalog: user first",
        SP_,
        '    _read_folder(builtin_dir(), "built-in", catalog)\n    if paths is not None:\n        _read_folder(user_dir(paths), "evals", catalog)',
        '    if paths is not None:\n        _read_folder(user_dir(paths), "evals", catalog)\n    _read_folder(builtin_dir(), "built-in", catalog)',
        SPT,
    ),
    (
        "sp user dir",
        SP_,
        '    return paths.home / "evals"',
        '    return paths.home / "scenarios"',
        SPT,
    ),
]
