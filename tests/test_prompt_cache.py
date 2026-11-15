"""Runs of one kind share a prompt-cache key, so the instructions and tools (thousands of tokens)
are paid for once rather than once per conversation.

Found with the real API: the SDK's own key is made per session, and OpenAI never shares a cached
prompt start between keys. Every timer, webhook and chat paid full price for identical text."""

from __future__ import annotations

import pytest
from agents import OpenAIResponsesModel
from agents.testing import ScriptedModel, assistant_message
from openai import AsyncOpenAI
from pydantic import ValidationError

from helpers import owner_message, timer, webhook
from personal_agent.config import Config, ModelConfig
from personal_agent.harness.models import cache_key_for, model_settings, shares_prompt_cache
from tooling import drain

TEST_KEY = "sk-" + "test-" + "x" * 20


class OpenAILike(ScriptedModel):
    """What the real Responses model says about itself when it talks to OpenAI."""

    def _supports_default_prompt_cache_key(self) -> bool:
        return True


def sent(model: ScriptedModel, call: int = 0):
    return model.calls[call].model_settings


# ---- the settings ---------------------------------------------------------------------------
def test_a_key_is_sent_as_an_extra_argument():
    settings = model_settings(ModelConfig(), cache_key="personal-agent:full")
    assert settings.extra_args == {"prompt_cache_key": "personal-agent:full"}


def test_without_a_key_nothing_is_added_so_the_sdk_keeps_its_own():
    settings = model_settings(ModelConfig(prompt_cache_retention="24h"))
    assert settings.extra_args is None
    assert settings.prompt_cache_retention is None  # only sent where the key is understood


def test_extended_retention_is_sent_only_when_asked_for():
    keyed = model_settings(ModelConfig(), cache_key="k")
    assert keyed.prompt_cache_retention is None
    assert (
        model_settings(
            ModelConfig(prompt_cache_retention="24h"), cache_key="k"
        ).prompt_cache_retention
        == "24h"
    )


def test_only_the_two_known_retention_values_are_accepted():
    with pytest.raises(ValidationError):
        ModelConfig(prompt_cache_retention="forever")  # type: ignore[arg-type]
    assert Config.model_validate({"model": {"prompt_cache_retention": "in_memory"}})


def test_the_key_depends_on_the_kind_of_run_and_nothing_else():
    keys = {cache_key_for(p) for p in ("full", "scheduled", "untrusted", "dream")}
    assert len(keys) == 4  # their tools and instructions differ, so their caches do
    assert cache_key_for("full") == cache_key_for("full")
    assert all(len(k) <= 64 for k in keys)  # the API's limit
    assert all(k.isascii() and k.isprintable() for k in keys)


# ---- which endpoints get it -----------------------------------------------------------------
def test_the_sdk_still_tells_us_which_endpoints_understand_the_key():
    """We rely on a private hook of the SDK. If an upgrade renames it we would silently lose the
    savings, so this fails loudly instead."""
    official = OpenAIResponsesModel(
        model="m", openai_client=AsyncOpenAI(api_key=TEST_KEY, base_url="https://api.openai.com/v1")
    )
    other = OpenAIResponsesModel(
        model="m", openai_client=AsyncOpenAI(api_key=TEST_KEY, base_url="http://localhost:1234/v1")
    )
    assert shares_prompt_cache(official) is True
    assert shares_prompt_cache(other) is False


def test_a_model_that_says_nothing_gets_no_key():
    assert shares_prompt_cache(object()) is False
    assert shares_prompt_cache(ScriptedModel([])) is False


# ---- in real runs ---------------------------------------------------------------------------
async def test_every_conversation_of_the_same_kind_sends_the_same_key(make_app):
    model = OpenAILike(
        [[assistant_message("a")], [assistant_message("b")], [assistant_message("c")]]
    )
    app = make_app(model)
    app.inbox.add(owner_message("one", session="work"))
    app.inbox.add(owner_message("two", session="family"))
    app.inbox.add(timer("tea", "make tea"))
    await drain(app)

    by_session = {}
    for call in model.calls:  # the context block of the first item names the session
        session = call.input[0]["content"].split("session: ")[1].split("\n")[0]
        by_session[session] = call.model_settings.extra_args
    assert by_session == {
        "work": {"prompt_cache_key": "personal-agent:full"},
        "family": {"prompt_cache_key": "personal-agent:full"},
        "timer:tea": {"prompt_cache_key": "personal-agent:scheduled"},
    }


async def test_outside_events_run_under_their_own_key(make_app):
    model = OpenAILike([[assistant_message("noted")]])
    app = make_app(model)
    app.inbox.add(webhook("github", {"action": "opened"}))
    await drain(app)
    assert sent(model).extra_args == {"prompt_cache_key": "personal-agent:untrusted"}


async def test_a_second_run_in_the_same_conversation_keeps_the_key(make_app):
    model = OpenAILike([[assistant_message("a")], [assistant_message("b")]])
    app = make_app(model)
    app.inbox.add(owner_message("one"))
    await drain(app)
    app.inbox.add(owner_message("two"))
    await drain(app)
    assert sent(model, 0).extra_args == sent(model, 1).extra_args


async def test_a_model_that_is_not_openai_is_left_alone(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=Config(model=ModelConfig(prompt_cache_retention="24h")))
    app.inbox.add(owner_message("hello"))
    await drain(app)
    assert sent(model).extra_args is None
    assert sent(model).prompt_cache_retention is None


async def test_extended_retention_reaches_the_request_when_configured(make_app):
    model = OpenAILike([[assistant_message("hi")]])
    app = make_app(model, config=Config(model=ModelConfig(prompt_cache_retention="24h")))
    app.inbox.add(owner_message("hello"))
    await drain(app)
    assert sent(model).prompt_cache_retention == "24h"
