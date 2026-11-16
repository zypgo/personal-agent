# Memory and context recovery

## Nightly memory passes

A dream writes a durable `dream:pending:<run_id>` receipt before changing memory. A successful
pass is reviewed for excessive deletion before the receipt is removed and its digest cursor is
advanced. A failed, budget-stopped, cancelled or timed-out pass reverts its candidate edits. A
restore error retains the receipt for recovery; later owner edits must not be overwritten to make
recovery appear successful. An owner-edit conflict sets `needs_review` on the same receipt and
queues one durable notification. It does not advance the success cursor, pretend rollback
completed, or block startup and unrelated inbox work. Genuine IO errors remain retryable errors.

The receipt spans approval rounds. Waiting for approval is not a failed night. On startup, a pass
with a saved execution checkpoint is preserved. After approval resume, a failure or interruption
closes that checkpoint, reverts the candidate edits and queues a durable notice. The old snapshot
cannot execute after rollback. A successful resume uses the same final review as an ordinary pass.

Startup integration calls `memory.recover()` first, then `Dreamer.recover()`, under the engine lock.
The latter only processes receipts from this protocol; it does not revisit old successful runs.
Receipt removal, memory journal updates, cursor changes and notices share the database transaction.

### Resolving an owner-edit conflict

Run `pa dream review` (or `pa dream review list`) to see each conflicted pass, its affected paths
and version numbers. `pa memory history <path>` shows prior versions for comparison. Review the
current notes: some changes from the unfinished pass may remain because its automatic undo was
aborted as a whole.

After deciding to retain the current memory, run:

```sh
pa dream review resolve RUN_ID --keep-current
```

This explicit owner action closes only the review receipt. It never overwrites files, erases
versions, or advances the successful dream cursor. There is no force-overwrite option. While a
review remains open, the receipt continues protecting the run and version evidence from normal
retention pruning; recovery and repeated finish calls leave it for the owner instead of retrying
the conflict or sending repeated notices.

## Context limits and usage

`runtime.run_timeout_s` covers asynchronous preparation, compaction and the model/tool loop as one
deadline. `context.timeout_s` separately limits a compaction attempt, including its optional
second response. A compaction timeout leaves the original visible history intact and sets the
retry cooldown; the main run can continue if its own deadline and budget permit it.

Each received summary response records usage immediately, including rejected empty/truncated
answers, cached input, cache writes and reasoning tokens. Budget checks run before each model
request, including retries. Provider failure, timeout or cancellation cannot erase a previous
response's charge or count it twice. Charges remain estimates based on received provider usage;
an interrupted request that supplies no usage cannot be measured locally.

## Retrieving details after compaction

`search_history` searches messages exchanged with the owner. `search_tool_history` searches
recorded tool outputs across sessions, including those hidden by compaction, reset or a tombstone.
It scans at most 256 log records per search and returns `next_before_id` to continue into older
history. A result's stable `result_id` supports reads in pages of at most 6,000 characters using
`next_offset`. An empty search page is not proof that older history has no match.

Each result includes the source tool and recorded arguments where available, session, run, call
ID, timestamp and audit outcome. Missing provenance is labelled unknown. This retrieves the
recorded output; bytes already clipped by the original tool guard cannot be recovered.

Output is secret-redacted before slicing, bounded and fenced. All retrieved tool text, including
old errors and local reads, taints the current run. Historical permission is never inherited.
Untrusted webhook runs cannot access this tool. Existing policy still decides subsequent actions.

## Evaluation coverage

The built-in cases include counterexamples for old news bundled into a new message, a Chinese
prefix followed by an English answer, excessive Chinese sentences, a reply that mentions a task
while denying it is open, and a task incorrectly marked completed. Style checks measure Han share
among letters, total character count and sentence fragments; task files additionally require an
open status pattern.

These offline scripts validate the checks and harness effects. They are explicit lexical/status
assertions, not general semantic understanding or evidence of real-model behavioral accuracy.
Paraphrases, context-dependent negation and other languages still require inspection of live
trial output. No paid model call is required by these regression tests.
