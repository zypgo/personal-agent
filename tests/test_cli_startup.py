"""`pa` answers at once: starting it, `--help` and shell completion must not load the model SDK.

The SDK (`agents`, `openai`, `mcp`) takes most of a second to import. A command that needs it
imports it itself, when it runs; everything else (the command tree, the help texts, argument
parsing) stays light. A stray top-level import in any command module would silently bring the
slowness back, so each is checked here, in a fresh interpreter (this process has long since loaded
the SDK).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HEAVY = ("agents", "openai", "mcp")

WALK = """
import json, sys
from typer.main import get_command
from typer.testing import CliRunner

from personal_agent.cli import app

HEAVY = {heavy!r}
after_import = sorted(m for m in HEAVY if m in sys.modules)

def walk(command, path=()):
    yield path
    for name, sub in getattr(command, "commands", {{}}).items():
        yield from walk(sub, (*path, name))

runner = CliRunner()
paths = list(walk(get_command(app)))
failed = {{}}
for path in paths:
    result = runner.invoke(app, [*path, "--help"])
    if result.exit_code != 0:
        failed[" ".join(path) or "(pa)"] = result.output[-300:]

print(json.dumps({{
    "commands": [" ".join(p) for p in paths],
    "failed": failed,
    "after_import": after_import,
    "after_help": sorted(m for m in HEAVY if m in sys.modules),
}}))
"""


def _fresh_interpreter(code: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OPENAI_", "PA_"))}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        env={**env, "HOME": env.get("TMPDIR", "/tmp")},
        check=False,
    )


def test_importing_the_command_line_does_not_load_the_model_sdk():
    result = _fresh_interpreter(
        "import sys; import personal_agent.cli; "
        f"print(sorted(m for m in {HEAVY!r} if m in sys.modules))"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", f"loaded at import: {result.stdout.strip()}"


def test_every_command_has_help_and_asking_for_it_does_not_load_the_model_sdk():
    result = _fresh_interpreter(WALK.format(heavy=HEAVY))
    assert result.returncode == 0, result.stderr
    walked = json.loads(result.stdout.strip().splitlines()[-1])

    assert walked["failed"] == {}, "help failed for: " + ", ".join(walked["failed"])
    assert walked["after_import"] == [], f"loaded at import: {walked['after_import']}"
    assert walked["after_help"] == [], (
        f"asking for help loaded {walked['after_help']}: a command imports the model SDK at the "
        "top of its module instead of inside the command that needs it"
    )
    # The walk must really have visited the tree, not an empty one.
    names = set(walked["commands"])
    assert {"ask", "chat", "serve", "timers add", "memory view", "mcp list"} <= names, names


def test_the_version_is_printed_without_the_model_sdk():
    result = _fresh_interpreter(
        "import sys; from typer.testing import CliRunner; from personal_agent.cli import app; "
        "r = CliRunner().invoke(app, ['--version']); print(r.exit_code, r.output.strip()); "
        f"print(sorted(m for m in {HEAVY!r} if m in sys.modules))"
    )
    assert result.returncode == 0, result.stderr
    shown, loaded = result.stdout.strip().splitlines()[-2:]
    assert shown.startswith("0 Qixu "), shown
    assert loaded == "[]"
