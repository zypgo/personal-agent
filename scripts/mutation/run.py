"""Break the code on purpose and check that the tests notice.

    uv run python scripts/mutation/run.py scripts/mutation/lists/iteration6_spec.py
    uv run python scripts/mutation/run.py LIST "sp check" "sp label"   # only names containing these

A list defines `M = [(name, "src/....py", old, new, ["tests/test_x.py", ...]), ...]`. In the named
file `old` (which must occur there) is replaced once by `new`; the listed tests must then fail. A
mutation they let through "survived": the protection it names is not pinned by any test.

What keeps the answer honest
- It works on a COPY of src/, tests/, docs/, README.md and pyproject.toml in a temporary folder,
  so a crash or Ctrl-C can never leave the real source broken.
- It first runs every distinct group of tests against the unchanged copy. A test that already fails
  would "kill" every mutation in its group and hide the ones that survive. (This happened once,
  for 13 mutations: see docs/ARCHITECTURE.md, iteration 6.)
- A test that hangs under a mutation (an endless loop) is stopped after `--timeout` seconds and
  counts as killed.

Exit status: 0 every mutation killed, 1 one survived or its anchor is gone, 3 a baseline fails.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
Mutation = tuple[str, str, str, str, list[str]]


def load(path: Path) -> list[Mutation]:
    namespace: dict = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)  # noqa: S102
    return namespace["M"]


def run_tests(copy: Path, tests: list[str], timeout: int) -> tuple[str, str]:
    """('pass' | 'fail' | 'timeout', the end of the output)."""
    env = {**os.environ, "PYTHONPATH": str(copy / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    command = [sys.executable, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider"]
    try:
        done = subprocess.run(
            command, cwd=copy, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        return "timeout", ""
    return ("pass" if done.returncode == 0 else "fail"), done.stdout[-1500:]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("list", type=Path, help="a file that defines M")
    parser.add_argument("only", nargs="*", help="only mutations whose name contains one of these")
    parser.add_argument("--timeout", type=int, default=300, help="seconds one test run may take")
    args = parser.parse_args()

    chosen = [m for m in load(args.list) if not args.only or any(o in m[0] for o in args.only)]
    if not chosen:
        print("No mutation matches.")
        return 1
    with tempfile.TemporaryDirectory(prefix="pa-mutation-") as folder:
        copy = Path(folder)
        skip = shutil.ignore_patterns("__pycache__")
        for name in ("src", "tests", "docs"):
            shutil.copytree(ROOT / name, copy / name, ignore=skip)
        for name in ("pyproject.toml", "README.md"):
            shutil.copy(ROOT / name, copy / name)

        groups = {tuple(m[4]) for m in chosen}
        for tests in sorted(groups):
            state, tail = run_tests(copy, list(tests), 3 * args.timeout)
            if state != "pass":
                print(f"BASELINE {state.upper()}: {list(tests)} (fix this before mutating)")
                print(tail)
                return 3
        print(f"baselines clean for {len(groups)} group(s) of tests; {len(chosen)} mutation(s)")

        killed, loose = 0, []
        for name, rel, old, new, tests in chosen:
            path = copy / rel
            original = path.read_text(encoding="utf-8")
            if old not in original:
                print(f"ANCHOR MISSING: {name}", flush=True)
                loose.append(f"{name} (anchor missing)")
                continue
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            try:
                state, _ = run_tests(copy, tests, args.timeout)
            finally:
                path.write_text(original, encoding="utf-8")
            if state == "pass":
                print(f"SURVIVED: {name}", flush=True)
                loose.append(name)
            else:
                killed += 1
                print(
                    f"killed  : {name}" + (" (timed out)" if state == "timeout" else ""), flush=True
                )

    print(f"\n{killed} killed, {len(loose)} survived")
    for name in loose:
        print(f"  - {name}")
    return 1 if loose else 0


if __name__ == "__main__":
    sys.exit(main())
