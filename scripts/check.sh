#!/usr/bin/env bash
# The quality gate: what has to pass before something is committed and pushed.
#
#   scripts/check.sh              format, lint, types, offline tests, frontend contract/build/tests
#   scripts/check.sh --fast       the same without the tests (a few seconds)
#   scripts/check.sh --python 3.11  the offline tests on another interpreter (default 3.11, the
#                                 oldest one `requires-python` allows), in a private environment
#   PA_TEST_WORKERS=2 scripts/check.sh  reduce parallel workers on memory-limited machines
#
# Not part of it, because they cost money or time:
#   PA_LIVE=1 uv run pytest tests/live                                  the real model (about $0.35)
#   uv run python scripts/mutation/run.py scripts/mutation/lists/X.py   break the code on purpose
#   HYPOTHESIS_PROFILE=thorough uv run pytest tests -q -n 4             many more random cases
set -euo pipefail
cd "$(dirname "$0")/.."

fast=0
workers="${PA_TEST_WORKERS:-4}"
case "${1:-}" in
  "") ;;
  --fast) fast=1 ;;
  --python)
    # What is declared has to be what is tested: some behaviour of asyncio and the standard
    # library differs between 3.11 and 3.13, and the default environment only has one of them.
    version="${2:-3.11}"
    printf '\n== tests on python %s\n' "$version"
    UV_PROJECT_ENVIRONMENT="${TMPDIR:-/tmp}/personal-agent-venv-py$version" \
      HYPOTHESIS_PROFILE=ci \
      uv run --python "$version" --frozen pytest tests -q -p no:cacheprovider -n "$workers"
    printf '\npython %s: all tests passed\n' "$version"
    exit 0
    ;;
  *) echo "usage: scripts/check.sh [--fast | --python VERSION]" >&2; exit 2 ;;
esac

step() { printf '\n== %s\n' "$1"; }

step "format"; uv run ruff format --check .
step "lint"; uv run ruff check .
step "types"; uv run mypy

if [ "$fast" -eq 0 ]; then
  # `ci` makes the random cases the same on every run, so a red run can be repeated.
  step "tests"; HYPOTHESIS_PROFILE=ci uv run pytest tests -q -p no:cacheprovider -n "$workers"
  step "web page"
  if command -v node >/dev/null 2>&1; then
    if [ ! -d node_modules ]; then
      echo "Install locked frontend build dependencies with npm ci, then rerun this check." >&2
      exit 1
    fi
    npm run check
  else
    echo "node is not installed: the page's tests were skipped"
  fi
fi

printf '\nall checks passed\n'
