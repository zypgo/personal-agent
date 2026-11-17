"""Resolve configured provider boundaries without constructing clients or making requests.

The CLI's preflight checks and the actual clients use the same rules. Speech may override its
endpoint and credential independently; omitting both retains the original shared-endpoint setup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from personal_agent.config import MediaConfig, ModelConfig

DEFAULT_KEY_ENV = "OPENAI_API_KEY"


def is_native_openai(base_url: str | None) -> bool:
    """Only the native HTTPS API has known hosted-tool support by default.

    A gateway, alternate path or lookalike hostname must opt in explicitly. The parser also
    rejects URLs carrying credentials, a query or a fragment rather than silently trusting them.
    """
    if base_url is None:
        return True
    try:
        url = urlsplit(base_url)
        return (
            url.scheme == "https"
            and url.hostname == "api.openai.com"
            and url.port in (None, 443)
            and url.path.rstrip("/") == "/v1"
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class Endpoint:
    base_url: str | None
    api_key_env: str

    @property
    def key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None

    @property
    def allows_anonymous(self) -> bool:
        """Legacy custom endpoints may ignore a key; an explicitly named custom credential must
        exist. Never borrow a different provider's key when that variable is missing."""
        return (
            self.base_url is not None
            and not is_native_openai(self.base_url)
            and self.api_key_env == DEFAULT_KEY_ENV
        )

    @property
    def missing_key(self) -> bool:
        return self.key is None and not self.allows_anonymous


def model_endpoint(cfg: ModelConfig) -> Endpoint:
    return Endpoint(cfg.base_url or os.environ.get("OPENAI_BASE_URL") or None, cfg.api_key_env)


def transcription_endpoint(model: ModelConfig, media: MediaConfig) -> Endpoint:
    inherited = model_endpoint(model)
    return Endpoint(
        media.transcribe_base_url or inherited.base_url,
        media.transcribe_api_key_env or inherited.api_key_env,
    )


def supports_hosted_search(cfg: ModelConfig) -> bool:
    if cfg.api != "responses":
        return False
    declared = cfg.capabilities.hosted_web_search
    return declared if declared is not None else is_native_openai(model_endpoint(cfg).base_url)
