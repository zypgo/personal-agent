# Reference review, 2026-11-16

Before changing Personal Agent, we read three outside projects. Everything in Personal Agent was written
independently, and no code from these projects was imported. The comparison base for Personal Agent is
`215358b6ce2b6e66315552ecbd156f263234aa8b`.

A branch snapshot, a release tag and a reconstruction are three different kinds of evidence.
So the table below says exactly what was read for each project.

## What we looked at

| Reference | Release or version | What we read |
| --- | --- | --- |
| OpenClaw | Stable `v2026.11.4`, released 2026-11-16; release commit `d843d6af39a42cd3c30084edbeabeece732fd93a` | A newer `main` snapshot, [`6f5d3b399b31855501cc3f38f1221f7165554c59`](https://github.com/openclaw/openclaw/tree/6f5d3b399b31855501cc3f38f1221f7165554c59). The claims below are about this snapshot, not automatically about the stable release. |
| DeepSeek Harness | Latest release seen: `dsh-v0.2.2-rc.3` (2026-11-13), marked prerelease | `master` at [`39163dd30f703a79079e2d2446545cff3c453738`](https://github.com/deepseek-ai/deepseek-harness/tree/39163dd30f703a79079e2d2446545cff3c453738). The project calls itself a developer preview. |
| Grok Bot reconstruction | An unofficial reconstruction of the shipped `0.18.0`. The original repository was archived on 2026-08-28. | [`b-nnett/grok-bot-0.18-reconstructed@a9f633e09d49a85829b8236331b9e21f7e612634`](https://github.com/b-nnett/grok-bot-0.18-reconstructed/tree/a9f633e09d49a85829b8236331b9e21f7e612634). It is not an official xAI release, and it is not the complete original source. |

Release sources: [OpenClaw release](https://github.com/openclaw/openclaw/releases/tag/v2026.11.4),
[DeepSeek release](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.3).

## What the comparison tells us

| Area | What the references do well | What it means for Personal Agent |
| --- | --- | --- |
| Swapping runtimes and providers | OpenClaw registers several runtimes and picks one. DeepSeek registers LLM adapters, the agent factory, persistence and many other services through Cordis. | Clear provider contracts and capability checks are useful now. We keep the SDK runner and the SQLite stores tightly coupled on purpose. Having an endpoint URL does not make something a replaceable agent runtime. |
| Long-running work | OpenClaw can recover sessions and outbound deliveries. DeepSeek keeps schedules, goal state and session logs on disk. | Other projects store this reliably too, so it is not something only we do. Keep the small Inbox/Outbox state machine, and test where it meets each channel. |
| What "recovery" means | DeepSeek's scheduler recovers pending inbox deliveries. Restored goals start disarmed and need an authorized resume. Local in-process jobs do not become durable just because session history exists. | Keep three things apart: replaying history, recovering a pending task, and not repeating an action in the outside world. Do not treat them as the same thing. |
| Channels and delegation | OpenClaw has more channel plugins and supports subagent lifecycles. DeepSeek offers several subagent providers, SDK/ACP integration and a richer desktop/Web setup. | A declared channel capability contract lets us add channels gradually. This PR does not add a new distributed multi-agent scheduler. |
| Output and interaction | Grok's reconstructed protocol spells out file attachments and other typed outputs. DeepSeek and OpenClaw expose more runtime and UI state. | Add real artifact delivery and limited, temporary streaming. The saved final messages stay the official history. |
| Isolation | DeepSeek has OS sandbox implementations on top of plugin scopes. OpenClaw's larger plugin surface means more trust and operating boundaries. | Plugin lifecycle, scope and type declarations do not isolate processes or untrusted code by themselves. The existing sandbox, approval and egress checks are still needed. |
| Cost of adopting | The bigger frameworks have more extension points and integrations, and more configuration and recovery paths to learn. DeepSeek warns about breaking changes during the preview. | Start with small interfaces around what already exists. Moving to a whole framework would need its own compatibility and recovery tests. |

### OpenClaw: files worth reading

- [Runtime registry](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/agents/harness/registry.ts) and [runtime selection](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/agents/harness/selection.ts).
- [Session delivery recovery](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/infra/session-delivery-queue-recovery.ts) and [SQLite outbound queue](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/infra/delivery-queue-sqlite.ts).
- [Channel catalog registry](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/plugins/channel-catalog-registry.ts).

### DeepSeek: files worth reading, and its limits

- [LLM service](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/llm/llm/src/index.ts), [agent service](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/core/agent/src/index.ts), and [architecture](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/architecture.md).
- [Persistence](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/session/session-persistence/src/index.ts), [schedule recovery](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/schedule/schedule/src/runtime.ts), and [goal activation](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/goal/goal/src/index.ts).
- The [experimental webhook documentation](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/subsystems/webhook.md) describes a different delivery model from a durable Inbox. Do not assume it deduplicates or replays automatically.
- Cordis's reversible effects clean up registrations, listeners and lifecycle resources. They do not undo a network request that was already sent, or an arbitrary file write. Agent and plugin scopes are not OS sandboxes. See the [safety notice](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/SAFETY.md).

## Where the Grok code comes from, and which version it is

The [README](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/README.md)
separates three parts: reconstructed runtime and control-plane code, a shipped renderer that was kept as it was,
and a partly readable frontend. It says plainly that the shipped frontend did not include the original source or
source maps. So it would be wrong to describe the repository as a full source-map recovery.

Its [NOTICE](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/NOTICE.md)
does not give a license to the upstream source code. We took only architectural ideas and observable protocol boundaries
from it. We did not copy or run any reconstructed source, renderer assets, installers, account cookies or authentication transport.

The reconstruction defines how files are returned as output, and it stores a record when a send is accepted:
[send-message schema](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/source/host/runner/tools/send-message-schema.ts),
[acceptance records](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/source/host/extensions/transcript/send-acceptance.ts).
The inference router for Cursor/Claude Code/Codex/OpenRouter, routed MCP tools, local usage tracking, the optional local Docker
connector and the router-settings UI are described by the author as later experiments. They are not evidence that official
Grok Bot 0.18 shipped those features.

We also checked two follow-up repositories:

| Repository | Fixed commit | What we found |
| --- | --- | --- |
| `ElizantOS/ant-bots` | [`1378374adf12e10cb4871c0d2bdac3343049ad6e`](https://github.com/ElizantOS/ant-bots/tree/1378374adf12e10cb4871c0d2bdac3343049ad6e) | Still built on the 0.18 reconstruction. Its later activity does not show a new upstream reconstruction. |
| `smartyua/Grok-Bot-reconstruction-` | [`2beb2f9409ed507385c09753d33977591aa59963`](https://github.com/smartyua/Grok-Bot-reconstruction-/tree/2beb2f9409ed507385c09753d33977591aa59963) | Its mention of 0.43 is about comparing against an installed app. The reconstructed base is still 0.18. |

We did not find a newer reconstruction that ties back to a newer shipped version we could verify.
That is the result of a limited search. It does not prove that none exists.

## What we took, and what we left for later

This PR takes the useful boundaries and applies them to the existing Python system:

- explicit provider and channel capabilities;
- separate speech settings;
- streamed progress kept apart from the committed history;
- file delivery through the existing Outbox, where the file cannot change after it is created and downloading needs authentication.

The [artifact contract](ARTIFACTS.md) describes its limits, how long files are kept, and what happens when delivery is uncertain.

Possible later work: acceptance obligations (an idea from Grok), durable background-job state, structured questions, and priority lanes.
None of these is implemented here. This PR does not replace the SDK agent loop, migrate storage, promise to roll back effects in
the outside world, or give the agent any new permission to run unattended.
