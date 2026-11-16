"""The mutation lists stay attached to the code they break.

A mutation list is the proof that the tests notice broken code (`scripts/mutation/run.py`). A
refactor that moves or reformats a line leaves its mutation pointing at nothing, and nothing says so
until somebody runs the slow runner by hand: that once happened silently to 28 of them. So the cheap
part of that check, "the anchor still exists and the tests it names still exist", runs with every
normal test run. Whether the tests then really kill the mutation is still the runner's job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LISTS = sorted((ROOT / "scripts" / "mutation" / "lists").glob("*.py"))


def mutations(path: Path) -> list[tuple[str, str, str, str, list[str]]]:
    namespace: dict = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)  # noqa: S102
    return namespace["M"]


@pytest.mark.skipif(not LISTS, reason="the mutation lists are not part of this copy of the tree")
@pytest.mark.parametrize("path", LISTS, ids=lambda p: p.stem)
def test_every_mutation_still_has_its_anchor_and_its_tests(path):
    sources: dict[str, str] = {}
    problems: list[str] = []
    for name, source, old, new, tests in mutations(path):
        if old == new:
            problems.append(f"changes nothing: {name}")
        if source not in sources:
            file = ROOT / source
            sources[source] = file.read_text(encoding="utf-8") if file.is_file() else ""
        if not old or old not in sources[source]:
            problems.append(f"the anchor is gone from {source}: {name}")
        problems.extend(
            f"{name}: names a test file that does not exist: {test}"
            for test in tests
            if not (ROOT / test).is_file()
        )
    assert not problems, "\n" + "\n".join(problems)


def test_the_check_notices_a_moved_line(tmp_path):
    """The guard itself must be able to fail: a list whose anchor is not in its source."""
    lists = tmp_path / "scripts" / "mutation" / "lists"
    lists.mkdir(parents=True)
    broken = lists / "broken.py"
    broken.write_text('M = [("gone", "pyproject.toml", "no such line anywhere", "x", [])]\n')
    [(name, source, old, _new, _tests)] = mutations(broken)
    assert name == "gone" and old not in (ROOT / source).read_text(encoding="utf-8")
