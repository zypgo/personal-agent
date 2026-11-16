# Durable recovery boundaries

This change addresses combined failure paths that the individual primitive tests did not cover.

## Approval rounds

`run_states.generation` identifies the suspension that an `approval_response` may resume. The
generation is copied into the event payload and the existing event id. A retried response from an
earlier round is acknowledged without touching the current round. A still-pending approval is
never interpreted as a refusal. Legacy response ids are read during the transition.

Superseding a suspended run is one database transaction: closing approvals, supplying missing
tool results, recording their outcomes, removing the snapshot and ending the run. Results from
an earlier completed approval round are not appended a second time.

## Memory commit and projection

`memory_documents` stores a path's desired kind/bytes, revision, last projected revision and
previous file snapshot. A mutation and its `memory_versions` entry commit together. File writes
are registered through `Database.after_commit()` and only begin after the outermost transaction
commits; savepoint rollback also discards its callbacks. Reads in the transaction see staged
bytes, so a later edit can use a file created earlier in the same `after_sent` batch.

`MemoryStore.recover()` projects outstanding rows. It writes complete desired contents with an
atomic replacement, then acknowledges the revision. Repeating either side of a process crash
cannot append again. Outbox delivery state, transcript and desired memory contents share the
same database transaction, so after a commit the dispatcher does not resend merely because a
file projection remains unfinished. Recovery must run before recovering interrupted dreams.
The open-task index and directory listings also use these logical snapshots: a task remains
visible when its file projection fails, and reads inside a transaction see staged task changes.

The files remain editable. Changes to an already projected file are imported and versioned as
owner edits. If a pending projection finds content different from both its previous and desired
snapshot, it preserves and imports that owner edit. A symlink outside the memory root is refused.
An unexpected file in a directory prevents its removal; recovery never recursively erases new
owner files. An I/O failure leaves the committed projection pending and logged for retry.

`revert_run()` is an atomic logical operation. It raises `MemoryRestoreError` instead of reporting
a partial rollback as success, and refuses to overwrite later owner/other-run edits. Callers must
keep their recovery receipt until this succeeds. Bootstrap respects known deletions, so restart
does not recreate a deleted template before a pending dream can restore its original contents.
Retention keeps the run and approval records for every pending dream, plus the entire memory
version sequence from the earliest pending edit onward. Later owner edits in that sequence are
also needed to detect unsafe rollback conflicts. Completing or resolving the review removes its
receipt and releases those records to normal retention; older unrelated history can still expire.

Existing files are imported lazily. A complete backup still copies the whole home, including
configuration, media and memory files; a database-only backup is not a replacement for that.
The guarantees tested here concern process termination. They do not claim power-loss durability
for SQLite's existing `synchronous=NORMAL` setting or for every filesystem.

## Usage accounting

`RunStore.add_usage()` keeps its API, but now appends `usage_events` with the actual call time in
the same transaction as the per-run totals. Daily limits query that ledger. Resuming a previous
day's run therefore charges today's calls to today. Old aggregate totals are migrated once with
their original start-time attribution; historical call times cannot be reconstructed.

## Terminal failure notifications

The `dead` transition writes a `dead_event_notices` receipt in the same transaction. The worker
removes the receipt only in the transaction that enqueues the owner's notice and, if necessary,
abandons a suspended run. The daemon also wakes for these receipts when no runnable event remains.
Retention must keep events referenced by unprocessed receipts.

## Regression coverage

`test_durability_recovery.py` combines multi-round approvals with crash replay, interrupts
supersession, crosses midnight during a run, and interrupts terminal-notice settlement.
`test_memory_journal.py` exercises transaction/savepoint rollback, both sides of projection,
owner edits, recovery path safety, atomic undo and outbox commit recovery. Existing crash and
real subprocess suites remain part of the gate; no paid model or external service is required.
