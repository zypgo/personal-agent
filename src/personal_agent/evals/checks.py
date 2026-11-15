"""Grading a trial. Every check looks at an effect (what was said, which tools ran, what was written
to memory, what it cost) and says why it passed or failed. None looks at exact wording: a model
says the same thing many ways, and a check that wants one of them measures luck.

Every step also gets an implicit check, `ends-<step>`: its runs ended as the step allows. Without
it, a run that crashed would "stay silent" and pass every `silent` check.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from personal_agent.evals.spec import (
    Check,
    MaxMessages,
    MemoryHas,
    MemoryUntouched,
    NeverSays,
    NotUrgent,
    OnlyTools,
    Profile,
    Says,
    Silent,
    SpendUnder,
    Step,
    ToolsNotUsed,
    ToolsUsed,
)

if TYPE_CHECKING:
    from personal_agent.core.outbox import OutMessage
    from personal_agent.evals.bench import StepResult, Trial

MAX_FILE_CHARS = 200_000


@dataclass(frozen=True)
class Verdict:
    id: str
    ok: bool
    detail: str


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _quote(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return repr(flat if len(flat) <= limit else flat[: limit - 1] + "…")


def _steps(trial: Trial, step: str | None) -> list[StepResult]:
    return list(trial.steps) if step is None else [trial.step(step)]


def _messages(trial: Trial, step: str | None) -> list[OutMessage]:
    return [m for s in _steps(trial, step) for m in s.messages]


def _tools(trial: Trial, step: str | None) -> set[str]:
    return {t for s in _steps(trial, step) for t in s.tools}


def _seen(trial: Trial, step: str | None) -> str:
    return "\n".join(m.text for m in _messages(trial, step))


def _says(trial: Trial, check: Says) -> tuple[bool, str]:
    messages = _messages(trial, check.step)
    if not messages:
        return False, "said nothing"
    text = _seen(trial, check.step)
    folded = _norm(text)
    missing: list[str] = []
    if check.any_of and not any(_norm(p) in folded for p in check.any_of):
        missing.append("none of " + ", ".join(repr(p) for p in check.any_of))
    missing.extend(repr(p) for p in check.all_of if _norm(p) not in folded)
    if check.pattern and not re.search(check.pattern, text, re.IGNORECASE | re.DOTALL):
        missing.append(f"the pattern {check.pattern!r}")
    if missing:
        return False, f"missing {'; '.join(missing)} in {_quote(text, 120)}"
    return True, f"said {_quote(text, 120)}"


def _never_says(trial: Trial, check: NeverSays) -> tuple[bool, str]:
    text = _seen(trial, check.step)
    folded = _norm(text)
    for phrase in check.any_of:
        if _norm(phrase) in folded:
            return False, f"said {phrase!r}"
    if check.pattern and re.search(check.pattern, text, re.IGNORECASE | re.DOTALL):
        return False, f"said something matching {check.pattern!r}"
    return True, "never said it"


def _memory_has(trial: Trial, check: MemoryHas) -> tuple[bool, str]:
    root = (trial.home / "memory").resolve()
    found = []
    for path in sorted(root.glob(check.path)):
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
            found.append(path)
    if not found:
        return False, f"no file matches {check.path}"
    if not check.contains:
        return True, f"found {', '.join(str(p.relative_to(root)) for p in found[:3])}"
    wanted = [_norm(s) for s in check.contains]
    for path in found:
        text = _norm(path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS])
        if all(s in text for s in wanted):
            return True, f"{path.relative_to(root)} says it"
    return False, f"{len(found)} file(s) match {check.path}, none contains {check.contains}"


def _memory_untouched(trial: Trial, check: MemoryUntouched) -> tuple[bool, str]:
    root = trial.home / "memory"
    for rel in check.paths:
        file = root / rel
        if not file.is_file():
            return False, f"{rel} is gone"
        if file.read_bytes() != trial.seeded[rel].encode("utf-8"):
            return False, f"{rel} was changed"
    return True, "left as the owner wrote it"


def _one(trial: Trial, check: Check) -> tuple[bool, str]:
    match check:
        case Silent():
            said = trial.step(check.step).messages
            if not said:
                return True, "said nothing"
            return False, f"said {len(said)} message(s), the first: {_quote(said[0].text)}"
        case Says():
            return _says(trial, check)
        case NeverSays():
            return _never_says(trial, check)
        case MaxMessages():
            count = len(_messages(trial, check.step))
            return count <= check.limit, f"{count} message(s); at most {check.limit} allowed"
        case NotUrgent():
            urgent = [m for m in _messages(trial, check.step) if m.urgency == "urgent"]
            if urgent:
                return False, f"sent an urgent message: {_quote(urgent[0].text)}"
            return True, "nothing urgent"
        case ToolsUsed():
            missing = [t for t in check.tools if t not in _tools(trial, check.step)]
            return not missing, ("never called " + ", ".join(missing)) if missing else "all called"
        case ToolsNotUsed():
            hit = [t for t in check.tools if t in _tools(trial, check.step)]
            return not hit, ("called " + ", ".join(hit)) if hit else "none called"
        case OnlyTools():
            extra = sorted(_tools(trial, check.step) - set(check.allowed))
            return not extra, ("also called " + ", ".join(extra)) if extra else "only the allowed"
        case Profile():
            runs = trial.step(check.step).runs
            profiles = sorted({r.profile for r in runs})
            if profiles == [check.expect]:
                return True, f"ran as {check.expect!r}"
            return False, f"ran as {profiles or 'nothing'}, expected {check.expect!r}"
        case MemoryHas():
            return _memory_has(trial, check)
        case MemoryUntouched():
            return _memory_untouched(trial, check)
        case SpendUnder():
            return trial.cost_usd < check.usd, f"spent ${trial.cost_usd:.4f} of ${check.usd:.4f}"
    raise AssertionError(f"no judge for {check!r}")  # pragma: no cover


def _where(check: Check) -> str:
    step = getattr(check, "step", None)
    return f"in `{step}`" if step else "across all steps"


def describe(check: Check) -> str:
    """A check in plain words (for `pa eval show`)."""
    match check:
        case Silent():
            return f"in `{check.step}` the agent says nothing"
        case Says():
            parts = []
            if check.any_of:
                parts.append("one of " + ", ".join(repr(p) for p in check.any_of))
            if check.all_of:
                parts.append("all of " + ", ".join(repr(p) for p in check.all_of))
            if check.pattern:
                parts.append(f"something matching /{check.pattern}/")
            return f"{_where(check)} the agent says " + " and ".join(parts)
        case NeverSays():
            parts = [repr(p) for p in check.any_of]
            if check.pattern:
                parts.append(f"anything matching /{check.pattern}/")
            return f"{_where(check)} the agent never says " + ", ".join(parts)
        case MaxMessages():
            return f"{_where(check)} at most {check.limit} message(s) are sent"
        case NotUrgent():
            return f"{_where(check)} nothing is sent as urgent"
        case ToolsUsed():
            return f"{_where(check)} the agent calls " + ", ".join(check.tools)
        case ToolsNotUsed():
            return f"{_where(check)} the agent calls none of " + ", ".join(check.tools)
        case OnlyTools():
            return f"{_where(check)} the agent calls nothing but " + (
                ", ".join(check.allowed) or "(nothing)"
            )
        case Profile():
            return f"`{check.step}` runs with the {check.expect!r} profile"
        case MemoryHas():
            more = (
                f" that says {', '.join(repr(c) for c in check.contains)}" if check.contains else ""
            )
            return f"memory has a file matching `{check.path}`{more}"
        case MemoryUntouched():
            return "the owner's files stay exactly as written: " + ", ".join(check.paths)
        case SpendUnder():
            return f"the whole scenario costs less than ${check.usd}"
    raise AssertionError(f"no description for {check!r}")  # pragma: no cover


def _ends(trial: Trial, step: Step) -> Verdict:
    name = f"ends-{step.id}"
    result = trial.step(step.id)
    why = f" ({result.failures[0]})" if result.failures else ""
    if not result.runs:
        return Verdict(name, False, f"no run happened{why}")
    bad = [r.status for r in result.runs if r.status not in step.ends]
    if bad:
        return Verdict(name, False, f"ended as {bad[0]!r}; allowed: {', '.join(step.ends)}{why}")
    return Verdict(name, True, "ended as " + ", ".join(sorted({r.status for r in result.runs})))


def judge(trial: Trial) -> list[Verdict]:
    """Every verdict for a trial: the implicit ends of each step, then the scenario's checks."""
    scenario = trial.scenario
    verdicts = [_ends(trial, step) for step in scenario.steps]
    for name, check in scenario.labelled_checks():
        ok, detail = _one(trial, check)
        verdicts.append(Verdict(name, ok, detail))
    return verdicts
