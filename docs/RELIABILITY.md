# Reliability and phone access

This round fixes the fourteen findings from the review of `38cd5f7`. It then adds recovery and phone-setup features around the existing single-user design. It doesn't replace the inbox, the scheduler or the Agents SDK.

## Review findings

| Finding | Change | Regression coverage |
|---|---|---|
| R01: a header placeholder lets extra data through | Only fixed credential header templates keep the read exemption. Other headers supplied by the model count as outbound writes | `test_web_tools.py` |
| R02: MCP errors lose their source | `ToolResult` keeps the error status and the untrusted status apart. Error text from a remote server still taints later actions | `test_tool_results.py`, MCP tests |
| R03: a failed dream skips undo | Persistent review receipts cover success, budget stops, exceptions, cancellation, timeouts and restarts. A suspended dream keeps its receipt | `test_dreaming.py`, `test_dream_approval_recovery.py` |
| R04: a stale approval event resumes a newer round | The snapshot and the resume event share a generation. A pending approval stays pending. Stale events can't abandon newer rounds | `test_durability_recovery.py` |
| R05: replacing a run leaves half-done state | Closing approvals, tool results, audit, snapshot removal and ending the run share one transaction | `test_durability_recovery.py` |
| R06: writing the file and the database separately duplicates memory | The desired content and the versions are committed in SQLite. File writes that are safe to repeat run after the commit and recover after an interruption | `test_memory_journal.py` |
| R07: costs across midnight disappear | Every usage update records its real timestamp. Daily budgets read this ledger | `test_durability_recovery.py` |
| R08: retrying a summary loses earlier usage | Each response is counted right away, even if a later attempt fails | `test_compaction_failures.py` |
| R09: the compaction timeout isn't used | Compaction has its configured deadline and also uses up the run's overall deadline | `test_compaction_failures.py` |
| R10: the final failure notice needs another event | Dead-event receipts are saved atomically. The daemon handles them without waiting for new work | `test_durability_recovery.py` |
| R11: the response size cap is applied after decompression | Responses with a body and a non-identity encoding are rejected before automatic decompression | `test_ssrf.py` |
| R12: detached output leaves child processes running | Every exit path cleans up the process group. Cancelling also cleans up Docker containers | `test_process_lifecycle.py` |
| R13: voice dedup runs after paid work | Durable ingress receipts claim the message before transcription, cache the words once transcribed, and prevent automatic re-billing after an uncertain interruption | `test_ingress_receipts.py` |
| R14: semantic counterexamples falsely pass | Graders check the stated language and length, old and new facts, and the state of unfinished tasks. Adversarial negative cases must fail | evaluation tests |

## What's new underneath

- **Memory that recovers after a crash.** SQLite records the logical edits. The Markdown files stay readable and editable. Owner edits are imported, and recovery never quietly overwrites a later edit. The task indexes read the committed content even while a file write is pending.
- **Conflict review you can see.** If a dream can't safely undo later owner edits, it keeps its evidence and queues one review notice, without blocking unrelated work. `pa dream review` lists the conflicts. `pa dream review resolve RUN_ID --keep-current` closes a review explicitly, without rewriting files and without treating the interrupted input as successfully processed.
- **Usage history for every call.** Costs follow the timestamp of the call across approval pauses and midnight, and the per-run totals are kept.
- **Incoming-message preprocessing that survives failures.** A transcription that already finished can be reused after a failure that happens before the message goes into the inbox. Deliveries that arrive at the same time can't start a second transcription. If the outcome of an external call is unknown, you have to send a new message.
- **Looking up original tool results.** `search_tool_history` gives limited, paged access to the original results, with run, call and source metadata, after the context has been compacted. Results it returns are scrubbed and treated as untrusted. It's separate from the conversation search you can see.
- **What's going on.** `/api/activity` and the phone's Recent tasks view tell these states apart: pending, waiting for approval, paused by the budget, delayed retry, and final failure. This view never shows raw internal exceptions or tool arguments.
- **Phone setup and checking.** `pa web connect --via lan|tailscale` checks the endpoints, keeps the comments in your configuration and prints a one-time pairing QR code. `pa web check` verifies a signed, fresh challenge from the current agent home, so an unrelated HTTP 200 doesn't count.

See [RECOVERY.md](RECOVERY.md) for the design of transactions and recovery, [memory-and-context-recovery.md](memory-and-context-recovery.md) for conflict review, and [MOBILE.md](MOBILE.md) for setup.

## Upgrading and rolling back

Before you upgrade an existing installation, stop the agent and back up the whole `PA_HOME` directory. `pa db backup` also makes a consistent copy of the database. That copy alone doesn't include the configuration, media or Markdown files you edited by hand.

The database migration only goes forward. It adds approval generations, per-call usage, terminal notification receipts, memory write records and ingress receipts. Existing memory files are imported lazily. Existing aggregate costs are migrated once and keep their old start-time attribution, because the timestamps of the individual earlier calls aren't available.

When the engine recovers, committed memory writes are settled before interrupted dreams are reviewed. An interrupted voice preprocessing claim is marked uncertain. Words that were already transcribed stay available for a retry, with no new transcription call. Pending terminal notices survive a restart and are kept until they're queued. Completed receipts and usage history follow the existing retention windows. Pending memory writes are never pruned as old history. Pending dream receipts, including conflicts waiting for review, also keep their runs, approvals and memory version chains until the review is resolved.

If you may need to roll back, keep the old executable together with its matching whole-home backup. Older code refuses a newer schema. Don't lower the schema version by hand.

## Checks, and what's still not covered

`scripts/check.sh` runs formatting, lint, strict types, all the offline Python tests and Node's TAP tests. `scripts/check.sh --python 3.11` tests with the oldest supported Python. The PR workflow runs on Linux and macOS with Python 3.11/3.13 and Node 24. It never turns on paid live tests.

The new tests combine persistence boundaries with retries, cancellation, owner edits and multi-round approvals. They prove fixed, repeatable properties. They don't prove that a real model will always read every instruction correctly. The improved evaluation checks cover the counterexamples they name. They still aren't a general proof that natural-language answers are correct.

These are choices we're keeping on purpose: single-user deployment, SQLite `synchronous=NORMAL`, a credential broker in the same process, and trusted installed Python/MCP programs. These changes don't claim to survive a power loss, to isolate an arbitrary plugin, to track memory provenance across every run, or to deliver exactly once when a remote service can't confirm the outcome.

Phone HTTPS/Tailscale setup is covered by command and protocol tests. Real iOS/Android devices, a live tailnet, a real Docker daemon and paid model behavior still need testing in your deployment. The PWA has no background Web Push. Telegram remains the option for proactive alerts when the browser is closed. The host computer has to stay awake and connected.
