"""Authenticated external identity routing and delivery guards shared by built-in channels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from personal_agent.channels.base import ChannelError
from personal_agent.channels.ingress import Ingress
from personal_agent.collaboration.models import OWNER_ID, OWNER_SPACE, ChannelBinding

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.core.outbox import OutMessage


class ChannelAuthority(TypedDict):
    principal_id: str
    membership_version: int | None
    principal_version: int | None


@dataclass(frozen=True)
class ChannelRoute:
    app: App
    ingress: Ingress
    principal_id: str
    membership_version: int | None = None
    principal_version: int | None = None
    legacy: bool = False

    @property
    def authority(self) -> ChannelAuthority:
        return {
            "principal_id": self.principal_id,
            "membership_version": self.membership_version,
            "principal_version": self.principal_version,
        }


def legacy_allowed(app: App, channel: str, account: str, sender: str, legacy_sender: str) -> bool:
    identities = app.identities
    if identities is None:
        return app.pairing.is_approved(channel, legacy_sender)
    return (
        app.space_id == OWNER_SPACE
        and identities.legacy_owner_sender(channel, legacy_sender)
        and not identities.has_channel_binding(channel, account, sender)
        and app.pairing.is_approved(channel, legacy_sender)
        and identities.authorized(OWNER_ID, OWNER_SPACE, write=True)
    )


def incoming_route(
    app: App,
    ingress: Ingress,
    *,
    channel: str,
    account: str,
    sender: str,
    recipient: str,
    legacy_sender: str,
) -> ChannelRoute | None:
    identities = app.identities
    if identities is not None:
        binding = identities.lookup_channel(channel, account, sender)
        if binding is not None and binding.recipient_id == recipient:
            if app.collaboration is None:
                if app.space_id != binding.space_id:
                    return None
                selected = app
            else:
                selected = app.collaboration.get(binding.space_id)
            if selected is app:
                target_ingress = ingress
            else:
                manager = app.collaboration
                assert manager is not None
                target_ingress = Ingress(
                    selected,
                    wake=lambda: manager.wake(binding.space_id),
                )
            return ChannelRoute(
                selected,
                target_ingress,
                binding.principal_id,
                binding.membership_version,
                binding.principal_version,
            )
    if legacy_allowed(app, channel, account, sender, legacy_sender):
        versions = app.actor_snapshot(OWNER_ID)
        return ChannelRoute(
            app,
            ingress,
            OWNER_ID,
            versions.get("membership_version"),
            versions.get("principal_version"),
            legacy=True,
        )
    return None


def validate_delivery(
    app: App,
    message: OutMessage,
    *,
    channel: str,
    account: str,
    sender: str,
    recipient: str,
    legacy_sender: str,
) -> ChannelBinding | None:
    """Revalidate at send time. Old queued replies cannot follow a recipient into another space."""
    identities = app.identities
    actor = message.payload.get("principal_id") or app.space_owner_id
    membership_version = message.payload.get("membership_version")
    principal_version = message.payload.get("principal_version")
    if identities is None:
        if not app.pairing.is_approved(channel, legacy_sender):
            raise ChannelError("That channel account is not approved", permanent=True)
        return None
    if not isinstance(actor, str) or not identities.authorized(
        actor,
        app.space_id,
        write=True,
        version=membership_version,
        principal_version=principal_version,
    ):
        raise ChannelError("The sender's authorization changed", permanent=True)
    binding = identities.lookup_channel(channel, account, sender)
    if binding is not None:
        if (
            binding.space_id == app.space_id
            and binding.principal_id == actor
            and binding.recipient_id == recipient
            and (membership_version is None or membership_version == binding.membership_version)
            and (principal_version is None or principal_version == binding.principal_version)
        ):
            return binding
        raise ChannelError("The channel identity or space changed before delivery", permanent=True)
    if actor == OWNER_ID and legacy_allowed(app, channel, account, sender, legacy_sender):
        return None
    raise ChannelError("That channel account is not bound to this space", permanent=True)
