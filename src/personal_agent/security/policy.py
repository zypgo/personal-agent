"""Layered tool policy: decide, for one tool call, whether to allow it, ask the owner, or refuse.

Layers, first match wins:

1. `deny` rules: never, in any mode (a floor no mode can lower).
2. Profile: a tool outside the run's profile is refused (it is normally not even offered).
3. `read_only` mode: anything that is not harmless is refused.
4. `always_ask` rules: ask, even in `auto` and `autonomous` mode.
5. Taint: once a run has read content the owner did not write, actions that change the world
   ask first, unless an `allow_when_tainted` rule says otherwise. (Not applied in `autonomous`.)
6. `allow` rules: pre-approved.
7. Mode default: `supervised` asks for anything that is not harmless, `auto` hands the call to a
   reviewer model, `autonomous` allows.

Rules are glob patterns over the tool name, or `tool:detail` where "detail" is a short summary
the tool provides (a shell command, a URL host, a file path). Unknown or odd input never makes
a decision more permissive than the layers above.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase

from personal_agent.config import PolicyConfig
from personal_agent.tools.base import Category

HARMLESS = frozenset({Category.READ, Category.MEMORY, Category.NOTIFY, Category.REMIND})
"""Never ask in `supervised` mode: they only look, tidy the agent's own notes, talk to the owner,
or set a one-off reminder for itself."""

ESCALATE_WHEN_TAINTED = frozenset(
    {Category.SCHEDULE, Category.WRITE, Category.EXEC, Category.NET_WRITE, Category.CONFIG}
)


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    REVIEW = "review"
    """Internal: `auto` mode wants the reviewer model's opinion before deciding."""


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    layer: str
    reason: str


@dataclass(frozen=True)
class ToolCall:
    tool: str
    category: Category
    detail: str
    profile: str
    tainted: bool
    allowed_profiles: frozenset[str]


def matches(rule: str, tool: str, detail: str) -> bool:
    if ":" in rule:
        tool_pattern, detail_pattern = rule.split(":", 1)
        return fnmatchcase(tool, tool_pattern) and fnmatchcase(detail, detail_pattern)
    return fnmatchcase(tool, rule)


class Policy:
    def __init__(self, cfg: PolicyConfig) -> None:
        self.cfg = cfg

    def blocks_entirely(self, tool: str, category: Category, *, can_vary: bool = False) -> bool:
        """True when every call of this tool would be refused, so it need not be offered.

        `can_vary` marks tools whose category depends on the arguments (their base category is
        the strictest one, so a read-only mode must still offer the harmless variants)."""
        for rule in self.cfg.deny:
            tool_pattern, _, detail_pattern = rule.partition(":")
            if fnmatchcase(tool, tool_pattern) and (":" not in rule or detail_pattern == "*"):
                return True
        return self.cfg.mode == "read_only" and category not in HARMLESS and not can_vary

    def _hit(self, rules: list[str], call: ToolCall) -> str | None:
        return next((r for r in rules if matches(r, call.tool, call.detail)), None)

    def evaluate(self, call: ToolCall) -> Decision:
        cfg = self.cfg

        if rule := self._hit(cfg.deny, call):
            return Decision(Verdict.DENY, "deny_rule", f"blocked by your rule {rule!r}")

        if call.profile not in call.allowed_profiles:
            return Decision(
                Verdict.DENY, "profile", f"{call.tool} is not available in {call.profile} runs"
            )

        if cfg.mode == "read_only" and call.category not in HARMLESS:
            return Decision(Verdict.DENY, "read_only", "the agent is in read-only mode")

        if rule := self._hit(cfg.always_ask, call):
            return Decision(Verdict.ASK, "always_ask", f"your rule {rule!r} always asks")

        if call.tainted and call.category in ESCALATE_WHEN_TAINTED and cfg.mode != "autonomous":
            if rule := self._hit(cfg.allow_when_tainted, call):
                return Decision(
                    Verdict.ALLOW, "allow_when_tainted", f"allowed by your rule {rule!r}"
                )
            return Decision(
                Verdict.ASK,
                "taint",
                "this run has read content you did not write, so it asks before acting",
            )

        if rule := self._hit(cfg.allow, call):
            return Decision(Verdict.ALLOW, "allow_rule", f"allowed by your rule {rule!r}")

        if call.category in HARMLESS:
            return Decision(
                Verdict.ALLOW, "mode", f"{call.category.value} actions need no approval"
            )
        if cfg.mode == "autonomous":
            return Decision(Verdict.ALLOW, "mode", "autonomous mode")
        if cfg.mode == "auto":
            return Decision(Verdict.REVIEW, "mode", "auto mode: a reviewer decides")
        return Decision(Verdict.ASK, "mode", f"{call.category.value} actions need your approval")
