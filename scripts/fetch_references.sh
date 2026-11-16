#!/usr/bin/env bash
# Fetch third-party reference implementations into ./references (git-ignored).
#
# Why: before implementing an unfamiliar capability we study actively maintained
# open-source projects (design, dependencies, known issues) instead of coding
# from scratch. Nothing here is vendored into our source tree; we only read it.
# The pinned commits are recorded in references/SOURCES.lock for provenance.
#
# Usage:  scripts/fetch_references.sh            # clone anything missing
#         scripts/fetch_references.sh --update   # also fast-forward existing clones
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p references
UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

# name | url | sparse paths (space separated, empty = shallow full clone) | why we read it
REFS=(
  "openai-agents-python|https://github.com/openai/openai-agents-python.git||Engine we build on: Session protocol, RunState/HITL, MCP, guardrails, tracing"
  "claude-quickstarts|https://github.com/anthropics/claude-quickstarts.git||Anthropic reference implementation: managed-agents/daily-brief + autonomous-coding harness"
  "anthropic-skills|https://github.com/anthropics/skills.git||Agent Skills format (SKILL.md, progressive disclosure)"
  "claude-agent-sdk-python|https://github.com/anthropics/claude-agent-sdk-python.git||Permission modes, hooks, can_use_tool deferral semantics"
  "nanobot|https://github.com/HKUDS/nanobot.git||Compact Python always-on agent: memory, cron, MCP, SQLite sessions"
  "nanoclaw|https://github.com/qwibitai/nanoclaw.git||Container-per-session isolation + credential gateway with placeholders"
  "hermes-agent|https://github.com/NousResearch/hermes-agent.git|agent cron gateway tools plugins|Gateway, cron, memory tool, approval flow, sender pairing"
  "openclaw|https://github.com/openclaw/openclaw.git|docs src/cron src/memory src/pairing src/security src/secrets src/agents|Gateway/channels/cron/skills/memory/pairing/exec-approvals"
  "zeroclaw|https://github.com/zeroclaw-labs/zeroclaw.git|crates/zeroclaw-runtime/src/approval crates/zeroclaw-memory docs|Approval manager (always_ask precedence), supervised-by-default"
  "aimock|https://github.com/CopilotKit/aimock.git||Mock of the OpenAI Responses API: the wire shape our fake server (tests/fake_openai.py) answers with"
  "home-assistant-core|https://github.com/home-assistant/core.git|homeassistant/components/recorder|A long-running SQLite store that prunes itself: batched purge, nightly auto-purge, monthly repack (VACUUM)"
)

LOCK="references/SOURCES.lock"
TMP_LOCK="$(mktemp)"
printf "name\turl\tcommit\tcommit_date\tlicense_file\twhy\n" > "$TMP_LOCK"

for entry in "${REFS[@]}"; do
  IFS='|' read -r name url sparse why <<<"$entry"
  dir="references/$name"
  # An interrupted clone leaves a .git dir without a valid HEAD; start over in that case.
  if [[ -d "$dir/.git" ]] && ! git -C "$dir" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    echo ">> $name: incomplete clone found, removing"
    rm -rf "$dir"
  fi
  if [[ -d "$dir/.git" ]]; then
    if [[ $UPDATE -eq 1 ]]; then
      echo ">> updating $name"
      git -C "$dir" fetch --depth 1 origin >/dev/null 2>&1 && git -C "$dir" reset --hard FETCH_HEAD >/dev/null 2>&1 || echo "   (update failed, keeping existing)"
    else
      echo ">> $name already present"
    fi
  else
    echo ">> cloning $name"
    if [[ -n "$sparse" ]]; then
      git clone --quiet --depth 1 --filter=blob:none --sparse "$url" "$dir"
      # shellcheck disable=SC2086
      git -C "$dir" sparse-checkout set $sparse
    else
      git clone --quiet --depth 1 "$url" "$dir"
    fi
  fi
  sha="$(git -C "$dir" rev-parse HEAD)"
  cdate="$(git -C "$dir" log -1 --format=%cs)"
  lic="$(ls "$dir" 2>/dev/null | grep -iE '^(LICENSE|COPYING)' | head -1 || true)"
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$name" "$url" "$sha" "$cdate" "${lic:-none-at-sparse-root}" "$why" >> "$TMP_LOCK"
done

mv "$TMP_LOCK" "$LOCK"
echo
echo "Pinned references written to $LOCK"
column -t -s $'\t' "$LOCK" | cut -c1-160
