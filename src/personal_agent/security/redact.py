"""Detect and scrub secrets, and flag text that tries to instruct the model.

Two jobs:
1. Keep credentials out of memory, logs and tool output (`find_secrets`, `redact`).
2. Notice instruction-like text in data the agent is about to store or read (`injection_hints`).
   This is a tripwire for the owner, not a security boundary: the boundary is that data is
   never treated as instructions and untrusted runs get narrower tools.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# (kind, pattern). Ordered so specific token formats win over the generic assignment rule.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("slack token", re.compile(r"\bxox[abprso]-[A-Za-z0-9-]{10,}")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe key", re.compile(r"\b[rs]k_live_[0-9a-zA-Z]{20,}")),
    # No `\b` in front: the token follows "bot" in every Bot API URL (`/bot123456789:AAA.../getMe`).
    ("telegram bot token", re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{24,}")),
    (
        "credential assignment",
        re.compile(
            r"""(?ix)\b(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token|token|password|passwd|pwd)\b
                \s*[:=]\s*['"]?[A-Za-z0-9_\-./+=!@#$%^&*]{12,}"""
        ),
    ),
]

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "override instructions",
        re.compile(
            r"(?i)\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your)\b.{0,30}\b(instructions?|rules?|prompts?|guidelines?)\b"
        ),
    ),
    (
        "new role",
        re.compile(
            r"(?i)\byou are now\b|\bfrom now on,? you\b|\bact as (?:if you are )?(?:an? )?(?:unrestricted|jailbroken|dan)\b"
        ),
    ),
    (
        "fake system text",
        re.compile(r"(?i)<\|?(?:im_start|system|endoftext)\|?>|\[/?INST\]|^\s*system\s*:", re.M),
    ),
    (
        "secrecy demand",
        re.compile(
            r"(?i)\b(do not|don't|never)\b.{0,20}\b(tell|inform|mention|reveal|show)\b.{0,20}\b(the )?(user|owner)\b"
        ),
    ),
    (
        "exfiltration",
        re.compile(
            r"(?i)\b(send|post|upload|forward|email|exfiltrate)\b.{0,60}\b(api[_ -]?keys?|passwords?|secrets?|credentials?|tokens?|\.env|ssh keys?)\b"
        ),
    ),
]

_SECRET_ENV_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)")
_EXTRA_SECRET_ENV: set[str] = set()


def register_secret_envs(names: Iterable[str]) -> None:
    """Environment variables to scrub from output even though their names do not look secret
    (the owner listed them under `[secrets]`). Process-wide: one process hosts one agent."""
    _EXTRA_SECRET_ENV.update(names)


@dataclass(frozen=True)
class SecretMatch:
    kind: str
    start: int
    end: int


def find_secrets(text: str) -> list[SecretMatch]:
    """Non-overlapping matches of credential-looking strings."""
    found: list[SecretMatch] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in _SECRET_PATTERNS:
        for m in pattern.finditer(text):
            span = (m.start(), m.end())
            if any(span[0] < b and a < span[1] for a, b in taken):
                continue
            taken.append(span)
            found.append(SecretMatch(kind, *span))
    return sorted(found, key=lambda s: s.start)


DECLARED_MIN_LENGTH = 4
"""The shortest value of a variable the owner declared secret that is still cut out of text."""


def known_secret_values(env: Mapping[str, str] | None = None, *, min_length: int = 8) -> list[str]:
    """Values of the environment variables that are secret: the ones whose names look like it, and
    the ones the owner declared (see `register_secret_envs`).

    A guess from a name needs a longer value: `KEYBOARD=us` is not a secret, and `us` would be cut
    out of every sentence. A declared one is a secret whatever its length (a PIN is four digits),
    down to `DECLARED_MIN_LENGTH`."""
    source = os.environ if env is None else env
    values = set()
    for name, value in source.items():
        if not value:
            continue
        if name in _EXTRA_SECRET_ENV:
            if len(value) >= DECLARED_MIN_LENGTH:
                values.add(value)
        elif len(value) >= min_length and _SECRET_ENV_NAME.search(name):
            values.add(value)
    return sorted(values, key=len, reverse=True)  # replace longer values first


def redact(text: str, known: Iterable[str] = ()) -> str:
    """Replace credential-looking strings and any exact `known` secret values.

    Longer values go first whatever order they come in: replacing `abc` before `abcdef` would
    leave `def` of the longer secret in the text."""
    for value in sorted({v for v in known if v}, key=len, reverse=True):
        if value in text:
            text = text.replace(value, "[REDACTED]")
    matches = find_secrets(text)
    if not matches:
        return text
    out: list[str] = []
    last = 0
    for m in matches:
        out.append(text[last : m.start])
        out.append(f"[REDACTED:{m.kind}]")
        last = m.end
    out.append(text[last:])
    return "".join(out)


def injection_hints(text: str) -> list[str]:
    """Names of instruction-like patterns found in `text` (empty if none)."""
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)]
