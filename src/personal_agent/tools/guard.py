"""The guard every tool call passes through: policy, approval, audit, timeout, output hygiene.

Tools never check permissions themselves. `ToolGuard.wrap` returns a copy of the tool whose
`needs_approval` asks the policy and whose invocation re-checks it, so a tool call cannot reach
the tool body without a decision, and the decision is written to the audit log either way.

The SDK evaluates `needs_approval` first: a call that needs the owner interrupts the run (see
the harness for suspension). The invocation wrapper is a second line of defense: it refuses
denied calls and unapproved calls even if the SDK's ordering ever changes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from agents import FunctionTool, RunContextWrapper
from agents.tool_context import ToolContext

from personal_agent.core.sessions import INTERRUPTED_RESULT
from personal_agent.harness.runctx import RunContext
from personal_agent.security.audit import safe_args
from personal_agent.security.policy import Decision, ToolCall, Verdict
from personal_agent.security.redact import known_secret_values, redact
from personal_agent.tools.base import ToolDef

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

DENIED_TEXT = (
    "Denied: {reason}. Do not try to work around this. Tell the owner what you could not do."
)
NOT_APPROVED_TEXT = "Denied: this action needs the owner's approval, which has not been given."
SUMMARY_CHARS = 300


def parse_args(raw: Any) -> dict[str, Any]:
    """Tool arguments as a dict; anything unparseable becomes {} (the tool reports the error)."""
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def summarize(tool: ToolDef, args: dict[str, Any]) -> str:
    """One line the owner reads when asked to approve a call."""
    text = tool.detail_of(args) or safe_args(args)
    return redact(text, known_secret_values(os.environ))[:SUMMARY_CHARS]


class ToolGuard:
    def __init__(self, app: App) -> None:
        self.app = app

    # ---- what the model is offered -------------------------------------------------------
    def offered(self, tool: ToolDef, profile: str) -> bool:
        """Tools that policy would refuse on every call are not shown to the model at all."""
        if profile not in tool.profiles or tool.unavailable(self.app):
            return False
        return not self.app.policy.blocks_entirely(
            tool.name, tool.category, can_vary=bool(tool.category_for)
        )

    def wrap(self, tool: ToolDef) -> FunctionTool:
        original = tool.tool
        guard = self

        async def needs_approval(
            wrapper: RunContextWrapper[RunContext], args: dict[str, Any], call_id: str
        ) -> bool:
            rc = wrapper.context
            if guard.app.approvals.is_approved(rc.run_id, call_id):
                return False
            decision = await guard.decide(tool, args or {}, rc, call_id)
            return decision.verdict is Verdict.ASK

        async def invoke(tool_context: ToolContext[RunContext], raw_args: str) -> Any:
            return await guard.invoke(tool, original, tool_context, raw_args)

        return dataclasses.replace(original, needs_approval=needs_approval, on_invoke_tool=invoke)

    # ---- deciding ------------------------------------------------------------------------
    def _call(self, tool: ToolDef, args: dict[str, Any], rc: RunContext) -> ToolCall:
        return ToolCall(
            tool=tool.name,
            category=tool.category_of(rc, args),
            detail=tool.detail_of(args),
            profile=rc.profile,
            tainted=rc.tainted,
            allowed_profiles=tool.profiles,
        )

    async def decide(
        self, tool: ToolDef, args: dict[str, Any], rc: RunContext, call_id: str
    ) -> Decision:
        """The policy's decision for one call (made once per call id, and audited)."""
        cached = rc.decisions.get(call_id)
        if cached is not None:
            return cached
        app = self.app
        call = self._call(tool, args, rc)
        problem = tool.problem_with(rc, args)
        if floor := tool.floor_of(rc, args):
            decision = Decision(Verdict.DENY, "floor", floor)
        elif problem:
            # Not worth the owner's attention: it cannot work. The model is told what is wrong.
            decision = Decision(Verdict.DENY, "invalid", problem)
        else:
            decision = app.policy.evaluate(call)
        if decision.verdict is Verdict.REVIEW:
            decision = await self._review(call, args, rc)
        rc.decisions[call_id] = decision
        app.audit.record(
            run_id=rc.run_id,
            session_key=rc.session_key,
            call_id=call_id,
            tool=tool.name,
            category=call.category.value,
            verdict=decision.verdict.value,
            layer=decision.layer,
            reason=decision.reason,
            args=args,
            detail=call.detail,
            outcome=(
                "invalid"
                if decision.layer == "invalid"
                else "denied"
                if decision.verdict is Verdict.DENY
                else None
            ),
            tainted=rc.tainted,
        )
        return decision

    async def _review(self, call: ToolCall, args: dict[str, Any], rc: RunContext) -> Decision:
        app = self.app
        review = await app.guardian.review(call, safe_args(args), owner_request=rc.owner_request)
        if review.cost_usd:
            app.runs.add_usage(
                rc.run_id,
                requests=1,
                input_tokens=review.input_tokens,
                output_tokens=review.output_tokens,
                cost_usd=review.cost_usd,
            )
        verdict = {"approve": Verdict.ALLOW, "deny": Verdict.DENY}.get(review.verdict, Verdict.ASK)
        return Decision(verdict, "guardian", review.reason or "reviewed")

    # ---- invoking ------------------------------------------------------------------------
    async def invoke(
        self,
        tool: ToolDef,
        original: FunctionTool,
        tool_context: ToolContext[RunContext],
        raw_args: str,
    ) -> Any:
        app = self.app
        rc: RunContext = tool_context.context
        call_id = tool_context.tool_call_id
        args = parse_args(raw_args)

        if app.approvals.is_approved(rc.run_id, call_id):
            # An approved action runs at most once. If a crash interrupted an earlier attempt to
            # resume this run, its effect is unknown: say so instead of doing it a second time.
            if app.audit.outcome_of(rc.run_id, call_id) not in (None, "pending"):
                return INTERRUPTED_RESULT
            # The owner said yes. Hard rules still win: neither a safety floor nor a deny rule
            # added since the question was asked can be bypassed.
            base = app.policy.evaluate(self._call(tool, args, rc))
            if floor := tool.floor_of(rc, args):
                decision = Decision(Verdict.DENY, "floor", floor)
            elif base.verdict is Verdict.DENY:
                decision = base
            else:
                decision = Decision(Verdict.ALLOW, "approval", "approved by the owner")
        else:
            decision = await self.decide(tool, args, rc, call_id)

        if decision.verdict is Verdict.DENY:
            if decision.layer == "invalid":
                return f"Error: {decision.reason}"
            app.audit.set_outcome(rc.run_id, call_id, "denied")
            return DENIED_TEXT.format(reason=decision.reason)
        if decision.verdict is not Verdict.ALLOW:
            app.audit.set_outcome(rc.run_id, call_id, "denied")
            return NOT_APPROVED_TEXT

        started = time.monotonic()
        outcome = "ok"
        app.audit.set_outcome(rc.run_id, call_id, "running")  # durable marker: it may have started
        limit = tool.timeout_of(app, args) or app.config.runtime.tool_timeout_s
        try:
            result = await asyncio.wait_for(original.on_invoke_tool(tool_context, raw_args), limit)
        except TimeoutError:
            outcome = "timeout"
            result = (
                f"Error: {tool.name} did not finish within {limit:.0f} seconds. It may have partly "
                "run; check the current state before trying again."
            )
        except Exception:
            app.audit.set_outcome(
                rc.run_id, call_id, "error", int((time.monotonic() - started) * 1000)
            )
            raise

        if isinstance(result, str):
            result = self._clean(result)
            if result.startswith("Error") and outcome == "ok":
                outcome = "error"
        if tool.taints and outcome == "ok":
            rc.taint(f"tool:{tool.name}")
        app.audit.set_outcome(rc.run_id, call_id, outcome, int((time.monotonic() - started) * 1000))
        return result

    def _clean(self, text: str) -> str:
        text = redact(text, known_secret_values(os.environ))
        cap = self.app.config.runtime.tool_output_chars
        if len(text) > cap:
            text = f"{text[:cap]}\n[... {len(text) - cap} more characters omitted]"
        return text
