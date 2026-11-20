# Sending files to you

`publish_artifact(path, name=None, description="")` takes a finished file from the workspace and turns it into a private snapshot that can't be changed. It then queues one notification. The tool returns a queue receipt. Only the existing dispatcher confirms delivery and records the file in the conversation history.

## How to use it

Ask the agent to create a report, spreadsheet, archive or any other file inside its workspace, and then publish it. For example:

```text
Create workspace/reports/summary.csv and publish reports/summary.csv with the caption
"The requested summary."
```

Paths are relative to `PA_HOME/workspace`. Absolute paths inside that directory also work. Publishing rejects `..` paths, symbolic links, hard links, directories, pipes and devices. The file must be fully written before you publish it. With this tool, the agent can't publish memory, credentials, arbitrary files from your home directory, or installed skills.

The tool is available to owner runs and scheduled runs when file tools and artifacts are turned on. It uses the existing `net_write` policy category, so the usual approval and taint rules still apply. It isn't available to untrusted runs or memory-maintenance runs.

Policy checks use the canonical path relative to the workspace. So `private/report.txt`, `./private/report.txt` and its absolute path in the workspace all match a rule like `publish_artifact:private/*`. Deny and always-ask rules also look at the original spelling and the canonical absolute path. These checks only compare text. They never read the file before authorization.

## How it's delivered

| Channel | Result |
| --- | --- |
| Web | A file card in the conversation history. Downloading needs the existing paired browser session. |
| Telegram | One `sendDocument` multipart upload, with the notification text as the caption. The recipient must still be paired. |
| Other channels, including DingTalk | A text notice points the owner to the signed-in Web conversation. If Web is turned off, the notice says to turn it on. No public link, and no link with a token in it, is made up. |

All paired Web devices belong to the same owner, just like the existing conversation history. This isn't a system for sharing between several users. Knowing an artifact ID is never enough to download the file.

`GET /api/artifacts/{id}` returns `401` without a valid Web session, `404` for a snapshot that isn't available, and `410` after it has expired. Downloads use `Content-Disposition: attachment`, `application/octet-stream`, `nosniff`, a sandbox CSP and `private, no-store`. HTML and SVG are downloaded as files. They're never shown under the application's own origin.

The history format stays stable:

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

The server builds this metadata from its own store. Clients work out the authenticated download path from `id`. They never receive a workspace path, a storage path or an access token. The MIME type is a display hint based on the file name. It doesn't prove what the content really is. History rebuilds file metadata from the snapshot tied to the Outbox ID. Attachment fields that a plugin supplies can't create or rename a file card.

## Limits and how long files are kept

```toml
[artifacts]
enabled = true
max_file_bytes = 10485760      # 10 MiB; configurable up to 50 MiB
max_total_bytes = 209715200    # 200 MiB of unexpired snapshots
max_active_files = 1000       # empty files also consume a slot
retention_days = 30
```

Descriptions are limited to 500 characters. A download file name can't contain path separators or control characters, and can't be longer than 240 UTF-8 bytes. The total quota must have room for at least one file of the maximum size. If you go over a limit, you get an error and nothing is queued.

Names and captions that contain known configured credentials, or anything that looks like a credential, are rejected before publishing. That includes values that show up after invisible characters are normalized. Safe captions use the same display normalization as review text. Approval can't override this check. It only checks metadata. It isn't general file-content DLP: the binary content is neither scanned nor rewritten, and the owner is responsible for the files they ask to export.

Expired bytes can't be accessed right away. The daily daemon upkeep, startup recovery and new publications clean up expired blobs and ones that never committed. The metadata stays in SQLite for history and deduplication. The byte quota doesn't limit how long database history is kept. Files the owner already saved from the browser or Telegram are outside the local retention.

## Crashes and retries

The store opens each part of the source path with directory descriptors and `O_NOFOLLOW`, including the workspace root. It checks the open inode before and after a bounded read. Snapshots live outside the writable workspace, in a directory only the owner can access, with `0600` files. Downloads and uploads check the stored length and SHA-256 before handing the bytes to a channel.

The blob is written and fsynced first. After that, its metadata and the Outbox message are committed in one SQLite transaction. If something fails, the notification is rolled back. If the process crashes before the commit, it can leave a blob that isn't listed, and recovery removes it. A committed publication survives a restart and doesn't depend on the original workspace file. This is about recovering from process crashes. It isn't a claim that every filesystem and SQLite setup survives a power loss.

Deduplication uses `(session_key, work_id, source_path)`. Retrying a task returns its original snapshot and notification, even if the workspace file was changed or deleted. Different paths in one task are separate results. To publish a corrected version in the same task, use a new path. A later task has a new work ID and can publish the original path again.

Files the owner asks for ignore quiet hours. Scheduled files respect them. If an upload's outcome is uncertain, it's always parked as `maybe_sent`. That includes interactive uploads and uploads cut off by a process crash. A Telegram connection failure before sending, or an explicit rate limit, can be retried. Permanent refusals fail. Read or write failures that happen late, and server errors, aren't replayed automatically. `pa outbox` shows deliveries that are still unresolved. The owner can check the chat and ask for a fresh publication if needed. The protocol doesn't claim that external delivery happens exactly once.

Telegram checks the pairing and the selected recipient again after it reads and verifies the snapshot, right before the upload. If access is revoked during that read, nothing is uploaded.

## Tests

`tests/test_artifacts.py` covers workspace boundaries, path swaps, concurrent writes, immutable snapshots, quota, expiry, transaction rollback, restart and concurrent duplicate publication. `tests/test_artifacts_delivery.py` covers authenticated downloads, active content, history, real multipart encoding over a fake HTTP transport, recipient checks, Telegram error classes, quiet hours and crash recovery. The tests make no external API calls. `tests/test_artifacts_policy.py` covers path aliases, restrictive policy rules and credential checks at the approval and publication boundaries.
