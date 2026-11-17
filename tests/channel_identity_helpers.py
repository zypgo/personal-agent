"""Explicit V3 identity setup for legacy channel behavior tests (no authorization bypass)."""

import json

from personal_agent.channels.dingtalk.channel import account_key
from personal_agent.collaboration.models import OWNER_ID, OWNER_SPACE


def bind_owner(app, channel, recipient):
    """The test owner intentionally links this external account to the existing private space."""
    if channel == "telegram":
        account, sender = "1", recipient  # Authenticated getMe result from FakeTelegram.
    else:
        client, corp, sender = json.loads(recipient)
        account = account_key(client, corp)
    code = app.identities.create_channel_binding(OWNER_ID, OWNER_SPACE).code
    return app.identities.bind_channel(code, channel, account, sender, recipient)


def revoke_owner(app, channel, recipient):
    if channel == "telegram":
        account, sender = "1", recipient
    else:
        client, corp, sender = json.loads(recipient)
        account = account_key(client, corp)
    return app.identities.revoke_channel(OWNER_ID, channel, account, sender)
