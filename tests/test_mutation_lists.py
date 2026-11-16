"""The mutation lists stay attached to the code they break.

A mutation list is the proof that the tests notice broken code (`scripts/mutation/run.py`). A
refactor that moves or reformats a line leaves its mutation pointing at nothing, and nothing says so
until somebody runs the slow runner by hand: that once happened silently to 28 of them. A refactor
can also add a second line the anchor matches. The runner changes only the first match, so the
mutation quietly starts testing the wrong line: that happened to five. So the cheap part of that
check, "the anchor is in its file exactly once and the tests it names exist", runs with every normal
test run. Whether the tests then really kill the mutation is still the runner's job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LISTS = sorted((ROOT / "scripts" / "mutation" / "lists").glob("*.py"))

Entry = tuple[str, str, str, str, list[str]]


def mutations(path: Path) -> list[Entry]:
    namespace: dict = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)  # noqa: S102
    return namespace["M"]


def problems_in(entries: list[Entry], root: Path = ROOT) -> list[str]:
    sources: dict[str, str] = {}
    problems: list[str] = []
    for name, source, old, new, tests in entries:
        if old == new:
            problems.append(f"changes nothing: {name}")
        if source not in sources:
            file = root / source
            sources[source] = file.read_text(encoding="utf-8") if file.is_file() else ""
        found = sources[source].count(old) if old else 0
        if found == 0:
            problems.append(f"the anchor is gone from {source}: {name}")
        elif found > 1:
            problems.append(
                f"the anchor is in {source} {found} times and only the first would change; "
                f"add the lines around it: {name}"
            )
        problems.extend(
            f"{name}: names a test file that does not exist: {test}"
            for test in tests
            if not (root / test).is_file()
        )
    return problems


@pytest.mark.skipif(not LISTS, reason="the mutation lists are not part of this copy of the tree")
@pytest.mark.parametrize("path", LISTS, ids=lambda p: p.stem)
def test_every_mutation_still_has_its_anchor_and_its_tests(path):
    problems = problems_in(mutations(path))
    assert not problems, "\n" + "\n".join(problems)


def test_the_check_notices_each_way_a_mutation_can_come_loose(tmp_path):
    """The guard itself must be able to fail, once for every way it is meant to."""
    (tmp_path / "source.py").write_text("a = 1\nb = 2\nb = 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_there.py").write_text("", encoding="utf-8")
    entries: list[Entry] = [
        ("fine", "source.py", "a = 1", "a = 3", ["tests/test_there.py"]),
        ("gone", "source.py", "c = 1", "c = 3", []),
        ("twice", "source.py", "b = 2", "b = 3", []),
        ("nothing changes", "source.py", "a = 1", "a = 1", []),
        ("no such test", "source.py", "a = 1", "a = 3", ["tests/test_missing.py"]),
        ("empty anchor", "source.py", "", "x", []),
        ("no such file", "nowhere.py", "a = 1", "a = 3", []),
    ]
    found = "\n".join(problems_in(entries, tmp_path))
    assert "fine" not in found
    for name in (
        "gone",
        "twice",
        "nothing changes",
        "no such test",
        "empty anchor",
        "no such file",
    ):
        assert name in found, f"{name!r} was not noticed:\n{found}"
