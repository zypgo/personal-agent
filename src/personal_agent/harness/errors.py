"""Failure taxonomy for runs.

The inbox needs to know whether a failure is the *event's* fault (a bug triggered by this
input: counts toward the poison-pill limit) or the *environment's* (network down, rate limit,
bad or missing API key: retry later, never kill the event).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import openai

from personal_agent.core.budget import BudgetExceeded
from personal_agent.security.redact import known_secret_values, redact

__all__ = [
    "BudgetExceeded",
    "Failure",
    "ModelUnavailable",
    "PreferencesProblem",
    "RunTimedOut",
    "classify",
]


class ModelUnavailable(Exception):
    """The model endpoint cannot be used at all (for example, no API key configured)."""


class PreferencesProblem(Exception):
    """The owner's preferences could not be read; the run was stopped on purpose."""


class RunTimedOut(Exception):
    """A run took longer than `runtime.run_timeout_s` and was stopped."""


@dataclass(frozen=True)
class Failure:
    environmental: bool
    retry_in: float
    summary: str
    user_hint: str | None = None
    """What the owner can do about it, if anything."""
    notice_key: str | None = None
    """Tell the owner once per key, even when no conversation is waiting (a paused budget)."""


def classify(exc: BaseException) -> Failure:
    text = redact(f"{type(exc).__name__}: {exc}"[:600], known_secret_values(os.environ))
    if isinstance(exc, ModelUnavailable):
        return Failure(
            True,
            300.0,
            text,
            "There is no OpenAI API key yet. Run `pa doctor` to see where to put it.",
        )
    if isinstance(exc, BudgetExceeded) and exc.scope == "daily":
        return Failure(
            True,
            exc.resets_in_s or 3600.0,
            text,
            f"Today's budget (${exc.limit:.2f}) is used up. Work resumes after midnight; "
            "run `pa budget extend 2` to allow $2 more today.",
            notice_key="budget:daily",
        )
    if isinstance(exc, BudgetExceeded):
        return Failure(
            False,
            0.0,
            text,
            f"That task reached its cost limit (${exc.limit:.2f}) and was stopped.",
        )
    if isinstance(exc, RunTimedOut):
        return Failure(
            False, 0.0, text, "That took too long, so I stopped it. What I did so far is saved."
        )
    if isinstance(exc, PreferencesProblem):
        return Failure(
            True,
            120.0,
            text,
            "I could not read your preferences file, so I stopped on purpose instead of guessing.",
        )
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return Failure(
            True, 300.0, text, "OpenAI rejected the API key. Check OPENAI_API_KEY (`pa doctor`)."
        )
    if isinstance(exc, openai.RateLimitError):
        quota = "insufficient_quota" in str(exc)
        return Failure(
            True,
            900.0 if quota else 60.0,
            text,
            "The OpenAI account is out of quota or credit." if quota else None,
        )
    if isinstance(
        exc, openai.APITimeoutError | openai.APIConnectionError | openai.InternalServerError
    ):
        return Failure(True, 30.0, text)
    if isinstance(exc, openai.APIStatusError) and getattr(exc, "status_code", 0) in (
        408,
        409,
        429,
        500,
        502,
        503,
        504,
    ):
        return Failure(True, 30.0, text)
    # Everything else (including MaxTurnsExceeded, ModelBehaviorError, UserError and 4xx request
    # errors) is attributable to this event or to our own code: it counts toward the attempt limit.
    return Failure(False, 0.0, text)
