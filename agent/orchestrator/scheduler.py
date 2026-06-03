"""Lightweight recurring-task scheduler — no external dependency.

APScheduler would work but pulls a dependency that must also be bundled into the
frozen exe. For this workload a tiny thread is plenty.

Schedule specs (stored in tasks.cron):
  "every <N>m" / "every <N>h"  — fixed interval from now
  "daily HH:MM"                 — once per day at local time

Template rows live in `tasks` with status='scheduled' and are NEVER executed
directly. A background thread wakes every CHECK_SECS; when a template is due it
enqueues a fresh queued copy (kind re-classified from the prompt) and recomputes
the next fire time.
"""
from __future__ import annotations

import datetime
import logging
import threading
from typing import Callable, Optional

from agent.orchestrator.queue import TaskQueue

logger = logging.getLogger("orchestrator.scheduler")

CHECK_SECS = 30


def next_fire(spec: str, after_ts: float) -> Optional[float]:
    """Return the next fire time (epoch seconds) strictly after `after_ts`, or
    None if the spec is invalid."""
    s = (spec or "").strip().lower()
    after = datetime.datetime.fromtimestamp(after_ts)

    if s.startswith("every "):
        body = s[6:].strip()
        try:
            if body.endswith("m"):
                secs = int(body[:-1]) * 60
            elif body.endswith("h"):
                secs = int(body[:-1]) * 3600
            else:
                return None
            if secs <= 0:
                return None
            return after_ts + secs
        except ValueError:
            return None

    if s.startswith("daily "):
        hhmm = s[6:].strip()
        try:
            hh, mm = hhmm.split(":")
            hh, mm = int(hh), int(mm)
            if not (0 <= hh < 24 and 0 <= mm < 60):
                return None
        except ValueError:
            return None
        candidate = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= after:
            candidate += datetime.timedelta(days=1)
        return candidate.timestamp()

    return None


def spec_is_valid(spec: str) -> bool:
    import time as _t
    return next_fire(spec, _t.time()) is not None


class Scheduler:
    def __init__(self, queue: TaskQueue, classify: Callable[[str], str],
                 poll: float = CHECK_SECS):
        self._q = queue
        self._classify = classify
        self._poll = poll
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next: dict[int, float] = {}  # template task id -> next fire ts
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.reload()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        logger.info("scheduler started (%d templates)", len(self._next))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def reload(self) -> None:
        """Re-read scheduled templates and (re)compute next-fire for any new ones.
        Call after a template is added or removed."""
        import time as _t
        now = _t.time()
        templates = self._q.list(status="scheduled", limit=200)
        live_ids = set()
        with self._lock:
            for t in templates:
                live_ids.add(t.id)
                if t.id not in self._next:
                    nf = next_fire(t.cron or "", now)
                    if nf is not None:
                        self._next[t.id] = nf
                    else:
                        logger.warning("template #%d has invalid spec %r", t.id, t.cron)
            # Drop templates that no longer exist (cancelled/deleted).
            for stale in [tid for tid in self._next if tid not in live_ids]:
                self._next.pop(stale, None)

    def _loop(self) -> None:
        import time as _t
        while not self._stop.is_set():
            try:
                now = _t.time()
                due: list[int] = []
                with self._lock:
                    for tid, fire_at in self._next.items():
                        if fire_at <= now:
                            due.append(tid)
                for tid in due:
                    self._fire(tid, now)
            except Exception as e:
                logger.error("scheduler loop error: %s", e)
            self._stop.wait(self._poll)

    def _fire(self, template_id: int, now: float) -> None:
        t = self._q.get(template_id)
        if not t or t.status != "scheduled":
            # Template vanished or was cancelled — stop tracking it.
            with self._lock:
                self._next.pop(template_id, None)
            return
        kind = self._classify(t.prompt)
        new_id = self._q.enqueue(t.chat_id, kind, t.prompt)
        logger.info("scheduled #%d fired -> queued #%d (%s)", template_id, new_id, kind)
        nf = next_fire(t.cron or "", now)
        with self._lock:
            if nf is not None:
                self._next[template_id] = nf
            else:
                self._next.pop(template_id, None)
