# Reference review — 2026-11-16

This review informed independently written Personal Agent changes. Branch snapshots, release
tags and reconstructed code are distinct evidence. No reference implementation was imported.
Personal Agent's comparison base is `215358b6ce2b6e66315552ecbd156f263234aa8b`.

## Pinned references

| Reference | Release/version evidence | Source inspected |
| --- | --- | --- |
| OpenClaw | Stable `v2026.11.4`, released 2026-11-16; release commit `d843d6af39a42cd3c30084edbeabeece732fd93a` | Newer `main` snapshot [`6f5d3b399b31855501cc3f38f1221f7165554c59`](https://github.com/openclaw/openclaw/tree/6f5d3b399b31855501cc3f38f1221f7165554c59). Claims below concern this inspected snapshot, not automatically the stable release. |
| DeepSeek Harness | Latest observed release `dsh-v0.2.2-rc.3`, 2026-11-13, marked prerelease | `master` [`39163dd30f703a79079e2d2446545cff3c453738`](https://github.com/deepseek-ai/deepseek-harness/tree/39163dd30f703a79079e2d2446545cff3c453738). The project explicitly labels itself a developer preview. |
| Grok Bot reconstruction | Unofficial reconstruction of shipped `0.18.0`; original repository archived 2026-08-28 | [`b-nnett/grok-bot-0.18-reconstructed@a9f633e09d49a85829b8236331b9e21f7e612634`](https://github.com/b-nnett/grok-bot-0.18-reconstructed/tree/a9f633e09d49a85829b8236331b9e21f7e612634), not an official xAI release or complete original source tree. |

Release sources: [OpenClaw release](https://github.com/openclaw/openclaw/releases/tag/v2026.11.4),
[DeepSeek release](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.3).

## What the comparison actually supports

| Area | Reference strengths | Implication for Personal Agent |
| --- | --- | --- |
| Runtime/provider substitution | OpenClaw registers and selects multiple runtimes. DeepSeek formally registers LLM adapters, the agent factory, persistence and many other services through Cordis. | Explicit provider contracts and capability checks are useful now. Personal Agent's SDK runner and SQLite stores remain deliberately coupled; an endpoint URL is not a replaceable agent runtime. |
| Long-running work | OpenClaw has session and outbound delivery recovery. DeepSeek has persisted schedules, goal state and session logs. | Reliable persistence is not unique to this project. Keep the small Inbox/Outbox state machine and test its boundary with each channel. |
| Recovery meaning | DeepSeek's scheduler recovers pending inbox deliveries; restored goals start disarmed and need authorized resumption. Local in-process jobs do not become durable merely because session history exists. | Distinguish replaying history, recovering a pending task, and avoiding duplicate external actions. Do not equate them. |
| Channels and delegation | OpenClaw has broader channel plugins and subagent lifecycle support. DeepSeek exposes multiple subagent providers, SDK/ACP integration and richer desktop/Web composition. | A declared channel capability contract helps expand gradually. This PR does not introduce a new distributed multi-agent scheduler. |
| Output and interaction | Grok's reconstructed protocol makes file attachments and other typed outputs explicit. DeepSeek and OpenClaw expose richer runtime/UI state. | Add real artifact delivery and bounded transient streaming while retaining durable final messages as the authoritative history. |
| Isolation | DeepSeek has OS sandbox implementations in addition to plugin scopes. OpenClaw's larger plugin surface creates more trust and operational boundaries. | Plugin lifecycle, scope and type declarations do not themselves isolate processes or untrusted code. Existing sandbox, approval and egress checks remain necessary. |
| Adoption cost | The broader frameworks offer more extension points and integrations, with more configuration and recovery paths to understand. DeepSeek warns of breaking changes during preview. | Adopt small interfaces around existing functionality first; a wholesale framework migration needs separate compatibility and recovery tests. |

### OpenClaw code anchors

- [Runtime registry](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/agents/harness/registry.ts) and [runtime selection](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/agents/harness/selection.ts).
- [Session delivery recovery](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/infra/session-delivery-queue-recovery.ts) and [SQLite outbound queue](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/infra/delivery-queue-sqlite.ts).
- [Channel catalog registry](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/plugins/channel-catalog-registry.ts).

### DeepSeek code anchors and boundaries

- [LLM service](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/llm/llm/src/index.ts), [agent service](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/core/agent/src/index.ts), and [architecture](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/architecture.md).
- [Persistence](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/session/session-persistence/src/index.ts), [schedule recovery](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/schedule/schedule/src/runtime.ts), and [goal activation](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/goal/goal/src/index.ts).
- [Experimental webhook documentation](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/subsystems/webhook.md) describes a different delivery boundary from a durable Inbox: do not assume automatic deduplication/replay.
- Cordis reversible effects dispose registrations/listeners and lifecycle resources. They do not automatically undo an already sent network request or an arbitrary file write. Agent/plugin scopes are not OS sandboxes. See [safety notice](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/SAFETY.md).

## Grok provenance and version check

The [README](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/README.md)
distinguishes reconstructed runtime/control-plane code, a retained shipped renderer, and a
partial readable frontend. It expressly says the shipped frontend did not include original
source or source maps. Describing the repository as complete source-map recovery would be wrong.

Its [NOTICE](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/NOTICE.md)
does not grant an upstream source-code license. Research here uses architectural ideas and
observable protocol boundaries only; no reconstructed source, renderer assets, installers,
account cookies or authentication transport are copied or executed.

The reconstruction contains file-oriented output contracts and persisted send acceptance:
[send-message schema](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/source/host/runner/tools/send-message-schema.ts),
[acceptance records](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/source/host/extensions/transcript/send-acceptance.ts).
The inference router for Cursor/Claude Code/Codex/OpenRouter, routed MCP tools, local usage
tracking, optional local Docker connector and router-settings UI are identified by the author
as later experiments. They are not evidence that official Grok Bot 0.18 shipped those features.

Checked follow-up repositories:

| Repository | Fixed commit | What was verified |
| --- | --- | --- |
| `ElizantOS/ant-bots` | [`1378374adf12e10cb4871c0d2bdac3343049ad6e`](https://github.com/ElizantOS/ant-bots/tree/1378374adf12e10cb4871c0d2bdac3343049ad6e) | Still based on the 0.18 reconstruction; later activity does not establish a new upstream reconstruction. |
| `smartyua/Grok-Bot-reconstruction-` | [`2beb2f9409ed507385c09753d33977591aa59963`](https://github.com/smartyua/Grok-Bot-reconstruction-/tree/2beb2f9409ed507385c09753d33977591aa59963) | Its 0.43 wording concerns comparison with an installed app; the reconstructed base remains 0.18. |

No newer reconstruction tied to a verifiable newer shipped version was established in this
review. That is a bounded search result, not proof that none exists.

## Adopted and deferred

This PR applies the useful boundaries to the existing Python system: explicit provider/channel
capabilities; independent speech configuration; streamed progress separate from committed
history; and immutable, authenticated artifact delivery through the existing Outbox. The
[artifact contract](ARTIFACTS.md) documents its limits, retention and uncertain-delivery behavior.

Grok-inspired acceptance obligations, durable background-job state, structured questions and
priority lanes are possible later work. They are not represented as implemented here. The
current PR does not replace the SDK agent loop, migrate storage, promise rollback of external
effects, or add unattended execution privileges.
