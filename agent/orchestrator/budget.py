"""Daily spend cap for the orchestrator.

Sums the cost of `runs` created since local midnight (via TaskQueue.cost_since)
and compares against a daily limit. The Dispatcher consults `allowed()` before
running a task; when the cap is hit, tasks stay queued until midnight rollover
or the user raises the limit.

Separate from the bot's monthly `state["spend"]` meter (which also counts chat
usage) — this one is orchestrator-only and per-day, so a runaway queue can't
burn the whole monthly budget in an hour.
"""
from __future__ import annotations

import datetime
import logging
from typing import Callable

from agent.orchestrator.queue import TaskQueue

logger = logging.getLogger("orchestrator.budget")

DEFAULT_DAILY_USD = 7.0


def _midnight_ts() -> float:
    today = datetime.date.today()
    return datetime.datetime(today.year, today.month, today.day).timestamp()


class Budget:
    def __init__(self, queue: TaskQueue, limit_provider: Callable[[], float]):
        """limit_provider returns the current daily limit (read live so config
        changes take effect without a restart)."""
        self._q = queue
        self._limit_provider = limit_provider

    def limit(self) -> float:
        try:
            v = float(self._limit_provider())
            return v if v > 0 else DEFAULT_DAILY_USD
        except Exception:
            return DEFAULT_DAILY_USD

    def today_spent(self) -> float:
        return self._q.cost_since(_midnight_ts())

    def remaining(self) -> float:
        return max(0.0, self.limit() - self.today_spent())

    def allowed(self) -> bool:
        """True if there is daily budget left to start another task."""
        return self.today_spent() < self.limit()

    def fraction(self) -> float:
        lim = self.limit()
        return (self.today_spent() / lim) if lim > 0 else 1.0
