from __future__ import annotations

import httpx
import openai
import pytest
from agents.exceptions import MaxTurnsExceeded

from personal_agent.harness.errors import ModelUnavailable, PreferencesProblem, classify

REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def status_error(cls, status: int, message: str = "boom"):
    response = httpx.Response(status, request=REQUEST)
    return cls(message, response=response, body=None)


@pytest.mark.parametrize(
    "exc",
    [
        ModelUnavailable("no key"),
        PreferencesProblem("unreadable"),
        status_error(openai.AuthenticationError, 401),
        status_error(openai.PermissionDeniedError, 403),
        status_error(openai.RateLimitError, 429),
        status_error(openai.InternalServerError, 500),
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
    ],
    ids=lambda e: type(e).__name__,
)
def test_problems_with_the_environment_are_not_the_events_fault(exc):
    failure = classify(exc)
    assert failure.environmental
    assert failure.retry_in > 0


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("bug"),
        ValueError("bad value"),
        MaxTurnsExceeded("too many turns"),
        status_error(openai.BadRequestError, 400, "invalid schema"),
        status_error(openai.NotFoundError, 404, "no such model"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_everything_else_counts_against_the_event(exc):
    failure = classify(exc)
    assert not failure.environmental
    assert failure.retry_in == 0


def test_a_missing_key_points_at_doctor():
    assert "pa doctor" in classify(ModelUnavailable("no key")).user_hint


def test_running_out_of_quota_backs_off_much_longer_than_a_burst_limit():
    quota = classify(status_error(openai.RateLimitError, 429, "insufficient_quota: add credit"))
    burst = classify(status_error(openai.RateLimitError, 429, "slow down"))
    assert quota.retry_in > burst.retry_in
    assert quota.user_hint and "quota" in quota.user_hint
    assert burst.user_hint is None


def test_secrets_are_scrubbed_from_failure_text(monkeypatch):
    key = "sk-live-abcdef0123456789abcdef0123456789"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    failure = classify(RuntimeError(f"upstream said the key {key} is invalid"))
    assert key not in failure.summary
    assert "invalid" in failure.summary
