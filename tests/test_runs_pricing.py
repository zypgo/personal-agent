import pytest

from personal_agent.core.runs import RunStore
from personal_agent.harness.pricing import LONG_CONTEXT_THRESHOLD, cost_usd, is_known, price_for


def test_known_and_dated_models():
    assert price_for("gpt-6.1-sol").input == 2.0
    assert price_for("gpt-6.1-sol-2026-10-28").output == 10.0
    assert price_for("gpt-6-luna").cached_input == 0.01
    assert price_for("gpt-6.1-solar") is None  # prefix must end at a dash boundary
    assert is_known("gpt-6-astra") and not is_known("mystery-model")


def test_cost_basic_and_cached():
    # 200k fresh input ($2/M) + 200k output ($10/M); below the long-context threshold
    cost = cost_usd("gpt-6.1-sol", input_tokens=200_000, output_tokens=200_000)
    assert cost == pytest.approx(0.2 * 2.0 + 0.2 * 10.0)
    # 160k of that input was cached ($0.10/M instead of $2/M)
    cost = cost_usd("gpt-6.1-sol", input_tokens=200_000, cached_tokens=160_000, output_tokens=0)
    assert cost == pytest.approx(0.04 * 2.0 + 0.16 * 0.10)


def test_a_million_token_request_is_billed_at_the_long_context_rate():
    # 1M input is above 272k: input and output are surcharged (2x and 1.5x) for the whole request
    cost = cost_usd("gpt-6.1-sol", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(1.0 * 2.0 * 2 + 1.0 * 10.0 * 1.5)


def test_cache_writes_cost_more():
    base = cost_usd("gpt-6.1-sol", input_tokens=100_000, output_tokens=0)
    with_write = cost_usd(
        "gpt-6.1-sol", input_tokens=100_000, cache_write_tokens=100_000, output_tokens=0
    )
    assert with_write == pytest.approx(base * 1.25)


def test_long_context_surcharge_applies_to_the_whole_request():
    n = LONG_CONTEXT_THRESHOLD + 1
    normal = cost_usd("gpt-6.1-sol", input_tokens=n - 2, output_tokens=1000)
    long = cost_usd("gpt-6.1-sol", input_tokens=n, output_tokens=1000)
    assert long > normal * 1.9


def test_unknown_model_is_priced_conservatively():
    unknown = cost_usd("some-new-model", input_tokens=1000, output_tokens=1000)
    assert unknown == cost_usd("gpt-6-astra", input_tokens=1000, output_tokens=1000)


@pytest.fixture
def runs(db, clock):
    return RunStore(db, clock)


def test_run_lifecycle(runs, clock):
    run_id = runs.start(
        session_key="main", trigger="message", profile="full", model="gpt-6.1-sol", event_ids=[1, 2]
    )
    rec = runs.get(run_id)
    assert rec.status == "running" and rec.event_ids == [1, 2] and rec.finished_at is None

    runs.add_usage(
        run_id,
        input_tokens=100,
        cached_tokens=40,
        output_tokens=10,
        reasoning_tokens=4,
        cost_usd=0.01,
    )
    runs.add_usage(run_id, input_tokens=50, output_tokens=5, cost_usd=0.005)
    runs.add_tool_call(run_id)
    runs.mark_tainted(run_id)
    clock.advance(3)
    runs.finish(run_id, "completed", final_text="all done")

    rec = runs.get(run_id)
    assert (rec.requests, rec.input_tokens, rec.cached_tokens, rec.output_tokens) == (
        2,
        150,
        40,
        15,
    )
    assert rec.reasoning_tokens == 4 and rec.tool_calls == 1 and rec.tainted
    assert rec.cost_usd == pytest.approx(0.015)
    assert (
        rec.status == "completed" and rec.final_text == "all done" and rec.finished_at is not None
    )


def test_cost_since_for_budgets(runs, clock):
    a = runs.start(session_key="main", trigger="message", profile="full", model="m", event_ids=[])
    runs.add_usage(a, cost_usd=1.5)
    clock.advance(86_400)
    b = runs.start(
        session_key="timer:x", trigger="timer", profile="scheduled", model="m", event_ids=[]
    )
    runs.add_usage(b, cost_usd=0.25)
    assert runs.cost_since(clock.now() - 3600) == pytest.approx(0.25)
    assert runs.cost_since(0) == pytest.approx(1.75)
    assert runs.cost_since(0, session_key="main") == pytest.approx(1.5)


def test_orphaned_runs_are_failed_at_startup(runs):
    a = runs.start(session_key="main", trigger="message", profile="full", model="m", event_ids=[])
    b = runs.start(session_key="main", trigger="message", profile="full", model="m", event_ids=[])
    runs.finish(b, "completed")
    assert runs.recover_orphans() == 1
    assert runs.get(a).status == "failed" and "stopped" in runs.get(a).error
    assert runs.get(b).status == "completed"


def test_rejects_unknown_status(runs):
    a = runs.start(session_key="main", trigger="message", profile="full", model="m", event_ids=[])
    with pytest.raises(ValueError):
        runs.finish(a, "exploded")
