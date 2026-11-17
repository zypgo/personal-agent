"""Spending limits: a hard cap per run and per day.

A runaway loop or a flood of events must cost a bounded amount of money. The per-run cap stops one
run (failed, the owner is told, not retried); the daily cap pauses all work until the next local
midnight (events wait in the inbox, nothing is lost). The owner can lift today's cap by hand.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import tzinfo

from personal_agent.clock import Clock, SystemClock, start_of_day, start_of_next_day, to_local
from personal_agent.config import BudgetConfig
from personal_agent.core.runs import RunStore
from personal_agent.db import Database


class BudgetExceeded(Exception):
    """A spending limit was reached; the run stops (per-run) or all work pauses (daily)."""

    def __init__(self, scope: str, spent: float, limit: float, resets_in_s: float = 0.0) -> None:
        super().__init__(f"{scope} budget reached: ${spent:.2f} of ${limit:.2f}")
        self.scope = scope
        self.spent = spent
        self.limit = limit
        self.resets_in_s = resets_in_s


@dataclass(frozen=True)
class BudgetStatus:
    spent_today: float
    daily_limit: float
    extra_today: float
    per_run_limit: float
    resets_in_s: float

    @property
    def remaining_today(self) -> float:
        return max(0.0, self.daily_limit - self.spent_today)


class BudgetGuard:
    def __init__(
        self, runs: RunStore, db: Database, cfg: BudgetConfig, *, clock: Clock | None, tz: tzinfo
    ) -> None:
        self.runs = runs
        self.db = db
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.tz = tz
        self.instance_guard: Callable[[], None] | None = None

    # ---- today ------------------------------------------------------------------------
    def _day_key(self) -> str:
        return f"budget_extra:{to_local(self.clock.now(), self.tz):%Y-%m-%d}"

    def extra_today(self) -> float:
        try:
            return float(self.db.kv_get(self._day_key(), "0") or 0)
        except ValueError:
            return 0.0

    def extend_today(self, amount: float) -> float:
        """Raise today's cap by `amount` dollars. Returns today's new limit."""
        if amount <= 0:
            raise ValueError("the extension must be positive")
        self.db.kv_set(self._day_key(), repr(self.extra_today() + amount), now=self.clock.now())
        return self.daily_limit()

    def daily_limit(self) -> float:
        return self.cfg.daily_usd + self.extra_today()

    def spent_today(self) -> float:
        since = start_of_day(self.clock.now(), self.tz)
        return self.runs.cost_since(since) + self.spend_since(since)

    # ---- money spent outside of runs (speech-to-text) ----------------------------------
    def record_spend(self, kind: str, usd: float, detail: str = "") -> None:
        """Count money that no run paid for, so the daily cap sees it."""
        self.db.execute(
            "INSERT INTO spend(at, kind, usd, detail) VALUES (?, ?, ?, ?)",
            (self.clock.now(), kind, max(0.0, float(usd)), detail[:200]),
        )

    def spend_since(self, since: float) -> float:
        return float(
            self.db.scalar("SELECT SUM(usd) FROM spend WHERE at >= ?", (since,), default=0.0)
        )

    def resets_in(self) -> float:
        now = self.clock.now()
        return max(1.0, start_of_next_day(now, self.tz) - now)

    def status(self) -> BudgetStatus:
        return BudgetStatus(
            spent_today=self.spent_today(),
            daily_limit=self.daily_limit(),
            extra_today=self.extra_today(),
            per_run_limit=self.cfg.per_run_usd,
            resets_in_s=self.resets_in(),
        )

    # ---- checks -----------------------------------------------------------------------
    def check_day(self) -> None:
        if self.instance_guard is not None:
            self.instance_guard()
        spent, limit = self.spent_today(), self.daily_limit()
        if spent >= limit:
            raise BudgetExceeded("daily", spent, limit, self.resets_in())

    def check_run(self, run_id: str, limit: float | None = None) -> None:
        """Raise if this run has used up its allowance: `limit` (a smaller one for work that
        should stay cheap) or the configured per-run cap, whichever is lower."""
        cap = self.cfg.per_run_usd if limit is None else min(limit, self.cfg.per_run_usd)
        run = self.runs.get(run_id)
        if run is not None and run.cost_usd >= cap:
            raise BudgetExceeded("run", run.cost_usd, cap)
        self.check_day()
