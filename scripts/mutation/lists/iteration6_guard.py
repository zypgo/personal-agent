"""Mutations for the guard that keeps every test away from the real launchd.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/iteration6_guard.py
Format and meaning: scripts/mutation/run.py.

Safe to run on a real machine on purpose: each mutation is checked by `tests/test_launchd_guard.py`
alone, whose only launchctl calls name a domain (`gui/1`) and a label that do not exist, so even a
mutation that lets the call through changes nothing. (The mutations of the service tests proper
must never be checked against the other service tests without this guard: see iteration 6 in
docs/ARCHITECTURE.md.)
"""

# ruff: noqa

LD = "src/personal_agent/launchd.py"
CF = "tests/conftest.py"

G = ["tests/test_launchd_guard.py"]
LT = ["tests/test_launchd.py"]

M = [
    # ---- the production side: the default is chosen when the manager is built ------------------
    (
        "gd default: bound when the module is imported",
        LD,
        "        launchctl: Launchctl | None = None,",
        "        launchctl: Launchctl = system_launchctl,",
        G,
    ),
    (
        "gd default: an injected launchctl is ignored",
        LD,
        "self.launchctl = launchctl if launchctl is not None else system_launchctl",
        "self.launchctl = system_launchctl",
        LT,
    ),
    (
        "gd default: the job folder ignores agents_dir()",
        LD,
        "self.agents = agents if agents is not None else agents_dir()",
        'self.agents = agents if agents is not None else Path("~/Library/LaunchAgents").expanduser()',
        G,
    ),
    # ---- the guard itself ------------------------------------------------------------------------
    (
        "gd guard: looking is refused too",
        CF,
        '        if args and args[0] == "print":',
        "        if False:",
        G,
    ),
    (
        "gd guard: everything is let through",
        CF,
        '        if args and args[0] == "print":',
        "        if True:",
        G,
    ),
    (
        "gd guard: no verb crashes instead of being refused",
        CF,
        '        if args and args[0] == "print":',
        '        if args[0] == "print":',
        G,
    ),
    (
        "gd guard: a looked-at call loses its target",
        CF,
        "            return real(*args)",
        "            return real(*args[:1])",
        G,
    ),
    (
        "gd guard: the refusal does not say what was refused",
        CF,
        "raise AssertionError(f\"a test tried to change the real launchd: launchctl {' '.join(args)}\")",
        'raise AssertionError("a test tried to change the real launchd")',
        G,
    ),
    (
        "gd guard: launchctl is not replaced",
        CF,
        '    monkeypatch.setattr(launchd, "system_launchctl", only_looking)',
        '    monkeypatch.setattr(launchd, "system_launchctl", real)',
        G,
    ),
    (
        "gd guard: the job folder is not replaced",
        CF,
        '    monkeypatch.setattr(launchd, "agents_dir", lambda: tmp_path / "guarded-launchagents")',
        '    monkeypatch.setattr(launchd, "agents_dir", launchd.agents_dir)',
        G,
    ),
    (
        "gd guard: the job folder is shared between tests",
        CF,
        'lambda: tmp_path / "guarded-launchagents"',
        'lambda: tmp_path.parent / "guarded-launchagents"',
        G,
    ),
    (
        "gd guard: it is opt-in",
        CF,
        "@pytest.fixture(autouse=True)\ndef _no_test_changes_the_real_launchd",
        "@pytest.fixture\ndef _no_test_changes_the_real_launchd",
        G,
    ),
]
