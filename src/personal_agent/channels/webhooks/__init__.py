"""Webhooks: other programs (GitHub, a build server, your own scripts) wake the agent."""

from personal_agent.channels.webhooks.service import WebhookService, route_url
from personal_agent.channels.webhooks.signatures import (
    SCHEMES,
    Rejected,
    SecretProblem,
    new_secret,
    secret_key,
    sign,
    verify,
)

__all__ = [
    "SCHEMES",
    "Rejected",
    "SecretProblem",
    "WebhookService",
    "new_secret",
    "route_url",
    "secret_key",
    "sign",
    "verify",
]
