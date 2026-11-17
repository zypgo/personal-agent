# File delivery

`publish_artifact(path, name=None, description="")` turns a finished workspace file into a
private, immutable snapshot and queues one notification. The tool returns a queue receipt.
Only the existing dispatcher confirms delivery and records the file in conversation history.

## Use

Ask the agent to create a report, spreadsheet, archive or other file inside its workspace,
then publish it. For example:

```text
Create workspace/reports/summary.csv and publish reports/summary.csv with the caption
"The requested summary."
```

Paths are relative to `PA_HOME/workspace`; absolute paths inside that directory also work.
Publication rejects parent traversal, symbolic links, hard links, directories, pipes and
devices. A file must finish writing before publication. The agent cannot publish memory,
credentials, arbitrary home files or installed skills directly through this tool.

The tool is available to owner and scheduled runs when file tools and artifacts are enabled.
It uses the existing `net_write` policy category: applicable approval and taint rules continue
to apply. It is unavailable to untrusted and memory-maintenance runs.

Policy details use the canonical workspace-relative path, so `private/report.txt`,
`./private/report.txt` and its absolute workspace path all match a rule such as
`publish_artifact:private/*`. Deny/always-ask rules also inspect the original spelling and
canonical absolute path. These checks are lexical and never read a file before authorization.

## Delivery

| Channel | Result |
| --- | --- |
| Web | A file card in conversation history, with a download requiring the existing paired browser session. |
| Telegram | One `sendDocument` multipart upload, with the notification text as its caption. The recipient must still be paired. |
| Other channels, including DingTalk | A text notification directs the owner to the signed-in Web conversation. If Web is disabled, it explicitly says to enable it. No public or token-bearing link is invented. |

All paired Web devices belong to the same owner, as with existing conversation history; this
is not a multi-tenant sharing system. An artifact ID alone never grants download access.

`GET /api/artifacts/{id}` returns `401` without a valid Web session, `404` for an unavailable
snapshot, and `410` after expiry. Downloads use `Content-Disposition: attachment`,
`application/octet-stream`, `nosniff`, a sandbox CSP and `private, no-store`. HTML and SVG are
downloaded as files rather than rendered under the application's origin.

The stable history contract is:

```json
{
  "kind": "artifact",
  "meta": {
    "run_id": "the-producing-run",
    "artifacts": [{
      "id": "32-lowercase-hex-characters",
      "name": "summary.csv",
      "mime": "text/csv",
      "size": 1234,
      "sha256": "64-lowercase-hex-characters",
      "expires_at": 1800000000
    }]
  }
}
```

The server creates this metadata from its store. Clients derive the authenticated download
path from `id`; they never receive a workspace/storage path or an access token. MIME is a
filename-based display hint, not proof of the content's type.
History reconstructs file metadata from the snapshot bound to the Outbox ID; arbitrary
attachment fields supplied by a plugin cannot create or rename a file card.

## Limits and retention

```toml
[artifacts]
enabled = true
max_file_bytes = 10485760      # 10 MiB; configurable up to 50 MiB
max_total_bytes = 209715200    # 200 MiB of unexpired snapshots
max_active_files = 1000       # empty files also consume a slot
retention_days = 30
```

Descriptions are limited to 500 characters. Download filenames cannot contain path separators,
control characters or more than 240 UTF-8 bytes. The total quota must accommodate at least
one maximum-size file. Exceeding a limit returns an error and queues nothing.

Names and captions containing known configured credentials or recognizable credential formats
are rejected before publication, including values exposed after normalizing invisible characters.
Safe captions use the same display normalization as review text. Approval cannot override this
check. This is a metadata check, not general file-content DLP: the binary payload is neither
scanned nor rewritten, and the owner remains responsible for the file they ask to export.

Expired bytes are inaccessible immediately. Daily daemon upkeep, startup recovery and new
publications reclaim expired/uncommitted blobs. Metadata remains in SQLite for history and
deduplication; the byte quota does not impose a database-history retention limit. Browser
and Telegram downloads already saved by the owner are outside local retention.

## Crash and retry semantics

The store opens each source path component using directory descriptors and `O_NOFOLLOW`,
including the workspace root. It checks the open inode before and after a bounded read.
Snapshots live outside the writable workspace, in an owner-only directory with `0600` files.
Downloads and uploads check stored length and SHA-256 before handing bytes to a channel.

The blob is written and fsynced before its metadata and Outbox message commit in one SQLite
transaction. A failure rolls back the notification; a process crash before commit can leave
an unlisted blob, which recovery removes. A committed publication survives restart without
depending on the original workspace file. These are process-recovery semantics, not a claim
that every filesystem and SQLite configuration is power-loss proof.

Deduplication uses `(session_key, work_id, source_path)`. Retrying a task returns its original
snapshot and notification, even if the workspace file changed or was deleted. Different
paths in one task are separate results. To publish a corrected revision in the same task,
use a new path; a later task has a new work ID and can publish the original path again.

Owner-requested files bypass quiet hours, while scheduled files respect them. Uncertain
artifact uploads are always parked as `maybe_sent`, including interactive uploads and
uploads interrupted by a process crash. A Telegram connection failure before sending or an
explicit rate limit can be retried; permanent refusals fail; late read/write failures and
server errors are not automatically replayed. `pa outbox` exposes unresolved delivery.
The owner can inspect the chat and request a fresh publication if needed. The protocol does
not claim exactly-once external delivery.
Telegram rechecks pairing and the selected recipient after reading/verifying the snapshot,
immediately before upload; a revocation during that read prevents the upload.

## Verification

`tests/test_artifacts.py` covers workspace boundaries, path swaps, concurrent writes, immutable
snapshots, quota, expiry, transaction rollback, restart and concurrent duplicate publication.
`tests/test_artifacts_delivery.py` covers authenticated downloads, active content, history,
real multipart encoding over a fake HTTP transport, recipient checks, Telegram error classes,
quiet hours and crash recovery. Tests make no external API calls.
`tests/test_artifacts_policy.py` covers path aliases, restrictive policy rules and credential
checks at the approval and publication boundaries.
