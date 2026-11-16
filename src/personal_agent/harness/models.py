"""Model endpoints: build SDK `Model` objects and `ModelSettings` from our config.

The harness only knows the `ModelFactory` type (model name -> SDK `Model`), so tests plug in a
scripted model and users can point `base_url` at any OpenAI-compatible server.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from agents import ModelSettings, OpenAIChatCompletionsModel, OpenAIResponsesModel
from agents.models.interface import Model
from agents.retry import ModelRetrySettings, retry_policies
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from personal_agent.config import ModelConfig
from personal_agent.harness.errors import ModelUnavailable

ModelFactory = Callable[[str], Model]


def make_client(cfg: ModelConfig, **options: Any) -> AsyncOpenAI:
    """An OpenAI client for the configured endpoint (`options` are passed on: timeout, retries)."""
    key = os.environ.get("OPENAI_API_KEY") or None
    base_url = cfg.base_url or os.environ.get("OPENAI_BASE_URL") or None
    if key is None:
        if base_url is None:
            raise ModelUnavailable(
                "OPENAI_API_KEY is not set. Put it in ~/.personal-agent/.env (see .env.example)."
            )
        key = "not-needed"  # many local OpenAI-compatible servers ignore the key
    return AsyncOpenAI(api_key=key, base_url=base_url, **options)


class OpenAIModels:
    """Creates models that share one lazily-created `AsyncOpenAI` client."""

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            # Retries are managed by the SDK runner (ModelSettings.retry); it turns off the
            # client's own retries automatically so the two layers do not multiply.
            self._client = make_client(self.cfg, timeout=self.cfg.timeout_s)
        return self._client

    async def aclose(self) -> None:
        """Close the shared client's connections. They belong to the event loop that is ending;
        asking for a model afterwards opens a new client."""
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def __call__(self, name: str) -> Model:
        if self.cfg.api == "chat_completions":
            return OpenAIChatCompletionsModel(model=name, openai_client=self.client)
        return OpenAIResponsesModel(model=name, openai_client=self.client)


def shares_prompt_cache(model: object) -> bool:
    """Whether `model` talks to OpenAI itself, the only endpoint that is known to understand
    `prompt_cache_key` (the Agents SDK makes the same call before adding a key of its own)."""
    check = getattr(model, "_supports_default_prompt_cache_key", None)
    return bool(check()) if callable(check) else False


def cache_key_for(profile: str) -> str:
    """One routing key for every run of the same kind.

    OpenAI only reuses a cached prompt start for requests with the same `prompt_cache_key`. The
    SDK's own key is made per conversation, so a timer, a webhook and each chat would all pay in
    full for the same instructions and tools. Runs of one profile share those, so they share a key."""
    return f"personal-agent:{profile}"


def model_settings(
    cfg: ModelConfig, *, hosted_search: bool = False, cache_key: str | None = None
) -> ModelSettings:
    """`hosted_search`: also return the sources of OpenAI's web searches, so the addresses the
    model was shown can be told apart from ones it made up (see `tools/urls.py`).
    `cache_key`: see `cache_key_for`; `None` leaves the SDK's own per-conversation key in place."""
    reasoning = Reasoning(effort=cfg.reasoning_effort)
    if cfg.reasoning_context is not None:
        reasoning = Reasoning(effort=cfg.reasoning_effort, context=cfg.reasoning_context)
    stateless_responses = cfg.api == "responses" and not cfg.store
    include = ["reasoning.encrypted_content"] if stateless_responses else []
    if hosted_search:
        include.append("web_search_call.action.sources")
    return ModelSettings(
        reasoning=reasoning,
        verbosity=cfg.verbosity,
        store=cfg.store if cfg.api == "responses" else None,
        # Stateless mode: ask for encrypted reasoning so it can be replayed from our own log.
        response_include=include or None,
        extra_args={"prompt_cache_key": cache_key} if cache_key else None,
        prompt_cache_retention=cfg.prompt_cache_retention if cache_key else None,
        timeout=cfg.timeout_s,
        retry=ModelRetrySettings(
            max_retries=cfg.max_retries,
            backoff={"initial_delay": 1.0, "max_delay": 30.0, "multiplier": 2.0, "jitter": True},
            policy=retry_policies.any(
                retry_policies.provider_suggested(),
                retry_policies.network_error(),
                retry_policies.http_status([408, 429, 500, 502, 503, 504]),
            ),
        ),
    )
