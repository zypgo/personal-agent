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
  "rakazo|https://github.com/elie222/rakazo.git||Open Grok Bot alternative (Apache-2.0): teammates with memory, routines, group chats, delegation"
  "rome|https://github.com/rome-os/rome.git||Open Grok Bot alternative (MIT): agent OS that compounds through memory, SOPs and protocols"
  "eta|https://github.com/0xfe-project/eta.git||Open Grok Bot alternative (MIT): personas as markdown cards, one SQLite file, wake-ups that carry only the increment"
  "dsh-bots|https://github.com/kadaliao/dsh-bots.git||Open Grok Bot alternative (MIT): a bot team with persona, model and skills per bot; small enough to read in one sitting"
  "opengrokbot-z4yton|https://github.com/Z4YT0N/OpenGrokBot.git||Open Grok Bot alternative (MIT): employee personas working in one group chat"
  "opengrokbot-nickylin|https://github.com/nickylin/OpenGrokBot.git||Open Grok Bot alternative (MIT): a small local team"
  "opengrokbot-callmejustdodo|https://github.com/callmejustdodo/OpenGrokBot.git||Open Grok Bot alternative (MIT): one agent per team member"
  "opengrokbot-aierzs|https://github.com/aierzs/OpenGrokBot.git||Open Grok Bot alternative (MIT): reliable parallel group chat"
  "t3code|https://github.com/pingdotgg/t3code.git||Threads as the unit of work (MIT): create, run, archive, clean up, scheduled tasks inside one thread"
  "t3-tots|https://github.com/Deepusleepy/t3-tots.git||Grok Bot companion for T3 Code (MIT): a main bot hands each task to its own thread"
  "grok-bot-reconstructed|https://github.com/b-nnett/grok-bot-0.18-reconstructed.git||Unofficial rebuild of Grok Bot 0.18; archived, NO license grant: ideas only, nothing copied"
  "ant-bots|https://github.com/ElizantOS/ant-bots.git||Derivative of the rebuild above; NO license grant: ideas only, nothing copied"
  "guaca|https://github.com/madebywelch/guaca.git||AGPL-3.0, so read only, nothing copied: a quiet group of agents that tells you only what matters"
  "obsidian-skills|https://github.com/obsidianmd/obsidian-skills.git||Official Obsidian agent skills (MIT): Obsidian-flavored markdown, Bases, JSON Canvas"
  "karpathy-llm-wiki|https://github.com/Astro-Han/karpathy-llm-wiki.git||The LLM-wiki pattern as an agent skill (MIT): raw sources, wiki pages, index, log, lint"
  "obsidian-mcp-server|https://github.com/cyanheads/obsidian-mcp-server.git||Vault tool surface (Apache-2.0): read, write, search, surgical edits, frontmatter, tags"
  "seekstone|https://github.com/shaqmughal/seekstone.git||Obsidian vault tools that work on plain files, no running app needed (MIT)"
  "llm-wiki-hermes|https://github.com/r0b0tlab/llm-wiki_obsidian_hermes_r0b0tlabbra1n.git||Filesystem-first LLM wiki plus agent memory (MIT)"
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
