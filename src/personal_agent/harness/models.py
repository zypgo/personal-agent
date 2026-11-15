"""Model endpoints: build SDK `Model` objects and `ModelSettings` from our config.

The harness only knows the `ModelFactory` type (model name -> SDK `Model`), so tests plug in a
scripted model and users can point `base_url` at any OpenAI-compatible server.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from agents import ModelSettings, OpenAIChatCompletionsModel, OpenAIResponsesModel
from agents.models.interface import Model
from agents.retry import ModelRetrySettings, retry_policies
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from personal_agent.config import ModelConfig
from personal_agent.harness.errors import ModelUnavailable

ModelFactory = Callable[[str], Model]


class OpenAIModels:
    """Creates models that share one lazily-created `AsyncOpenAI` client."""

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            key = os.environ.get("OPENAI_API_KEY") or None
            base_url = self.cfg.base_url or os.environ.get("OPENAI_BASE_URL") or None
            if key is None:
                if base_url is None:
                    raise ModelUnavailable(
                        "OPENAI_API_KEY is not set. Put it in ~/.personal-agent/.env (see .env.example)."
                    )
                key = "not-needed"  # many local OpenAI-compatible servers ignore the key
            # Retries are managed by the SDK runner (ModelSettings.retry); it turns off the
            # client's own retries automatically so the two layers do not multiply.
            self._client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=self.cfg.timeout_s)
        return self._client

    def __call__(self, name: str) -> Model:
        if self.cfg.api == "chat_completions":
            return OpenAIChatCompletionsModel(model=name, openai_client=self.client)
        return OpenAIResponsesModel(model=name, openai_client=self.client)


def model_settings(cfg: ModelConfig, *, hosted_search: bool = False) -> ModelSettings:
    """`hosted_search`: also return the sources of OpenAI's web searches, so the addresses the
    model was shown can be told apart from ones it made up (see `tools/urls.py`)."""
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
