"""Stable identifiers and immutable identity records. No credential values in these records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OWNER_ID = "owner"
OWNER_SPACE = "personal-owner"
Role = Literal["owner", "member", "viewer"]
SpaceKind = Literal["personal", "project"]


class IdentityError(ValueError):
    """An invalid identity-management request."""


class AccessDenied(IdentityError):
    """An inactive identity, inaccessible space, or insufficient privilege."""


class InvalidInvite(IdentityError):
    """A missing, expired, revoked or already consumed invitation."""


@dataclass(frozen=True)
class Principal:
    id: str
    display_name: str
    is_admin: bool
    authz_version: int
    created_at: float


@dataclass(frozen=True)
class Space:
    id: str
    kind: SpaceKind
    name: str
    owner_id: str
    created_at: float


@dataclass(frozen=True)
class Membership:
    space_id: str
    principal_id: str
    role: Role
    version: int
    joined_at: float

    @property
    def can_write(self) -> bool:
        return self.role in ("owner", "member")

    @property
    def can_manage(self) -> bool:
        return self.role == "owner"


@dataclass(frozen=True)
class Invitation:
    code: str
    expires_at: float
    kind: str
    space_id: str | None = None
    role: Role | None = None


@dataclass(frozen=True)
class Enrollment:
    """What accepting a project invitation did.

    `created`: a new identity was made for the person. `joined`: the project gained a member;
    False when they were already in it, in which case the invitation was not used up."""

    principal: Principal
    space: Space
    created: bool
    recovery_code: str | None = None
    role: Role = "member"
    joined: bool = True


@dataclass(frozen=True)
class InvitePreview:
    """What a project invitation is for, shown before anyone accepts it."""

    space_name: str
    role: Role
    invited_by: str
    expires_at: float
    already_member: bool


@dataclass(frozen=True)
class ResourceGrant:
    id: str
    space_id: str
    resource_type: str
    resource_id: str
    principal_id: str
    actions: tuple[str, ...]
    version: int
    expires_at: float | None


@dataclass(frozen=True)
class ChannelBinding:
    channel: str
    account_id: str
    sender_id: str
    principal_id: str
    space_id: str
    recipient_id: str
    membership_version: int
    principal_version: int
    version: int
