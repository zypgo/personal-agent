"""The scenarios that ship with the agent: each must prove that its checks can fail.

A scripted model plays every scenario's good run (all checks pass) and each of its named bad runs
(the checks they are meant to trip must fail). A scenario that cannot do that measures nothing,
and nobody would notice until the agent misbehaved in front of the owner.
"""

from __future__ import annotations

import pytest

from personal_agent.evals.runner import check_scenario
from personal_agent.evals.spec import load_catalog

SCENARIOS = list(load_catalog())

KNOWN_TAGS = {"always-on", "quiet", "honesty", "security", "memory"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_a_built_in_scenario_tells_its_good_run_from_each_bad_run(scenario):
    checked = await check_scenario(scenario)
    problems: list[str] = []
    assert checked.good is not None
    if checked.good.error:
        problems.append(f"the good run could not be played: {checked.good.error}")
    problems += [f"the good run fails {v.id}: {v.detail}" for v in checked.good.failed]
    for bad in checked.bad:
        if not bad.caught:
            problems.append(
                f"bad run {bad.name!r} should fail {bad.expected} but failed {bad.failed}"
                + (f" ({bad.error})" if bad.error else "")
            )
    assert not problems, "\n".join(problems)
    assert len(checked.bad) >= 1


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_a_built_in_scenario_is_documented_and_tagged(scenario):
    assert len(scenario.about.split()) >= 15, "say what is being protected and why"
    assert scenario.tags and set(scenario.tags) <= KNOWN_TAGS
    assert any(c.kind == "spend_under" for c in scenario.checks), "a runaway agent must fail"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_check_of_a_built_in_scenario_is_shown_to_fail_somewhere(scenario):
    """A check that no bad run ever trips could be deleted without anyone noticing.

    Three kinds are exempt: they guard what the product itself enforces (the profile a run gets,
    the tools it has, the owner's files being read-only), which no scripted run can break; the
    unit tests of the checks prove they can fail. The budget check is proved by the money tests."""
    assert scenario.mock is not None
    expected = {name for bad in scenario.mock.bad for name in bad.fails}
    guards = {"profile", "only_tools", "memory_untouched", "spend_under"}
    untested = [
        name
        for name, check in scenario.labelled_checks()
        if name not in expected and check.kind not in guards
    ]
    assert not untested, f"no bad run is meant to fail {untested}"


def test_the_scenarios_cover_the_always_on_failures_that_matter():
    ids = {s.id for s in SCENARIOS}
    assert {
        "quiet-when-nothing-new",  # a quiet day stays quiet
        "news-once",  # the owner is told once
        "unreadable-source-is-not-a-quiet-day",  # a check that failed is not "nothing new"
        "webhook-injection-is-data",  # outside text is never an instruction
        "preferences-are-followed",  # the owner's standing rules hold
        "progress-file-across-conversations",  # work that takes days is not lost between runs
    } <= ids
