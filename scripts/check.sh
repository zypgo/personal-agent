#!/usr/bin/env bash
# The quality gate: what has to pass before something is committed and pushed.
#
#   scripts/check.sh          format, lint, types, every offline test, the web page's tests
#   scripts/check.sh --fast   the same without the tests (a few seconds)
#
# Not part of it, because they cost money or time:
#   PA_LIVE=1 uv run pytest tests/live                                  the real model (about $0.35)
#   uv run python scripts/mutation/run.py scripts/mutation/lists/X.py   break the code on purpose
#   HYPOTHESIS_PROFILE=thorough uv run pytest tests -q -n 4             many more random cases
set -euo pipefail
cd "$(dirname "$0")/.."

fast=0
case "${1:-}" in
  "") ;;
  --fast) fast=1 ;;
  *) echo "usage: scripts/check.sh [--fast]" >&2; exit 2 ;;
esac

step() { printf '\n== %s\n' "$1"; }

step "format"; uv run ruff format --check .
step "lint"; uv run ruff check .
step "types"; uv run mypy

if [ "$fast" -eq 0 ]; then
  # `ci` makes the random cases the same on every run, so a red run can be repeated.
  step "tests"; HYPOTHESIS_PROFILE=ci uv run pytest tests -q -p no:cacheprovider -n 4
  step "web page"
  if command -v node >/dev/null 2>&1; then
    node --test tests/js/*.test.mjs
  else
    echo "node is not installed: the page's tests were skipped"
  fi
fi

printf '\nall checks passed\n'
