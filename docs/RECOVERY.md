# What survives a crash

This change closes gaps in cases where several failures happen together. The tests for the individual pieces didn't cover those cases.

## Approval rounds

`run_states.generation` says which pause an `approval_response` is allowed to resume. The generation is copied into the event payload and into the existing event id. If an answer from an earlier round is sent again, it's acknowledged and the current round isn't touched. An approval that's still pending is never treated as a refusal. During the transition, old-style response ids are still read.

Replacing a paused run with a newer one is a single database transaction. It closes the approvals, fills in the missing tool results, records the outcomes, removes the snapshot and ends the run. Results from an earlier completed approval round aren't appended a second time.

## Memory: commit first, then write the files

`memory_documents` stores, for each path: the kind and bytes it should have, its revision, the last revision written to disk, and the previous file snapshot. A change and its `memory_versions` entry are committed together. File writes are registered through `Database.after_commit()` and only start after the outermost transaction commits. A savepoint rollback also drops its callbacks. Reads inside the transaction see the staged bytes, so a later edit can use a file created earlier in the same `after_sent` batch.

`MemoryStore.recover()` writes out the rows that are still waiting. It writes the complete content with an atomic replacement, and then acknowledges the revision. If the process crashes on either side, repeating the step can't append again. The Outbox delivery state, the transcript and the memory content that should be on disk share one database transaction. So after a commit, the dispatcher doesn't resend just because a file write isn't finished. Recovery must run before interrupted dreams are recovered. The open-task index and the directory listings also use these logical snapshots: a task stays visible when its file write fails, and reads inside a transaction see the staged task changes.

You can still edit the files. Changes to a file that's already been written are imported and versioned as owner edits. If a pending write finds content that differs from both the previous and the desired snapshot, it keeps that owner edit and imports it. A symlink that points outside the memory root is refused. An unexpected file in a directory stops the directory from being removed. Recovery never wipes out new owner files recursively. If an I/O failure happens, the committed write stays pending and is logged for retry.

`revert_run()` is one atomic logical operation. It raises `MemoryRestoreError` instead of reporting a partial rollback as a success, and it refuses to overwrite later edits made by the owner or other runs. Callers must keep their recovery receipt until this succeeds. Bootstrap respects deletions it knows about, so a restart doesn't re-create a deleted template before a pending dream can restore its original contents. Retention keeps the run and approval records of every pending dream, plus the whole memory version sequence from the earliest pending edit onward. Later owner edits in that sequence are also needed to spot an unsafe rollback conflict. Finishing or resolving the review removes its receipt and releases those records to normal retention. Older, unrelated history can still expire.

Existing files are imported lazily. A complete backup still copies the whole home directory, including configuration, media and memory files. A database-only backup doesn't replace that. The guarantees tested here are about the process being killed. They don't claim to survive a power loss for SQLite's existing `synchronous=NORMAL` setting or for every filesystem.

## Usage accounting

`RunStore.add_usage()` keeps its API. Now it also adds a row to `usage_events` with the actual call time, in the same transaction that updates the per-run totals. Daily limits read that ledger. So if you resume yesterday's run, today's calls are charged to today. The old aggregate totals were migrated once and kept their original start-time attribution, because the historical call times can't be rebuilt.

## Notices for events that failed for good

When an event moves to `dead`, a `dead_event_notices` receipt is written in the same transaction. The worker removes the receipt only in the transaction that queues the owner's notice and, if needed, abandons a suspended run. The daemon also wakes up for these receipts when no runnable event is left. Retention must keep the events that unprocessed receipts point to.

## Regression tests

`test_durability_recovery.py` combines multi-round approvals with crash replay, interrupts a replacement, crosses midnight during a run, and interrupts the settling of a terminal notice. `test_memory_journal.py` exercises transaction and savepoint rollback, both sides of a file write, owner edits, path safety during recovery, atomic undo and Outbox commit recovery. The existing crash and real-subprocess suites remain part of the gate. No paid model or external service is needed.
