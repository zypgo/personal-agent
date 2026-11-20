"""Global identity directory. Authorization is checked on every operation, never by nickname.

The directory contains membership and resource references, not a cross-space history index.
Personal and project histories live in separate App databases.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from typing import cast

from personal_agent.channels.pairing import clean_label
from personal_agent.clock import Clock, SystemClock
from personal_agent.collaboration.models import (
    OWNER_ID,
    AccessDenied,
    ChannelBinding,
    Enrollment,
    IdentityError,
    InvalidInvite,
    Invitation,
    Membership,
    Principal,
    ResourceGrant,
    Role,
    Space,
    SpaceKind,
)
from personal_agent.db import Database

MAX_PENDING_INVITES = 10
MAX_PRINCIPALS = 100
MAX_SPACES = 100
MAX_TOKEN_CHARS = 200
INVITE_SECONDS = 600.0
RECOVERY_SECONDS = 365 * 86400.0
ROLES = frozenset(("owner", "member", "viewer"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def _role(value: str) -> Role:
    if value not in ROLES:
        raise IdentityError("Unknown project role.")
    return cast(Role, value)


def _principal(row: sqlite3.Row) -> Principal:
    return Principal(
        row["id"],
        row["display_name"],
        bool(row["is_admin"]),
        int(row["authz_version"]),
        float(row["created_at"]),
    )


def _space(row: sqlite3.Row) -> Space:
    return Space(
        row["id"], cast(SpaceKind, row["kind"]), row["name"], row["owner_id"], row["created_at"]
    )


def _membership(row: sqlite3.Row) -> Membership:
    return Membership(
        row["space_id"],
        row["principal_id"],
        cast(Role, row["role"]),
        row["version"],
        row["joined_at"],
    )


class IdentityStore:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()

    def principal(self, principal_id: str) -> Principal:
        row = self.db.one(
            "SELECT * FROM principals WHERE id = ? AND disabled_at IS NULL", (principal_id,)
        )
        if row is None:
            raise AccessDenied("Identity is unavailable.")
        return _principal(row)

    def require_admin(self, actor_id: str) -> Principal:
        principal = self.principal(actor_id)
        if not principal.is_admin:
            raise AccessDenied("Instance administrator permission is required.")
        return principal

    def space(self, space_id: str) -> Space:
        row = self.db.one("SELECT * FROM spaces WHERE id = ? AND archived_at IS NULL", (space_id,))
        if row is None:
            raise AccessDenied("Space is unavailable.")
        return _space(row)

    def require_access(
        self,
        principal_id: str,
        space_id: str,
        *,
        write: bool = False,
        manage: bool = False,
        version: int | None = None,
    ) -> Membership:
        self.principal(principal_id)
        space = self.space(space_id)
        if space.kind == "personal" and space.owner_id != principal_id:
            raise AccessDenied("Space is unavailable.")
        row = self.db.one(
            "SELECT * FROM memberships WHERE principal_id = ? AND space_id = ? AND revoked_at IS NULL",
            (principal_id, space_id),
        )
        if row is None:
            raise AccessDenied("Space is unavailable.")
        membership = _membership(row)
        if (write and not membership.can_write) or (manage and not membership.can_manage):
            raise AccessDenied("The requested action is not permitted in this space.")
        if version is not None and membership.version != version:
            raise AccessDenied("Space authorization changed; request a new action.")
        return membership

    def authorized(
        self,
        principal_id: str,
        space_id: str,
        *,
        write: bool = False,
        version: int | None = None,
        principal_version: int | None = None,
    ) -> bool:
        try:
            principal = self.principal(principal_id)
            if principal_version is not None and principal.authz_version != principal_version:
                return False
            self.require_access(principal_id, space_id, write=write, version=version)
        except AccessDenied:
            return False
        return True

    def spaces_for(self, principal_id: str) -> list[Space]:
        self.principal(principal_id)
        rows = self.db.query(
            "SELECT s.* FROM spaces s JOIN memberships m ON m.space_id = s.id "
            "WHERE m.principal_id = ? AND m.revoked_at IS NULL AND s.archived_at IS NULL "
            "AND (s.kind = 'project' OR s.owner_id = ?) ORDER BY s.kind, s.created_at, s.id",
            (principal_id, principal_id),
        )
        return [_space(row) for row in rows]

    def personal_space(self, principal_id: str) -> Space:
        self.principal(principal_id)
        return _space(
            self.db.row(
                "SELECT * FROM spaces WHERE kind = 'personal' AND owner_id = ?", (principal_id,)
            )
        )

    def members(self, actor_id: str, space_id: str) -> list[dict[str, str | int]]:
        self.require_access(actor_id, space_id)
        rows = self.db.query(
            "SELECT m.*, p.display_name FROM memberships m JOIN principals p ON p.id = m.principal_id "
            "WHERE m.space_id = ? AND m.revoked_at IS NULL AND p.disabled_at IS NULL ORDER BY m.joined_at, m.principal_id",
            (space_id,),
        )
        return [
            dict(
                principal_id=r["principal_id"],
                display_name=r["display_name"],
                role=r["role"],
                version=r["version"],
            )
            for r in rows
        ]

    def _audit(
        self, actor: str, action: str, *, space_id: str | None = None, subject: str | None = None
    ) -> None:
        self.db.execute(
            "INSERT INTO collaboration_audit(actor_id, action, space_id, subject_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (actor, action, space_id, subject, self.clock.now()),
        )

    def create_project(self, actor_id: str, name: str) -> Space:
        label = clean_label(name)
        if not label:
            raise IdentityError("A project name is required.")
        with self.db.tx():
            self.principal(actor_id)
            if self.db.scalar("SELECT COUNT(*) FROM spaces", default=0) >= MAX_SPACES:
                raise IdentityError("The instance space limit has been reached.")
            space_id = _id("s_")
            now = self.clock.now()
            self.db.execute(
                "INSERT INTO spaces(id, kind, name, owner_id, created_at) VALUES (?, 'project', ?, ?, ?)",
                (space_id, label, actor_id, now),
            )
            self.db.execute(
                "INSERT INTO memberships(space_id, principal_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
                (space_id, actor_id, now),
            )
            self._audit(actor_id, "project_created", space_id=space_id)
            return self.space(space_id)

    def _create_principal(self, label: str) -> Principal:
        """Only called from a validated member invitation, inside the same transaction."""
        if self.db.scalar("SELECT COUNT(*) FROM principals", default=0) >= MAX_PRINCIPALS:
            raise IdentityError("The instance member limit has been reached.")
        if self.db.scalar("SELECT COUNT(*) FROM spaces", default=0) >= MAX_SPACES:
            raise IdentityError("The instance space limit has been reached.")
        principal_id, space_id, now = _id("u_"), _id("s_"), self.clock.now()
        self.db.execute(
            "INSERT INTO principals(id, display_name, created_at) VALUES (?, ?, ?)",
            (principal_id, clean_label(label) or "Member", now),
        )
        self.db.execute(
            "INSERT INTO spaces(id, kind, name, owner_id, created_at) VALUES (?, 'personal', 'Personal', ?, ?)",
            (space_id, principal_id, now),
        )
        self.db.execute(
            "INSERT INTO memberships(space_id, principal_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
            (space_id, principal_id, now),
        )
        return self.principal(principal_id)

    def _new_invite(
        self,
        actor_id: str,
        kind: str,
        *,
        target: str | None = None,
        space_id: str | None = None,
        role: Role | None = None,
        label: str = "",
        membership_version: int | None = None,
        lifetime_s: float = INVITE_SECONDS,
    ) -> Invitation:
        principal = self.principal(actor_id)
        now = self.clock.now()
        pending = self.db.scalar(
            "SELECT COUNT(*) FROM collaboration_invites WHERE issued_by = ? AND kind != 'recovery' "
            "AND used_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
            (actor_id, now),
            default=0,
        )
        if kind != "recovery" and pending >= MAX_PENDING_INVITES:
            raise IdentityError(
                "Too many unused invitations; use or revoke an existing invitation."
            )
        code = secrets.token_urlsafe(32)
        expires = now + lifetime_s
        self.db.execute(
            "INSERT INTO collaboration_invites(token_hash, kind, issued_by, target_principal_id, space_id, role, label, "
            "issuer_version, membership_version, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash(code),
                kind,
                actor_id,
                target,
                space_id,
                role,
                clean_label(label),
                principal.authz_version,
                membership_version,
                now,
                expires,
            ),
        )
        self._audit(actor_id, f"{kind}_invite_created", space_id=space_id, subject=target)
        return Invitation(code, expires, kind, space_id, role)

    def create_member_invite(
        self, actor_id: str, space_id: str, role: str = "member", label: str = ""
    ) -> Invitation:
        selected_role = _role(role)
        with self.db.tx():
            membership = self.require_access(actor_id, space_id, manage=True)
            if self.space(space_id).kind != "project":
                raise IdentityError("Personal spaces cannot invite members; create a project.")
            return self._new_invite(
                actor_id,
                "member",
                space_id=space_id,
                role=selected_role,
                label=label,
                membership_version=membership.version,
            )

    def create_device_invite(self, actor_id: str, label: str = "") -> Invitation:
        with self.db.tx():
            return self._new_invite(actor_id, "device", target=actor_id, label=label)

    def create_admin_device_invite(
        self, actor_id: str, principal_id: str, label: str = ""
    ) -> Invitation:
        """Trusted local recovery adds a device to an existing identity, never a membership.

        Keep the device code bound to the target's current authorization version using the
        regular device flow, and separately record who performed the administrative action.
        """
        with self.db.tx():
            self.require_admin(actor_id)
            self.principal(principal_id)
            invitation = self.create_device_invite(principal_id, label)
            self._audit(actor_id, "admin_device_invite_created", subject=principal_id)
            return invitation

    def _live_invite(self, code: str, kind: str) -> sqlite3.Row:
        code = code.strip()
        if not code or len(code) > MAX_TOKEN_CHARS:
            raise InvalidInvite("Invitation is unavailable.")
        row = self.db.one(
            "SELECT * FROM collaboration_invites WHERE token_hash = ? AND kind = ? AND used_at IS NULL "
            "AND revoked_at IS NULL AND expires_at > ?",
            (_hash(code), kind, self.clock.now()),
        )
        if row is None:
            raise InvalidInvite("Invitation is unavailable.")
        try:
            issuer = self.principal(row["issued_by"])
            if issuer.authz_version != row["issuer_version"]:
                raise AccessDenied("Invitation issuer changed.")
            if kind == "member":
                self.require_access(
                    row["issued_by"],
                    row["space_id"],
                    manage=True,
                    version=row["membership_version"],
                )
            elif kind == "channel":
                self.require_access(
                    row["issued_by"], row["space_id"], write=True, version=row["membership_version"]
                )
        except AccessDenied as exc:
            raise InvalidInvite("Invitation is unavailable.") from exc
        return row

    def _consume(self, row: sqlite3.Row) -> None:
        changed = self.db.execute(
            "UPDATE collaboration_invites SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND revoked_at IS NULL",
            (self.clock.now(), row["token_hash"]),
        )
        if changed.rowcount != 1:
            raise InvalidInvite("Invitation is unavailable.")

    def redeem_member(
        self, code: str, *, existing_principal_id: str | None = None, label: str = ""
    ) -> Enrollment:
        with self.db.tx():
            row = self._live_invite(code, "member")
            space = self.space(row["space_id"])
            if space.kind != "project":
                raise InvalidInvite("Invitation is unavailable.")
            created = existing_principal_id is None
            principal = (
                self._create_principal(label or row["label"])
                if created
                else self.principal(str(existing_principal_id))
            )
            existing = self.db.one(
                "SELECT * FROM memberships WHERE space_id = ? AND principal_id = ?",
                (space.id, principal.id),
            )
            # A generic invitation never silently upgrades or demotes an already active member.
            # Owners use set_role for explicit, audited privilege changes.
            if existing is None:
                self.db.execute(
                    "INSERT INTO memberships(space_id, principal_id, role, joined_at) VALUES (?, ?, ?, ?)",
                    (space.id, principal.id, row["role"], self.clock.now()),
                )
            elif existing["revoked_at"] is not None:
                self.db.execute(
                    "UPDATE memberships SET role = ?, version = version + 1, revoked_at = NULL, joined_at = ? WHERE space_id = ? AND principal_id = ?",
                    (row["role"], self.clock.now(), space.id, principal.id),
                )
            self._consume(row)
            recovery = self.issue_recovery(principal.id) if created else None
            self._audit(principal.id, "member_joined", space_id=space.id)
            return Enrollment(principal, space, created, recovery)

    def redeem_device(self, code: str) -> str:
        with self.db.tx():
            row = self._live_invite(code, "device")
            principal = self.principal(row["target_principal_id"])
            if principal.id != row["issued_by"]:
                raise InvalidInvite("Invitation is unavailable.")
            self._consume(row)
            return principal.id

    def issue_recovery(self, actor_id: str) -> str:
        with self.db.tx():
            self.principal(actor_id)
            self.db.execute(
                "UPDATE collaboration_invites SET revoked_at = ? WHERE kind = 'recovery' AND target_principal_id = ? AND used_at IS NULL AND revoked_at IS NULL",
                (self.clock.now(), actor_id),
            )
            return self._new_invite(
                actor_id, "recovery", target=actor_id, lifetime_s=RECOVERY_SECONDS
            ).code

    def recover(self, code: str) -> str:
        with self.db.tx():
            row = self._live_invite(code, "recovery")
            principal = self.principal(row["target_principal_id"])
            if principal.id != row["issued_by"]:
                raise InvalidInvite("Invitation is unavailable.")
            self._consume(row)
            # A recovery code is for the day every device is lost or suspect. Whatever was signed
            # in, and whatever a signed-in device had prepared (a spare pairing, channel, member
            # or node code), ends here, in the same transaction that spends the code. The caller
            # then issues the one replacement session. Devices, nodes and channel bindings that
            # were already completed stay: the owner reviews those in the settings.
            now = self.clock.now()
            self.db.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",
                (now, principal.id),
            )
            self.db.execute(
                "UPDATE web_invites SET used_at = ? WHERE used_at IS NULL "
                "AND (principal_id = ? OR issued_by = ?)",
                (now, principal.id, principal.id),
            )
            self.db.execute(
                "UPDATE collaboration_invites SET revoked_at = ? WHERE issued_by = ? AND kind != 'recovery' AND used_at IS NULL AND revoked_at IS NULL",
                (now, principal.id),
            )
            self.db.execute(
                "UPDATE node_pairings SET used_at = ? WHERE owner_id = ? AND used_at IS NULL",
                (now, principal.id),
            )
            self._audit(principal.id, "identity_recovered")
            return principal.id

    def revoke_invites(self, actor_id: str, *, space_id: str | None = None) -> int:
        with self.db.tx():
            self.principal(actor_id)
            if space_id is not None:
                self.require_access(actor_id, space_id, manage=True)
                cur = self.db.execute(
                    "UPDATE collaboration_invites SET revoked_at = ? WHERE space_id = ? AND used_at IS NULL AND revoked_at IS NULL",
                    (self.clock.now(), space_id),
                )
            else:
                cur = self.db.execute(
                    "UPDATE collaboration_invites SET revoked_at = ? WHERE issued_by = ? AND kind != 'recovery' AND used_at IS NULL AND revoked_at IS NULL",
                    (self.clock.now(), actor_id),
                )
            return cur.rowcount

    def _require_project(self, actor_id: str, space_id: str) -> Space:
        self.require_access(actor_id, space_id, manage=True)
        space = self.space(space_id)
        if space.kind != "project":
            raise IdentityError("Personal space membership cannot be changed.")
        return space

    def _keep_owner(self, space_id: str, target_id: str, next_role: str | None) -> None:
        current = self.db.one(
            "SELECT role FROM memberships WHERE space_id = ? AND principal_id = ? AND revoked_at IS NULL",
            (space_id, target_id),
        )
        if current is not None and current["role"] == "owner" and next_role != "owner":
            count = self.db.scalar(
                "SELECT COUNT(*) FROM memberships m JOIN principals p ON p.id = m.principal_id WHERE m.space_id = ? AND m.role = 'owner' AND m.revoked_at IS NULL AND p.disabled_at IS NULL",
                (space_id,),
                default=0,
            )
            if count <= 1:
                raise IdentityError("Keep at least one active project owner.")

    def set_role(self, actor_id: str, space_id: str, target_id: str, role: str) -> Membership:
        selected = _role(role)
        with self.db.tx():
            self._require_project(actor_id, space_id)
            self.require_access(target_id, space_id)
            self._keep_owner(space_id, target_id, selected)
            self.db.execute(
                "UPDATE memberships SET role = ?, version = version + 1 WHERE space_id = ? AND principal_id = ?",
                (selected, space_id, target_id),
            )
            self._refresh_project_owner(space_id)
            self._audit(actor_id, "member_role_changed", space_id=space_id, subject=target_id)
            return self.require_access(target_id, space_id)

    def revoke_member(self, actor_id: str, space_id: str, target_id: str) -> bool:
        with self.db.tx():
            self._require_project(actor_id, space_id)
            self._keep_owner(space_id, target_id, None)
            cur = self.db.execute(
                "UPDATE memberships SET revoked_at = ?, version = version + 1 WHERE space_id = ? AND principal_id = ? AND revoked_at IS NULL",
                (self.clock.now(), space_id, target_id),
            )
            if cur.rowcount:
                self._refresh_project_owner(space_id)
                self.db.execute(
                    "UPDATE resource_grants SET revoked_at = ?, version = version + 1 WHERE space_id = ? AND principal_id = ? AND revoked_at IS NULL",
                    (self.clock.now(), space_id, target_id),
                )
                self._audit(actor_id, "member_removed", space_id=space_id, subject=target_id)
            return cur.rowcount == 1

    def _refresh_project_owner(self, space_id: str) -> None:
        """Keep the default background actor an active owner after an ownership transfer."""
        current = self.space(space_id)
        if (
            self.db.one(
                "SELECT 1 FROM memberships m JOIN principals p ON p.id=m.principal_id "
                "WHERE m.space_id=? AND m.principal_id=? AND m.role='owner' "
                "AND m.revoked_at IS NULL AND p.disabled_at IS NULL",
                (space_id, current.owner_id),
            )
            is not None
        ):
            return
        replacement = self.db.one(
            "SELECT m.principal_id FROM memberships m JOIN principals p ON p.id=m.principal_id "
            "WHERE m.space_id=? AND m.role='owner' AND m.revoked_at IS NULL AND p.disabled_at IS NULL "
            "ORDER BY m.joined_at, m.principal_id LIMIT 1",
            (space_id,),
        )
        if replacement is None:
            raise IdentityError("Keep at least one active project owner.")
        self.db.execute(
            "UPDATE spaces SET owner_id=? WHERE id=?", (replacement["principal_id"], space_id)
        )

    def disable_principal(self, actor_id: str, target_id: str) -> bool:
        with self.db.tx():
            self.require_admin(actor_id)
            if target_id == OWNER_ID:
                raise IdentityError("The original instance administrator cannot be disabled.")
            target = self.principal(target_id)
            if target.is_admin:
                raise IdentityError("Administrator accounts require explicit recovery management.")
            owned_projects = self.db.query(
                "SELECT m.space_id FROM memberships m JOIN spaces s ON s.id=m.space_id "
                "WHERE m.principal_id=? AND m.role='owner' AND m.revoked_at IS NULL "
                "AND s.kind='project' AND s.archived_at IS NULL",
                (target_id,),
            )
            for project in owned_projects:
                self._keep_owner(project["space_id"], target_id, None)
            cur = self.db.execute(
                "UPDATE principals SET disabled_at = ?, authz_version = authz_version + 1 WHERE id = ? AND disabled_at IS NULL",
                (self.clock.now(), target_id),
            )
            for project in owned_projects:
                self._refresh_project_owner(project["space_id"])
            self.db.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",
                (self.clock.now(), target_id),
            )
            self._audit(actor_id, "principal_disabled", subject=target_id)
            return cur.rowcount == 1

    def grant_resource(
        self,
        actor_id: str,
        space_id: str,
        resource_type: str,
        resource_id: str,
        actions: list[str],
        *,
        principal_id: str = "*",
        expires_at: float | None = None,
    ) -> ResourceGrant:
        if (
            not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", resource_type)
            or not resource_id
            or len(resource_id) > 200
        ):
            raise IdentityError("Invalid resource reference.")
        if (
            not actions
            or len(actions) > 32
            or any(not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", action) for action in actions)
        ):
            raise IdentityError("Invalid resource actions.")
        if expires_at is not None and expires_at <= self.clock.now():
            raise IdentityError("Resource grant must expire in the future.")
        with self.db.tx():
            self.require_admin(actor_id)
            self.space(space_id)
            if principal_id != "*":
                self.require_access(principal_id, space_id, write=True)
            grant_id = _id("g_")
            self.db.execute(
                "INSERT INTO resource_grants(id, space_id, resource_type, resource_id, principal_id, actions, issued_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(space_id, resource_type, resource_id, principal_id) "
                "DO UPDATE SET actions = excluded.actions, version = resource_grants.version + 1, issued_by = excluded.issued_by, created_at = excluded.created_at, expires_at = excluded.expires_at, revoked_at = NULL",
                (
                    grant_id,
                    space_id,
                    resource_type,
                    resource_id,
                    principal_id,
                    json.dumps(sorted(set(actions))),
                    actor_id,
                    self.clock.now(),
                    expires_at,
                ),
            )
            row = self.db.row(
                "SELECT * FROM resource_grants WHERE space_id = ? AND resource_type = ? AND resource_id = ? AND principal_id = ?",
                (space_id, resource_type, resource_id, principal_id),
            )
            self._audit(actor_id, "resource_granted", space_id=space_id, subject=row["id"])
            return self._grant(row)

    @staticmethod
    def _grant(row: sqlite3.Row) -> ResourceGrant:
        actions = json.loads(row["actions"])
        return ResourceGrant(
            row["id"],
            row["space_id"],
            row["resource_type"],
            row["resource_id"],
            row["principal_id"],
            tuple(actions),
            row["version"],
            row["expires_at"],
        )

    def require_resource(
        self,
        principal_id: str,
        space_id: str,
        resource_type: str,
        resource_id: str,
        action: str,
        *,
        version: int | None = None,
    ) -> ResourceGrant:
        self.require_access(principal_id, space_id, write=True)
        rows = self.db.query(
            "SELECT * FROM resource_grants WHERE space_id = ? AND resource_type = ? AND resource_id = ? AND principal_id IN (?, '*') "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) ORDER BY principal_id DESC",
            (space_id, resource_type, resource_id, principal_id, self.clock.now()),
        )
        for row in rows:
            grant = self._grant(row)
            if action in grant.actions and (version is None or grant.version == version):
                return grant
        raise AccessDenied("This resource is not authorized for the requested action.")

    def revoke_resource(self, actor_id: str, grant_id: str) -> bool:
        with self.db.tx():
            self.require_admin(actor_id)
            cur = self.db.execute(
                "UPDATE resource_grants SET revoked_at = ?, version = version + 1 WHERE id = ? AND revoked_at IS NULL",
                (self.clock.now(), grant_id),
            )
            if cur.rowcount:
                self._audit(actor_id, "resource_revoked", subject=grant_id)
            return cur.rowcount == 1

    def create_channel_binding(self, principal_id: str, space_id: str) -> Invitation:
        """The signed-in person displays this code, then sends it from their external account."""
        with self.db.tx():
            membership = self.require_access(principal_id, space_id, write=True)
            return self._new_invite(
                principal_id,
                "channel",
                target=principal_id,
                space_id=space_id,
                membership_version=membership.version,
            )

    def bind_channel(
        self, code: str, channel: str, account_id: str, sender_id: str, recipient_id: str
    ) -> ChannelBinding:
        """Consume a code only on an authenticated provider event with stable sender IDs.

        The provider listener supplies channel/account/sender/recipient; none are taken from
        the message body. No nickname matching, and no implicit invitation to another space.
        """
        if not all(
            value and len(value) <= 200 and not any(ord(c) < 32 for c in value)
            for value in (channel, account_id, sender_id, recipient_id)
        ):
            raise IdentityError("Invalid channel identity.")
        with self.db.tx():
            invite = self._live_invite(code, "channel")
            principal = self.principal(invite["target_principal_id"])
            if principal.id != invite["issued_by"]:
                raise InvalidInvite("Invitation is unavailable.")
            membership = self.require_access(
                principal.id, invite["space_id"], write=True, version=invite["membership_version"]
            )
            self.db.execute(
                "INSERT INTO channel_identities(channel, account_id, sender_id, principal_id, space_id, recipient_id, membership_version, principal_version, bound_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(channel, account_id, sender_id) DO UPDATE SET "
                "principal_id = excluded.principal_id, space_id = excluded.space_id, recipient_id = excluded.recipient_id, "
                "membership_version = excluded.membership_version, principal_version = excluded.principal_version, "
                "version = channel_identities.version + 1, bound_at = excluded.bound_at, revoked_at = NULL",
                (
                    channel,
                    account_id,
                    sender_id,
                    principal.id,
                    membership.space_id,
                    recipient_id,
                    membership.version,
                    principal.authz_version,
                    self.clock.now(),
                ),
            )
            self._consume(invite)
            self._audit(principal.id, "channel_bound", space_id=membership.space_id)
            return self._binding(
                self.db.row(
                    "SELECT * FROM channel_identities WHERE channel = ? AND account_id = ? AND sender_id = ?",
                    (channel, account_id, sender_id),
                )
            )

    @staticmethod
    def _binding(row: sqlite3.Row) -> ChannelBinding:
        return ChannelBinding(
            row["channel"],
            row["account_id"],
            row["sender_id"],
            row["principal_id"],
            row["space_id"],
            row["recipient_id"],
            row["membership_version"],
            row["principal_version"],
            row["version"],
        )

    def lookup_channel(
        self, channel: str, account_id: str, sender_id: str
    ) -> ChannelBinding | None:
        row = self.db.one(
            "SELECT * FROM channel_identities WHERE channel = ? AND account_id = ? AND sender_id = ? AND revoked_at IS NULL",
            (channel, account_id, sender_id),
        )
        if row is None:
            return None
        binding = self._binding(row)
        if not self.authorized(
            binding.principal_id,
            binding.space_id,
            write=True,
            version=binding.membership_version,
            principal_version=binding.principal_version,
        ):
            return None
        return binding

    def channel_bindings(
        self, principal_id: str, space_id: str
    ) -> list[dict[str, str | int | float | bool]]:
        """Own bindings only; stale generations remain visible so they can be removed."""
        self.require_access(principal_id, space_id)
        rows = self.db.query(
            "SELECT * FROM channel_identities WHERE principal_id = ? AND space_id = ? "
            "AND revoked_at IS NULL ORDER BY bound_at DESC, channel, account_id, sender_id",
            (principal_id, space_id),
        )
        return [
            {
                "channel": row["channel"],
                "account_id": row["account_id"],
                "sender_id": row["sender_id"],
                "recipient_id": row["recipient_id"],
                "bound_at": row["bound_at"],
                "version": row["version"],
                "active": self.lookup_channel(row["channel"], row["account_id"], row["sender_id"])
                is not None,
            }
            for row in rows
        ]

    def revoke_channel(
        self,
        actor_id: str,
        channel: str,
        account_id: str,
        sender_id: str,
        *,
        space_id: str | None = None,
    ) -> bool:
        with self.db.tx():
            actor = self.principal(actor_id)
            if space_id is not None:
                self.require_access(actor_id, space_id)
            row = self.db.one(
                "SELECT * FROM channel_identities WHERE channel = ? AND account_id = ? AND sender_id = ?",
                (channel, account_id, sender_id),
            )
            if row is None:
                return False
            if space_id is not None and row["space_id"] != space_id:
                raise AccessDenied("Channel binding is unavailable.")
            if not actor.is_admin and row["principal_id"] != actor_id:
                raise AccessDenied("Channel binding is unavailable.")
            cur = self.db.execute(
                "UPDATE channel_identities SET revoked_at = ?, version = version + 1 WHERE channel = ? AND account_id = ? AND sender_id = ? AND revoked_at IS NULL",
                (self.clock.now(), channel, account_id, sender_id),
            )
            if cur.rowcount:
                self._audit(actor_id, "channel_revoked", space_id=row["space_id"])
            return cur.rowcount == 1

    def legacy_owner_sender(self, channel: str, sender_id: str) -> bool:
        """Only identities that predated the migration inherit the old owner meaning."""
        return (
            self.db.one(
                "SELECT 1 FROM legacy_channel_owners WHERE channel = ? AND sender = ?",
                (channel, sender_id),
            )
            is not None
        )

    def has_channel_binding(self, channel: str, account_id: str, sender_id: str) -> bool:
        """A revoked/replaced explicit identity must never fall back to its legacy owner role."""
        return (
            self.db.one(
                "SELECT 1 FROM channel_identities WHERE channel = ? AND account_id = ? AND sender_id = ?",
                (channel, account_id, sender_id),
            )
            is not None
        )

    def channel_principal(self, channel: str, account_id: str, sender_id: str) -> Principal | None:
        binding = self.lookup_channel(channel, account_id, sender_id)
        return self.principal(binding.principal_id) if binding is not None else None
