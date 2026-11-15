"""The results of a run, as JSON (for tools) and Markdown (for people).

Both are saved in the owner's own folder (`PA_HOME/evals/reports`, private). Everything that came
from the agent or from a scenario is scrubbed of secrets and shortened first: a report is the kind of
file that ends up pasted into an issue.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from personal_agent.evals.runner import CheckedScenario, LiveRun, TrialResult
from personal_agent.paths import Paths
from personal_agent.security.redact import known_secret_values, redact

SHOWN_CHARS = 400
"""How much of a message a report shows; the full text is in the throwaway home that is deleted."""


def reports_dir(paths: Paths) -> Path:
    return paths.home / "evals" / "reports"


def _clean(text: str) -> str:
    scrubbed = redact(text, known_secret_values(os.environ))
    return scrubbed if len(scrubbed) <= SHOWN_CHARS else scrubbed[: SHOWN_CHARS - 1] + "…"


def noise_of(trials: list[TrialResult]) -> dict[str, Any]:
    """How often the agent spoke when it was asked to stay quiet: messages per quiet step."""
    quiet = sum(t.quiet_steps for t in trials)
    spoken = sum(t.noise for t in trials)
    return {
        "quiet_steps": quiet,
        "messages": spoken,
        "per_quiet_step": round(spoken / quiet, 3) if quiet else None,
    }


# ---- live --------------------------------------------------------------------------------------------
def _trial_dict(trial: TrialResult) -> dict[str, Any]:
    return {
        "index": trial.index,
        "ok": trial.ok,
        "error": _clean(trial.error) if trial.error else None,
        "cost_usd": round(trial.cost_usd, 6),
        "requests": trial.requests,
        "checks": [{"id": v.id, "ok": v.ok, "detail": _clean(v.detail)} for v in trial.verdicts],
        "steps": [
            {
                "id": s.id,
                "statuses": s.statuses,
                "tools": s.tools,
                "messages": [{"urgency": u, "text": _clean(t)} for u, t in s.messages],
            }
            for s in trial.steps
        ],
    }


def live_dict(run: LiveRun, *, when: datetime) -> dict[str, Any]:
    trials = [t for r in run.results for t in r.trials]
    return {
        "mode": "live",
        "when": when.isoformat(timespec="seconds"),
        "model": run.model,
        "effort": run.effort,
        "trials_per_scenario": run.trials,
        "ok": run.ok,
        "cost_usd": round(run.cost_usd, 6),
        "cap_usd": run.cap_usd,
        "noise": noise_of(trials),
        "scenarios": [
            {
                "id": r.scenario_id,
                "title": r.title,
                "passed": r.passed,
                "ran": len(r.trials),
                "skipped": r.skipped,
                "all_passed": r.all_passed,
                "trials": [_trial_dict(t) for t in r.trials],
            }
            for r in run.results
        ],
    }


def live_markdown(data: dict[str, Any]) -> str:
    noise = data["noise"]
    lines = [
        f"# Live evaluation, {data['when']}",
        "",
        f"- Model: `{data['model']}` (reasoning effort `{data['effort']}`)",
        f"- Trials per scenario: {data['trials_per_scenario']}",
        f"- Spent: ${data['cost_usd']:.4f} of the ${data['cap_usd']:.2f} ceiling",
        "- Noise: "
        + (
            f"{noise['messages']} message(s) in {noise['quiet_steps']} step(s) that asked for "
            f"silence ({noise['per_quiet_step']} per quiet step)"
            if noise["quiet_steps"]
            else "no scenario here asks for silence"
        ),
        f"- Result: **{'all scenarios held every time' if data['ok'] else 'something did not hold'}**",
        "",
        "| Scenario | Passed | Every time |",
        "|---|---|---|",
    ]
    for s in data["scenarios"]:
        note = f", {s['skipped']} not run (ceiling reached)" if s["skipped"] else ""
        lines.append(
            f"| `{s['id']}` | {s['passed']}/{s['ran']}{note} | {'yes' if s['all_passed'] else 'no'} |"
        )
    for s in data["scenarios"]:
        broken = [t for t in s["trials"] if not t["ok"]]
        if not broken:
            continue
        lines += ["", f"## {s['id']}: {s['title']}"]
        for t in broken:
            lines.append(
                f"\n**Trial {t['index'] + 1}**"
                + (f" could not be played: {t['error']}" if t["error"] else "")
            )
            for c in t["checks"]:
                if not c["ok"]:
                    lines.append(f"- `{c['id']}` failed: {c['detail']}")
            for step in t["steps"]:
                said = (
                    "; ".join(f"[{m['urgency']}] {m['text']}" for m in step["messages"])
                    or "(nothing)"
                )
                lines.append(
                    f"  - step `{step['id']}` {step['statuses']}, tools {step['tools']}: {said}"
                )
    return "\n".join(lines) + "\n"


# ---- scripted ----------------------------------------------------------------------------------------
def checked_dict(checked: list[CheckedScenario], *, when: datetime) -> dict[str, Any]:
    def status(c: CheckedScenario) -> str:
        return "unscripted" if c.unscripted else ("ok" if c.ok else "failed")

    return {
        "mode": "scripted",
        "when": when.isoformat(timespec="seconds"),
        "ok": all(c.ok for c in checked),
        "scenarios": [
            {
                "id": c.scenario_id,
                "title": c.title,
                "status": status(c),
                "good": None
                if c.good is None
                else {
                    "ok": c.good.ok,
                    "error": _clean(c.good.error) if c.good.error else None,
                    "failed": [{"id": v.id, "detail": _clean(v.detail)} for v in c.good.failed],
                },
                "bad": [
                    {
                        "name": b.name,
                        "expected": b.expected,
                        "failed": b.failed,
                        "caught": b.caught,
                        "error": _clean(b.error) if b.error else None,
                    }
                    for b in c.bad
                ],
            }
            for c in checked
        ],
    }


def checked_markdown(data: dict[str, Any]) -> str:
    lines = [
        f"# Scenario self-check, {data['when']}",
        "",
        "A scripted model plays each scenario's good run and its bad runs. The good run must pass "
        "every check and each bad run must fail the checks it is meant to: otherwise the checks "
        "could not tell a good agent from a bad one.",
        "",
        "| Scenario | Result | Bad runs caught |",
        "|---|---|---|",
    ]
    for s in data["scenarios"]:
        caught = sum(b["caught"] for b in s["bad"])
        lines.append(f"| `{s['id']}` | {s['status']} | {caught}/{len(s['bad'])} |")
    for s in data["scenarios"]:
        if s["status"] != "failed":
            continue
        lines += ["", f"## {s['id']}"]
        good = s["good"]
        if good and (good["error"] or good["failed"]):
            lines.append(f"- the good run did not pass: {good['error'] or ''}")
            lines += [f"  - `{f['id']}`: {f['detail']}" for f in good["failed"]]
        for b in s["bad"]:
            if not b["caught"]:
                lines.append(
                    f"- the bad run `{b['name']}` should fail {b['expected']} but failed "
                    f"{b['failed'] or 'nothing'}" + (f" ({b['error']})" if b["error"] else "")
                )
    return "\n".join(lines) + "\n"


# ---- files ---------------------------------------------------------------------------------------------
def save(paths: Paths, data: dict[str, Any], markdown: str, *, when: datetime) -> tuple[Path, Path]:
    """Write `<time>-<mode>.json` and `.md`, readable by the owner only."""
    folder = reports_dir(paths)
    folder.mkdir(parents=True, exist_ok=True)
    for parent in (folder.parent, folder):
        try:
            parent.chmod(0o700)
        except OSError:
            pass
    base = f"{when.strftime('%Y%m%d-%H%M%S')}-{data['mode']}"
    stem, counter = base, 1
    while any((folder / f"{stem}{suffix}").exists() for suffix in (".json", ".md")):
        counter += 1  # two runs in the same second must not overwrite each other
        stem = f"{base}-{counter}"
    written = []
    bodies = ((".json", json.dumps(data, indent=2, ensure_ascii=False) + "\n"), (".md", markdown))
    for suffix, body in bodies:
        fd = os.open(folder / f"{stem}{suffix}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        written.append(folder / f"{stem}{suffix}")
    return written[0], written[1]
