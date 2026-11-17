"""A node's private durable receipts. Receiving a lease never means starting it twice."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_agent.nodes.transfers import identifier, validate_envelope


@dataclass(frozen=True)
class Receipt:
    attempt_id: str
    generation: int
    plan_hash: str
    resource_id: str
    payload: dict[str, Any]
    state: str
    admitted: bool
    desired_cancel: bool
    deadline_at: float
    sequence: int
    log_hash: str
    result: dict[str, Any] | None
    acknowledged: bool
    cleanup_state: str


def _receipt(row: sqlite3.Row) -> Receipt:
    return Receipt(
        attempt_id=row["attempt_id"],
        generation=row["generation"],
        plan_hash=row["plan_hash"],
        resource_id=row["resource_id"],
        payload=json.loads(row["payload"]),
        state=row["state"],
        admitted=bool(row["admitted"]),
        desired_cancel=bool(row["desired_cancel"]),
        deadline_at=row["deadline_at"],
        sequence=row["sequence"],
        log_hash=row["log_hash"],
        result=json.loads(row["result"]) if row["result"] else None,
        acknowledged=bool(row["acknowledged"]),
        cleanup_state=row["cleanup_state"],
    )


def execution_digest(payload: dict[str, Any]) -> str:
    keys = (
        "attempt_id",
        "job_id",
        "space_id",
        "generation",
        "plan_hash",
        "command",
        "timeout_s",
        "network",
        "input_manifest",
        "output_paths",
    )
    body = json.dumps({k: payload.get(k) for k in keys}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class NodeStore:
    def __init__(self, home: Path) -> None:
        self.home = home.expanduser().resolve()
        self.home.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.home.chmod(0o700)
        self.jobs_dir = self.home / "jobs"
        self.jobs_dir.mkdir(mode=0o700, exist_ok=True)
        if self.jobs_dir.is_symlink():
            raise ValueError("node jobs directory must not be a symlink")
        db_path = self.home / "receipts.db"
        if db_path.is_symlink():
            raise ValueError("node receipt database must not be a symlink")
        fd = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        db_path.chmod(0o600)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=1000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS receipts (
                attempt_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                plan_hash TEXT NOT NULL, digest TEXT NOT NULL, resource_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'received',
                admitted INTEGER NOT NULL DEFAULT 0, desired_cancel INTEGER NOT NULL DEFAULT 0,
                deadline_at REAL NOT NULL, sequence INTEGER NOT NULL DEFAULT 0,
                log_hash TEXT NOT NULL DEFAULT '', result TEXT,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                cleanup_state TEXT NOT NULL DEFAULT 'retained',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )"""
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> NodeStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get(self, attempt_id: str) -> Receipt | None:
        row = self._conn.execute(
            "SELECT * FROM receipts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return None if row is None else _receipt(row)

    def pending(self) -> list[Receipt]:
        return [
            _receipt(row)
            for row in self._conn.execute(
                "SELECT * FROM receipts WHERE cleanup_state != 'released' ORDER BY created_at"
            ).fetchall()
        ]

    def accept(self, payload: dict[str, Any], *, now: float | None = None) -> Receipt:
        validate_envelope(payload)
        now = time.time() if now is None else now
        digest = execution_digest(payload)
        attempt_id = payload["attempt_id"]
        resource = (
            "pa-v3-"
            + hashlib.sha256(f"{attempt_id}:{payload['generation']}".encode()).hexdigest()[:32]
        )
        timeout = float(payload.get("timeout_s", 900))
        # A server clock discrepancy may shorten work, but can never extend the local limit.
        remote_deadline = payload.get("deadline_at", now + timeout)
        if (
            not isinstance(remote_deadline, (int, float))
            or isinstance(remote_deadline, bool)
            or not math.isfinite(remote_deadline)
        ):
            raise ValueError("invalid execution deadline")
        deadline = min(now + timeout, float(remote_deadline))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT digest FROM receipts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    raise ValueError("attempt ID has already been admitted with another plan")
            else:
                self._conn.execute(
                    """INSERT INTO receipts
                       (attempt_id,generation,plan_hash,digest,resource_id,payload,deadline_at,
                        created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        attempt_id,
                        payload["generation"],
                        payload["plan_hash"],
                        digest,
                        resource,
                        json.dumps(payload, sort_keys=True),
                        deadline,
                        now,
                        now,
                    ),
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        receipt = self.get(attempt_id)
        assert receipt is not None
        return receipt

    def update(self, attempt_id: str, **changes: Any) -> Receipt:
        allowed = {
            "state",
            "admitted",
            "desired_cancel",
            "sequence",
            "log_hash",
            "result",
            "acknowledged",
            "cleanup_state",
        }
        if not changes or not set(changes) <= allowed:
            raise ValueError("invalid receipt transition")
        if "result" in changes and changes["result"] is not None:
            changes["result"] = json.dumps(changes["result"], sort_keys=True)
        changes["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in changes)
        cur = self._conn.execute(
            f"UPDATE receipts SET {assignments} WHERE attempt_id=?",
            (*changes.values(), identifier(attempt_id)),
        )
        if cur.rowcount != 1:
            raise LookupError("execution receipt is missing")
        receipt = self.get(attempt_id)
        assert receipt is not None
        return receipt

    def attempt_dir(self, receipt: Receipt) -> Path:
        return self.jobs_dir / identifier(receipt.attempt_id)

    def finish_cleanup(self, attempt_id: str) -> None:
        receipt = self.get(attempt_id)
        if receipt is None or not receipt.acknowledged:
            raise ValueError("unacknowledged work cannot be discarded")
        minimal = {key: receipt.payload[key] for key in ("attempt_id", "job_id", "space_id")}
        self._conn.execute(
            """UPDATE receipts SET cleanup_state='released', result=NULL, payload=?, updated_at=?
               WHERE attempt_id=? AND acknowledged=1""",
            (json.dumps(minimal), time.time(), attempt_id),
        )
