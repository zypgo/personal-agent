# Memory and context recovery

## Nightly memory passes

Before a dream changes memory, it writes a durable `dream:pending:<run_id>` receipt. When a pass succeeds, it's checked for too many deletions. Only then is the receipt removed and its digest cursor moved forward. If a pass fails, runs out of budget, is cancelled or times out, its candidate edits are reverted. If the restore itself fails, the receipt stays so recovery can try again. Later owner edits must never be overwritten just to make recovery look successful. If an owner edit conflicts, the same receipt gets `needs_review` and one durable notification is queued. In that case the success cursor doesn't move, nobody pretends the rollback finished, and startup and unrelated inbox work aren't blocked. Real IO errors stay retryable errors.

The receipt spans approval rounds. Waiting for approval isn't a failed night. On startup, a pass with a saved execution checkpoint is kept. After the approval comes back, if the pass fails or is interrupted, that checkpoint is closed, the candidate edits are reverted and a durable notice is queued. The old snapshot can't run after a rollback. A successful resume goes through the same final review as an ordinary pass.

At startup, `memory.recover()` runs first, then `Dreamer.recover()`, both under the engine lock. `Dreamer.recover()` only handles receipts from this protocol. It doesn't go back over old successful runs. Removing the receipt, updating the memory journal, moving the cursor and queuing notices share one database transaction.

### Resolving an owner-edit conflict

Run `pa dream review` (or `pa dream review list`) to see each conflicted pass with its affected paths and version numbers. `pa memory history <path>` shows earlier versions so you can compare. Look over the current notes. Some changes from the unfinished pass may still be there, because its automatic undo was stopped as a whole.

Once you've decided to keep the current memory, run:

```sh
pa dream review resolve RUN_ID --keep-current
```

This is an explicit action by you, and it only closes the review receipt. It never overwrites files, erases versions or moves the successful dream cursor. There's no option to force an overwrite. While a review is open, the receipt keeps protecting the run and the version evidence from normal retention pruning. Recovery and repeated finish calls leave it for you. They don't retry the conflict or send repeated notices.

## Context limits and usage

`runtime.run_timeout_s` is one deadline that covers the async preparation, the compaction and the model/tool loop. `context.timeout_s` separately limits one compaction attempt, including its optional second response. If compaction times out, the original visible history stays as it is and the retry cooldown is set. The main run can carry on if its own deadline and budget allow it.

Each summary response is counted as soon as it arrives. That includes empty or truncated answers that were rejected, cached input, cache writes and reasoning tokens. Budget checks run before every model request, retries included. If the provider fails, times out or is cancelled, the charge of an earlier response is neither erased nor counted twice. Charges are still estimates based on the usage the provider sent back. If a request is interrupted and no usage came back, it can't be measured locally.

## Finding details after compaction

`search_history` searches the messages exchanged with the owner. `search_tool_history` searches the recorded tool outputs across sessions, including the ones hidden by compaction, a reset or a tombstone. It scans at most 256 log records per search and returns `next_before_id` so you can continue into older history. Each result has a stable `result_id`, which you can read in pages of at most 6,000 characters using `next_offset`. An empty search page doesn't prove that older history has no match.

Each result includes the source tool and the recorded arguments where available, plus the session, run, call ID, timestamp and audit outcome. Missing provenance is labelled unknown. This gets back the recorded output. Bytes that the original tool guard already cut off can't be recovered.

The output is redacted of secrets before it's sliced, and it's size-limited and fenced. All the tool text retrieved this way, including old errors and local reads, taints the current run. Permission from the past is never inherited. Untrusted webhook runs can't use this tool. The existing policy still decides what happens next.

## What the evaluations check

The built-in cases include counterexamples for: old news bundled into a new message, a Chinese prefix followed by an English answer, too many Chinese sentences, a reply that mentions a task while denying it's open, and a task wrongly marked as completed. The style checks measure the share of Han characters among the letters, the total character count and sentence fragments. Task files also need an open-status pattern.

These offline scripts check the checks and the harness effects. They're explicit text and status assertions. They aren't general semantic understanding, and they aren't evidence that a real model behaves accurately. Paraphrases, context-dependent negation and other languages still need a person to look at live trial output. These regression tests don't need a paid model call.
