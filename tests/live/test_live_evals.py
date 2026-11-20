"""The always-on scenarios against the real model: `pa eval run --live`, as tests.

Each built-in scenario is played twice with `gpt-6.1-sol` (medium reasoning) and must hold both
times: an agent that is right only most of the time wakes the owner at night or goes quiet when
something is wrong. A failure here is a finding about the model or the prompts, not flakiness to
retry away: the report says which check failed and what the agent said.
"""

from __future__ import annotations

import pytest
from livehelp import LIVE_HOME, live_config, record_spend

from live_credentials import live_key
from personal_agent.evals.runner import run_live
from personal_agent.evals.spec import load_catalog

SCENARIOS = list(load_catalog())


@pytest.fixture
def key(monkeypatch):
    try:
        value = live_key(LIVE_HOME)
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)
    monkeypatch.setenv("OPENAI_API_KEY", value)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_the_model_behaves_in_every_built_in_scenario(key, scenario):
    run = await run_live([scenario], owner=live_config(), trials=2, cap_usd=0.5)
    record_spend(run.cost_usd)
    [result] = run.results
    problems = [
        f"trial {t.index + 1}: " + (t.error or "; ".join(f"{v.id}: {v.detail}" for v in t.failed))
        for t in result.trials
        if not t.ok
    ]
    assert run.ok, "\n".join(problems) or f"{result.skipped} trial(s) not played: out of budget"
