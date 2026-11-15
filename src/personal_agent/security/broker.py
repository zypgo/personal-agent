"""Credentials the agent can use but never see.

The model writes `{{secret:github}}` where a token belongs (a header, the URL, the body). The tool
fills the real value in at the last moment, and only if the request goes to a host the owner listed
for that secret. The value never enters the conversation, the logs or the audit trail, and anything
that echoes it back is scrubbed from the result. A prompt-injected page that talks the model into
sending `{{secret:github}}` to `evil.example` gets a refusal, not your token.

This is the "credential broker" idea from Anthropic's managed-agents design (the sandbox never holds
the credentials; a proxy attaches them per destination), reduced to what one process can do.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from personal_agent.config import SecretConfig

PLACEHOLDER = re.compile(r"\{\{\s*secret:([A-Za-z][A-Za-z0-9_]{0,40})\s*\}\}")
ANY_PLACEHOLDER = re.compile(r"\{\{\s*secret:[^}]*\}\}")


class SecretProblem(Exception):
    """A request used a secret in a way that is not allowed. The message is for the model."""


def host_matches(pattern: str, host: str) -> bool:
    """`api.example.com` matches itself; `*.example.com` matches any subdomain, not the apex."""
    pattern = pattern.strip().lower().rstrip(".")
    host = host.strip().lower().rstrip(".")
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


class SecretBroker:
    def __init__(self, secrets: Mapping[str, SecretConfig], env: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)
        self._env = env

    def names(self) -> list[str]:
        return sorted(self._secrets)

    def describe(self) -> str:
        """What the model may be told: the names and where each may be used, never a value."""
        return "; ".join(
            f"{{{{secret:{name}}}}} (only for {', '.join(self._secrets[name].hosts)})"
            for name in self.names()
        )

    def used_in(self, *texts: str | None) -> set[str]:
        return {m.group(1) for text in texts if text for m in PLACEHOLDER.finditer(text)}

    def check(
        self, names: Iterable[str], *, host: str, https: bool, private_ok: bool = False
    ) -> None:
        """Raise SecretProblem unless every named secret may go to this destination."""
        for name in sorted(set(names)):
            secret = self._secrets.get(name)
            if secret is None:
                known = ", ".join(self.names()) or "none are configured"
                raise SecretProblem(f"there is no secret named {name!r} (available: {known}).")
            if not any(host_matches(pattern, host) for pattern in secret.hosts):
                raise SecretProblem(
                    f"the secret {name!r} may only be sent to {', '.join(secret.hosts)}, "
                    f"not to {host}."
                )
            if not https and not private_ok:
                raise SecretProblem(
                    f"the secret {name!r} is only sent over https (this request is plain http)."
                )
            if not self._env.get(secret.env):
                raise SecretProblem(
                    f"the secret {name!r} has no value yet: the owner has to set {secret.env}."
                )

    def fill(self, text: str) -> str:
        """Replace every placeholder with its value. `check` must have passed first."""

        def value(match: re.Match[str]) -> str:
            return self._env[self._secrets[match.group(1)].env]

        return PLACEHOLDER.sub(value, text)

    def reject_malformed(self, *texts: str | None) -> None:
        """A `{{secret:...}}` that does not parse as a name must not slip through unfilled."""
        for text in texts:
            if not text:
                continue
            for match in ANY_PLACEHOLDER.finditer(text):
                if not PLACEHOLDER.fullmatch(match.group(0)):
                    raise SecretProblem(
                        f"{match.group(0)!r} is not a valid secret placeholder; "
                        "write {{secret:name}} with the name exactly as configured."
                    )
