"""Durable claims around paid inbound preprocessing, before an inbox event exists.

An interrupted external call has an unknown outcome. A repeated delivery must not silently
pay for it again: the owner can send a new message to explicitly start another attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_agent.clock import Clock
from personal_agent.db import Database, dumps, loads


@dataclass(frozen=True)
class Receipt:
    state: str
    result: dict[str, Any]
    fresh: bool = False


class ReceiptStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    def recover(self) -> int:
        """Called only by the new engine owner, when the previous process is known to be gone."""
        with self.db.tx():
            return self.db.execute(
                "UPDATE ingress_receipts SET state='uncertain', updated_at=? WHERE state='processing'",
                (self.clock.now(),),
            ).rowcount

    def claim(self, source: str, external_id: str, fingerprint: str, timeout_s: float) -> Receipt:
        now = self.clock.now()
        with self.db.tx():
            existing = self.db.one(
                "SELECT id FROM events WHERE source=? AND external_id=?", (source, external_id)
            )
            if existing is not None:
                return Receipt("done", {"event_id": existing["id"]})
            row = self.db.one(
                "SELECT * FROM ingress_receipts WHERE source=? AND external_id=?",
                (source, external_id),
            )
            if row is None:
                self.db.execute(
                    """INSERT INTO ingress_receipts(source,external_id,fingerprint,state,result,
                       created_at,updated_at,lease_until) VALUES(?,?,?,'processing','{}',?,?,?)""",
                    (source, external_id, fingerprint, now, now, now + timeout_s + 30),
                )
                return Receipt("processing", {}, fresh=True)
            if row["fingerprint"] != fingerprint:
                return Receipt("conflict", {})
            state = row["state"]
            if state == "processing" and row["lease_until"] <= now:
                self.save(source, external_id, "uncertain", {})
                state = "uncertain"
            return Receipt(state, loads(row["result"], {}))

    def save(self, source: str, external_id: str, state: str, result: dict[str, Any]) -> None:
        with self.db.tx():
            self.db.execute(
                """UPDATE ingress_receipts SET state=?, result=?, updated_at=?
                   WHERE source=? AND external_id=?""",
                (state, dumps(result), self.clock.now(), source, external_id),
            )
