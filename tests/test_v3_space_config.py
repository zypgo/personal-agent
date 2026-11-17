"""What a new space takes over from the instance's configuration, and what it never does.

`test_collaboration_identity.py` checks that no private capability of the owner is inherited.
This file adds the other half: the channels of the owner's own accounts stay out, and what the
space does need (the model, the spending limits, how long a question waits) is carried over.
"""

from __future__ import annotations

from personal_agent.collaboration.scopes import derive_space_config
from personal_agent.config import Config


def test_a_new_space_has_none_of_the_owners_chat_bots_or_webhooks():
    route = {"scheme": "generic", "secret_env": "HOOK_SECRET", "prompt": "Review it"}
    root = Config.model_validate(
        {
            "telegram": {"enabled": True},
            "dingtalk": {"enabled": True},
            "webhooks": {"enabled": True, "routes": {"deploy": route}},
            "web": {"enabled": True},
            "notify": {"channel": "telegram"},
            "runtime": {"concurrency": 8},
        }
    )
    child = derive_space_config(root)
    assert not child.telegram.enabled
    assert not child.dingtalk.enabled
    assert not child.webhooks.enabled and not child.webhooks.routes
    assert not child.web.enabled
    assert child.notify.channel == "web"
    assert child.runtime.concurrency == 1
    assert root.telegram.enabled and root.dingtalk.enabled and root.webhooks.routes


def test_a_new_space_keeps_the_model_the_limits_and_the_time_a_question_waits():
    root = Config.model_validate(
        {
            "model": {"main": "some-main-model", "fast": "some-fast-model"},
            "budget": {"per_run_usd": 0.25, "daily_usd": 2.5},
            "policy": {"approval_timeout_hours": 7},
            "runtime": {"run_timeout_s": 123.0},
        }
    )
    child = derive_space_config(root)
    assert child.model == root.model
    assert child.budget == root.budget
    assert child.policy.approval_timeout_hours == 7
    assert child.runtime.run_timeout_s == 123.0
