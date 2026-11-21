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
            "还没有配置模型的 API 密钥。运行 `pa doctor`，看看该设置哪个变量。",
        )
    if isinstance(exc, BudgetExceeded) and exc.scope == "daily":
        return Failure(
            True,
            exc.resets_in_s or 3600.0,
            text,
            f"今天的预算（${exc.limit:.2f}）用完了，过了零点会接着做。"
            "想今天多给 $2，运行 `pa budget extend 2`。",
            notice_key="budget:daily",
        )
    if isinstance(exc, BudgetExceeded):
        return Failure(
            False,
            0.0,
            text,
            f"这件事花到了费用上限（${exc.limit:.2f}），已经停下。",
        )
    if isinstance(exc, RunTimedOut):
        return Failure(False, 0.0, text, "这次花的时间太长，我先停下了。已经做完的部分都保留着。")
    if isinstance(exc, PreferencesProblem):
        return Failure(
            True,
            120.0,
            text,
            "没能读懂你的偏好文件，我先停下来，不乱猜。",
        )
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return Failure(
            True,
            300.0,
            text,
            "模型服务拒绝了这个 API 密钥。运行 `pa doctor` 检查一下配置的密钥。",
        )
    if isinstance(exc, openai.RateLimitError):
        quota = "insufficient_quota" in str(exc)
        return Failure(
            True,
            900.0 if quota else 60.0,
            text,
            "模型服务的账户额度用完了。" if quota else None,
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
