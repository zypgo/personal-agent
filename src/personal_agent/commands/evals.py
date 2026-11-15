"""`pa eval`: put the agent through the always-on scenarios and see whether it behaves.

Without `--live` a scripted model plays each scenario's good and bad runs to prove that the checks
can tell them apart: free, offline, and the first thing to run after editing a scenario. With
`--live` the real model plays the scenarios, several times each, against a throwaway home, and the
run stops when the spending ceiling is reached.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, get_args

import typer
from rich.markup import escape

from personal_agent.commands.common import USAGE, fail, home, say, warn
from personal_agent.config import ConfigError, ReasoningEffort, api_key, load_config
from personal_agent.evals import report as reports
from personal_agent.evals.checks import describe
from personal_agent.evals.spec import Catalog, Scenario, load_catalog
from personal_agent.paths import Paths

if TYPE_CHECKING:
    from personal_agent.evals.runner import CheckedScenario, LiveRun, TrialResult

EFFORTS = get_args(ReasoningEffort)

app = typer.Typer(
    help="Test the agent's always-on behaviour: quiet days, news once, hostile input, memory.",
    invoke_without_command=True,
)

Tags = Annotated[list[str] | None, typer.Option("--tag", help="Only this tag.")]


def _catalog(paths: Paths) -> Catalog:
    found = load_catalog(paths)
    for problem in found.problems:
        warn(f"skipped: {problem}")
    return found


def _select(found: Catalog, ids: list[str] | None, tags: list[str] | None) -> list[Scenario]:
    chosen = list(found)
    if ids:
        unknown = [i for i in ids if i not in found.scenarios]
        if unknown:
            known = ", ".join(sorted(found.scenarios))
            fail(f"No scenario named {unknown[0]!r}. Known: {known}", code=USAGE)
        chosen = [found.scenarios[i] for i in dict.fromkeys(ids)]
    if tags:
        chosen = [s for s in chosen if any(t in s.tags for t in tags)]
    if not chosen:
        fail("No scenario matches. `pa eval list` shows what there is.", code=USAGE)
    return chosen


# ---- list and show ------------------------------------------------------------------------
@app.callback()
def _default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        list_scenarios()


@app.command("list")
def list_scenarios(tag: Tags = None) -> None:
    """The scenarios there are: the built-in ones and your own (PA_HOME/evals/*.yaml)."""
    found = _catalog(Paths.resolve())
    shown = [s for s in found if not tag or any(t in s.tags for t in tag)]
    if not shown:
        say("[dim]no scenarios[/dim]")
    for s in shown:
        tags = f" [dim]\\[{', '.join(s.tags)}][/dim]" if s.tags else ""
        say(f"[bold]{s.id}[/bold]{tags}  [dim]({found.origin[s.id]})[/dim]")
        say(f"    {escape(s.title)}")


@app.command("show")
def show(scenario_id: str) -> None:
    """What a scenario does, step by step, and what it demands of the agent."""
    scenario = _select(_catalog(Paths.resolve()), [scenario_id], None)[0]
    say(f"[bold]{scenario.id}[/bold]  {escape(scenario.title)}")
    if scenario.about:
        say(escape(" ".join(scenario.about.split())))
    seed = scenario.seed
    for label, items in (
        ("preferences", list(seed.preferences)),
        ("memory", list(seed.memory)),
        ("tasks", list(seed.tasks)),
        ("workspace files", list(seed.files)),
    ):
        if items:
            say(f"[dim]starts with {label}:[/dim] {escape(', '.join(items))}")
    for text in seed.sent:
        say(f"[dim]already told the owner:[/dim] {escape(text)}")
    say("[bold]steps[/bold]")
    for step in scenario.steps:
        what = {
            "message": lambda s=step: f"the owner writes (in {s.session_key}): {s.message}",
            "timer": lambda s=step: f"schedule {s.timer.name} fires: {s.timer.prompt}",
            "webhook": lambda s=step: f"webhook {s.webhook.name} arrives",
        }[step.kind]()
        say(f"  {step.id}: {escape(' '.join(what.split()))}")
    say("[bold]checks[/bold]")
    for name, check in scenario.labelled_checks():
        say(f"  {name}: {escape(describe(check))}")
    if scenario.mock:
        say(f"[dim]scripted: a good run and {len(scenario.mock.bad)} bad run(s)[/dim]")
    else:
        say("[dim]no scripts: its checks have not been shown to be able to fail[/dim]")


# ---- run ----------------------------------------------------------------------------------
def _print_scripted(results: list[CheckedScenario], verbose: bool) -> None:
    for c in results:
        if c.unscripted:
            say(f"[dim]--  {c.scenario_id}  has no scripts, so its checks were not tested[/dim]")
            continue
        caught = sum(b.caught for b in c.bad)
        head = (
            f"{c.scenario_id}  the good run {'passes' if c.good and c.good.ok else 'FAILS'}, "
            f"{caught}/{len(c.bad)} bad runs caught"
        )
        say(("[green]ok  [/green]" if c.ok else "[red]FAIL[/red]") + f" {head}")
        if c.good and (verbose or not c.good.ok):
            if c.good.error:
                say(f"      [red]{escape(c.good.error)}[/red]")
            for v in c.good.verdicts:
                if verbose or not v.ok:
                    mark = "[green]ok[/green]" if v.ok else "[red]no[/red]"
                    say(f"      {mark} {v.id}: {escape(v.detail)}")
        for b in c.bad:
            if verbose or not b.caught:
                mark = "[green]caught[/green]" if b.caught else "[red]NOT caught[/red]"
                got = ", ".join(b.failed) or "nothing"
                extra = f" ({escape(b.error)})" if b.error else ""
                say(
                    f"      {mark} {b.name}: must fail {', '.join(b.expected)}; failed {got}{extra}"
                )


def _print_trial(scenario: Scenario, trial: TrialResult, trials: int) -> None:
    mark = "[green]pass[/green]" if trial.ok else "[red]FAIL[/red]"
    say(f"  {scenario.id}  trial {trial.index + 1}/{trials}  {mark}  ${trial.cost_usd:.4f}")


def _print_live(run: LiveRun) -> None:
    for r in run.results:
        note = f" ({r.skipped} not run: spending ceiling reached)" if r.skipped else ""
        mark = "[green]ok  [/green]" if r.all_passed else "[red]FAIL[/red]"
        say(f"{mark} {r.scenario_id}  {r.passed}/{len(r.trials)} trials passed{note}")
        shown = next((t for t in r.trials if not t.ok), None)
        if shown is None:
            continue
        if shown.error:
            say(f"      [red]{escape(shown.error)}[/red]")
        for v in shown.failed:
            say(f"      [red]no[/red] {v.id}: {escape(v.detail)}")
    say(f"Spent ${run.cost_usd:.4f} of the ${run.cap_usd:.2f} ceiling.")
    noise = reports.noise_of([t for r in run.results for t in r.trials])
    if noise["quiet_steps"]:
        say(
            f"Noise: {noise['messages']} message(s) in {noise['quiet_steps']} step(s) that "
            f"asked for silence ({noise['per_quiet_step']} per quiet step)."
        )


def _save(paths: Paths, data: dict[str, Any], markdown: str, when: datetime) -> None:
    if not paths.home.is_dir():
        say("[dim]No report saved: there is no home yet (`pa init`).[/dim]")
        return
    try:
        json_file, md_file = reports.save(paths, data, markdown, when=when)
    except OSError as exc:  # the run may have cost money: say so, do not end in a traceback
        warn(f"No report saved: {exc}")
        return
    say(f"Report: {md_file} (and {json_file.name})")


@app.command("run")
def run(
    ids: Annotated[list[str] | None, typer.Argument(help="Scenario ids; default: all.")] = None,
    tag: Tags = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Use the real model (costs money) instead of scripted runs."),
    ] = False,
    trials: Annotated[
        int,
        typer.Option(
            "--trials", "-n", min=1, max=10, help="Live: how often to play each scenario."
        ),
    ] = 1,
    max_cost: Annotated[
        float,
        typer.Option(
            "--max-cost", min=0.01, help="Live: stop when this many US dollars are spent."
        ),
    ] = 1.0,
    model: Annotated[
        str | None, typer.Option("--model", help="Live: the main model to use.")
    ] = None,
    effort: Annotated[
        str | None,
        typer.Option("--effort", help=f"Live: reasoning effort ({', '.join(EFFORTS)})."),
    ] = None,
    no_report: Annotated[
        bool, typer.Option("--no-report", help="Do not write report files.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show every check.")] = False,
) -> None:
    """Check the checks (default), or measure the real model with --live."""
    from personal_agent.evals.runner import check_scenario, run_live

    paths = Paths.resolve()
    chosen = _select(_catalog(paths), ids, tag)
    when = datetime.now().astimezone()
    if effort is not None and effort not in EFFORTS:
        fail(f"--effort is one of {', '.join(EFFORTS)}", code=USAGE)

    if not live:
        results = [asyncio.run(check_scenario(s)) for s in chosen]
        _print_scripted(results, verbose)
        data = reports.checked_dict(results, when=when)
        if not no_report:
            _save(paths, data, reports.checked_markdown(data), when)
        if not all(r.ok for r in results):
            raise typer.Exit(1)
        say("Every scenario tells its good run from its bad ones.")
        return

    home()  # the real model needs the agent's own home, key and config
    try:
        owner = load_config(paths)
    except ConfigError as exc:
        fail(str(exc), code=USAGE)
    if not api_key() and not (owner.model.base_url or os.environ.get("OPENAI_BASE_URL")):
        fail(
            f"OPENAI_API_KEY is not set. Put it in {paths.env_file}; nothing was spent.", code=USAGE
        )
    used = model or owner.model.main
    say(
        f"Live: {len(chosen)} scenario(s) x {trials} trial(s) on {escape(used)} "
        f"({effort or owner.model.reasoning_effort}); spending is capped at ${max_cost:.2f}."
    )
    outcome = asyncio.run(
        run_live(
            chosen,
            owner=owner,
            trials=trials,
            cap_usd=max_cost,
            model=model,
            effort=effort,
            on_trial=lambda s, t: _print_trial(s, t, trials),
        )
    )
    _print_live(outcome)
    data = reports.live_dict(outcome, when=when)
    if not no_report:
        _save(paths, data, reports.live_markdown(data), when)
    if not outcome.ok:
        raise typer.Exit(1)
