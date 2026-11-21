"""Node credentials are stored only as hashes, and what each node may use is granted explicitly.

Whoever calls this (an HTTP route or the CLI) must first check that the person is an administrator
before creating an invitation. The token that results can reach only that registered node and the
spaces granted to it; it never works as an owner login.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from personal_agent.clock import Clock
from personal_agent.db import Database

DEFAULT_CAPABILITIES = (
    "isolated_exec",
    "persistent_receipts",
    "network_none",
    "cancel_confirmation",
    "log_resume",
)
MAX_SPACES = 32


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _strings(values: Sequence[str], *, limit: int = 32) -> list[str]:
    if not values or len(values) > limit:
        raise ValueError("at least one grant and no more than the limit are required")
    if any(not isinstance(x, str) or not x or len(x) > 128 for x in values):
        raise ValueError("invalid node grant")
    result = sorted(set(values))
    return result


@dataclass(frozen=True)
class Node:
    id: str
    owner_id: str
    label: str
    space_ids: tuple[str, ...]
    capabilities: tuple[str, ...]
    declared_capabilities: tuple[str, ...]
    credential_version: int
    created_at: float
    last_seen_at: float | None
    revoked_at: float | None

    @property
    def effective_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.capabilities) & set(self.declared_capabilities)))

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["effective_capabilities"] = list(self.effective_capabilities)
        return result


@dataclass(frozen=True)
class PairingInvite:
    code: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True)
class Registration:
    node: Node
    token: str = field(repr=False)


def _node(row: Any) -> Node:
    return Node(
        id=row["id"],
        owner_id=row["owner_id"],
        label=row["label"],
        space_ids=tuple(json.loads(row["space_ids"])),
        capabilities=tuple(json.loads(row["capabilities"])),
        declared_capabilities=tuple(json.loads(row["declared_capabilities"])),
        credential_version=row["credential_version"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        revoked_at=row["revoked_at"],
    )


class NodeRegistry:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def create_invite(
        self,
        owner_id: str,
        space_ids: list[str] | tuple[str, ...],
        label: str = "",
        capabilities: list[str] | tuple[str, ...] = DEFAULT_CAPABILITIES,
        *,
        ttl_s: float = 600,
    ) -> PairingInvite:
        if not owner_id or len(owner_id) > 128 or not 30 <= ttl_s <= 3600:
            raise ValueError("invalid node invitation")
        spaces = _strings(space_ids, limit=MAX_SPACES)
        caps = _strings(capabilities)
        if not set(caps) <= set(DEFAULT_CAPABILITIES):
            raise ValueError("unsupported node capability grant")
        now = self.clock.now()
        code = secrets.token_urlsafe(32)
        with self.db.tx():
            pending = self.db.scalar(
                "SELECT COUNT(*) FROM node_pairings WHERE used_at IS NULL AND expires_at > ?",
                (now,),
                default=0,
            )
            if pending >= 32:
                raise ValueError("too many pending node invitations")
            self.db.execute(
                """INSERT INTO node_pairings
                   (token_hash, owner_id, label, space_ids, capabilities, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    token_hash(code),
                    owner_id,
                    label[:120],
                    json.dumps(spaces),
                    json.dumps(caps),
                    now,
                    now + ttl_s,
                ),
            )
        return PairingInvite(code, now + ttl_s)

    def redeem(self, code: str, capabilities: list[str] | tuple[str, ...]) -> Registration | None:
        if not code or len(code) > 256:
            return None
        declared = _strings(capabilities)
        now = self.clock.now()
        with self.db.tx():
            row = self.db.one(
                "SELECT * FROM node_pairings WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
                (token_hash(code), now),
            )
            if row is None:
                return None
            node_id = "node_" + secrets.token_hex(16)
            token = secrets.token_urlsafe(48)
            self.db.execute(
                "UPDATE node_pairings SET used_at=? WHERE token_hash=?",
                (now, token_hash(code)),
            )
            self.db.execute(
                """INSERT INTO nodes
                   (id, owner_id, label, token_hash, space_ids, capabilities, declared_capabilities,
                    created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    node_id,
                    row["owner_id"],
                    row["label"],
                    token_hash(token),
                    row["space_ids"],
                    row["capabilities"],
                    json.dumps(declared),
                    now,
                    now,
                ),
            )
            node = self.get(node_id)
            assert node is not None
            return Registration(node, token)

    def get(self, node_id: str) -> Node | None:
        row = self.db.one("SELECT * FROM nodes WHERE id=?", (node_id,))
        return None if row is None else _node(row)

    def list(self, owner_id: str | None = None) -> list[Node]:
        rows = (
            self.db.query("SELECT * FROM nodes ORDER BY created_at")
            if owner_id is None
            else self.db.query(
                "SELECT * FROM nodes WHERE owner_id=? ORDER BY created_at", (owner_id,)
            )
        )
        return [_node(row) for row in rows]

    def authenticate(self, token: str) -> Node | None:
        if len(token) < 32 or len(token) > 256:
            return None
        row = self.db.one(
            "SELECT * FROM nodes WHERE token_hash=? AND revoked_at IS NULL", (token_hash(token),)
        )
        return None if row is None else _node(row)

    def authorize(self, node_id: str, space_id: str) -> bool:
        node = self.get(node_id)
        return bool(node and node.revoked_at is None and space_id in node.space_ids)

    def heartbeat(self, node_id: str, capabilities: Sequence[str] | None = None) -> Node | None:
        with self.db.tx():
            node = self.get(node_id)
            if node is None or node.revoked_at is not None:
                return None
            declared = (
                list(node.declared_capabilities) if capabilities is None else _strings(capabilities)
            )
            self.db.execute(
                "UPDATE nodes SET last_seen_at=?, declared_capabilities=? WHERE id=?",
                (self.clock.now(), json.dumps(declared), node_id),
            )
            return self.get(node_id)

    def revoke(self, node_id: str, owner_id: str) -> bool:
        with self.db.tx():
            cur = self.db.execute(
                """UPDATE nodes SET revoked_at=?, credential_version=credential_version+1
                   WHERE id=? AND owner_id=? AND revoked_at IS NULL""",
                (self.clock.now(), node_id, owner_id),
            )
            return cur.rowcount == 1

    def set_grants(
        self,
        node_id: str,
        owner_id: str,
        space_ids: Sequence[str],
        capabilities: Sequence[str],
    ) -> Node:
        spaces, caps = _strings(space_ids), _strings(capabilities)
        if not set(caps) <= set(DEFAULT_CAPABILITIES):
            raise ValueError("unsupported node capability grant")
        with self.db.tx():
            cur = self.db.execute(
                """UPDATE nodes SET space_ids=?, capabilities=?, credential_version=credential_version+1
                   WHERE id=? AND owner_id=? AND revoked_at IS NULL""",
                (json.dumps(spaces), json.dumps(caps), node_id, owner_id),
            )
            if cur.rowcount != 1:
                raise PermissionError("node is not owned by this principal")
            node = self.get(node_id)
            assert node is not None
            return node
