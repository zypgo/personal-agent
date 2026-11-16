# Reliability and phone access

This iteration fixes the fourteen findings from the review of `38cd5f7`, then adds recovery and
phone setup capabilities around the existing single-user architecture. It does not replace the
inbox, scheduler or Agents SDK.

## Review findings

| Finding | Change | Regression coverage |
|---|---|---|
| R01: header placeholder exempts extra data | Only fixed credential header templates retain the read exemption; other model-supplied headers are outbound writes | `test_web_tools.py` |
| R02: MCP errors lose provenance | `ToolResult` separates error and untrusted status; remote error text still taints later actions | `test_tool_results.py`, MCP tests |
| R03: failed dreaming skips undo | Persistent review receipts cover success, budget stops, exceptions, cancellation, timeouts and restart; suspended dreams retain their receipt | `test_dreaming.py`, `test_dream_approval_recovery.py` |
| R04: stale approval event resumes a newer round | Snapshot and resume event share a generation; pending stays pending; stale events cannot abandon newer rounds | `test_durability_recovery.py` |
| R05: supersede leaves partial state | Approval closure, tool results, audit, snapshot removal and run termination share one transaction | `test_durability_recovery.py` |
| R06: file/DB dual writes duplicate memory | Desired contents and versions commit in SQLite; idempotent file projections run after commit and recover after interruption | `test_memory_journal.py` |
| R07: cross-midnight costs disappear | Every usage update records its actual timestamp; daily budgets query this ledger | `test_durability_recovery.py` |
| R08: summary retry loses earlier usage | Each response is accounted for immediately, including when a later attempt fails | `test_compaction_failures.py` |
| R09: compaction timeout is unused | Compaction has its configured deadline and consumes the run's overall deadline | `test_compaction_failures.py` |
| R10: final failure notification needs another event | Dead-event receipts persist atomically; the daemon drains them without needing new work | `test_durability_recovery.py` |
| R11: response cap applies after decompression | Body-bearing responses with non-identity encoding are rejected before automatic decompression | `test_ssrf.py` |
| R12: detached output leaves child processes running | Every exit path cleans up the process group; cancellation also cleans Docker containers | `test_process_lifecycle.py` |
| R13: voice dedup runs after paid work | Durable ingress receipts claim before transcription, cache successful words and prevent automatic rebilling after uncertain interruption | `test_ingress_receipts.py` |
| R14: semantic counterexamples falsely pass | Graders check the stated language/length, old/new facts and unfinished-task state; adversarial negative cases must fail | evaluation tests |

## New underlying capabilities

- **Recoverable memory projection.** SQLite records logical edits; Markdown remains readable and
  editable. Owner edits are imported, and recovery does not silently overwrite a later edit.
  Task indexes read the committed logical contents even while a file projection is pending.
- **Explicit conflict review.** A dream that cannot safely undo later owner edits keeps its
  evidence and queues one review notice without blocking unrelated work. `pa dream review`
  lists conflicts; `pa dream review resolve RUN_ID --keep-current` explicitly closes a review
  without rewriting files or treating the interrupted input as successfully processed.
- **Per-call usage history.** Cost attribution follows the call's timestamp across approval pauses
  and midnight, while retaining per-run totals.
- **Durable inbound preprocessing.** Completed transcription can be reused after failure before
  inbox insertion. Concurrent deliveries cannot start another transcription. Unknown external
  outcomes require the owner to send a new message.
- **Original tool-result retrieval.** `search_tool_history` offers bounded, paginated access to
  original results with run/call/source metadata after context compaction. Retrieved results are
  scrubbed and treated as untrusted; this is separate from human-visible conversation search.
- **Work visibility.** `/api/activity` and the phone's Recent tasks view distinguish pending,
  approval waits, budget pauses, delayed retries and final failure. Raw internal exceptions and
  tool arguments are not exposed by this view.
- **Phone setup and diagnosis.** `pa web connect --via lan|tailscale` validates endpoints, preserves
  configuration comments and prints a one-time pairing QR. `pa web check` verifies a signed,
  fresh challenge from the current agent home, rather than accepting an unrelated HTTP 200.

See [RECOVERY.md](RECOVERY.md) for transaction/recovery design,
[memory-and-context-recovery.md](memory-and-context-recovery.md) for conflict review,
and [MOBILE.md](MOBILE.md) for setup.

## Upgrade and recovery

Before upgrading an existing installation, stop the agent and keep a backup of the whole
`PA_HOME` directory. `pa db backup` also provides a consistent database copy; that copy alone
does not include configuration, media or manually edited Markdown files.

The database migration is forward-only. It adds approval generations, per-call usage, terminal
notification receipts, memory projection records and ingress receipts. Existing memory files
are imported lazily. Existing aggregate costs are migrated once with their old start-time
attribution because historical individual call timestamps are unavailable.

On engine recovery, committed memory projections are settled before interrupted dreams are
reviewed. An interrupted voice preprocessing claim is marked uncertain; already completed words
remain available for retry without another transcription call. Pending terminal notices survive
restart and are retained until enqueued. Completed receipts and usage history follow the existing
retention windows; pending memory projections are never pruned as historical data.
Pending dream receipts, including conflicts awaiting review, also retain their runs, approvals
and memory version chains until the review is resolved.

Keep the old executable and its matching whole-home backup together if rollback is needed.
Older code will refuse a newer schema; do not manually lower the schema version.

## Validation and remaining boundaries

`scripts/check.sh` runs formatting, lint, strict types, all offline Python tests and Node's TAP
tests. `scripts/check.sh --python 3.11` exercises the oldest supported Python. The PR workflow
runs on Linux and macOS with Python 3.11/3.13 and Node 24; it never opts into paid live tests.

The new tests combine persistence boundaries with retries, cancellation, owner edits and
multi-round approvals. They establish deterministic invariants, not that a real model will
always interpret every instruction correctly. Improved evaluation checks cover their stated
counterexamples and are still not a general natural-language correctness proof.

The single-user deployment, SQLite `synchronous=NORMAL`, same-process credential broker and
trusted installed Python/MCP programs remain deliberate boundaries. These changes do not claim
power-loss durability, arbitrary-plugin isolation, full memory provenance across every run, or
exactly-once external delivery when a remote service cannot confirm an outcome.

Phone HTTPS/Tailscale setup is covered with command and protocol tests. Physical iOS/Android
devices, a live tailnet, a real Docker daemon and paid model behavior require deployment testing.
The PWA has no background Web Push; Telegram remains the existing option for proactive alerts
when the browser is closed. The host computer must remain awake and connected.
