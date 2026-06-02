"""Dispatcher — the orchestrator worker thread.

Polls the TaskQueue and executes the oldest queued task when Dispatch is
enabled. Executors per `kind` are registered via `register(kind, fn)`; until
the AI executors are wired (P3.10+), unknown kinds fall back to a stub that
marks the task done with a placeholder so the end-to-end plumbing is testable.

Decoupled from TelegramBot via injected callables:
  report(chat_id, text)  — surface progress/result to the user
  is_enabled() -> bool   — read the Dispatch ON/OFF toggle from state
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from agent.orchestrator.queue import Task, TaskQueue

logger = logging.getLogger("orchestrator.dispatcher")

# An executor takes the Task and returns the result string. It may raise — the
# dispatcher catches and marks the task failed.
Executor = Callable[[Task], str]


class Dispatcher:
    def __init__(
        self,
        queue: TaskQueue,
        is_enabled: Callable[[], bool],
        report: Callable[[int, str], None],
        poll_interval: float = 2.0,
    ):
        self._q = queue
        self._is_enabled = is_enabled
        self._report = report
        self._poll = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._executors: dict[str, Executor] = {}

    def register(self, kind: str, fn: Executor) -> None:
        self._executors[kind] = fn

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dispatcher", daemon=True)
        self._thread.start()
        logger.info("dispatcher started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("dispatcher stopped")

    # ── internals ──────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._is_enabled():
                    self._stop.wait(self._poll)
                    continue
                task = self._q.claim_next()
                if task is None:
                    self._stop.wait(self._poll)
                    continue
                self._run_task(task)
            except Exception as e:
                logger.error("dispatcher loop error: %s", e)
                self._stop.wait(self._poll)

    def _run_task(self, task: Task) -> None:
        logger.info("running task #%d (%s)", task.id, task.kind)
        self._safe_report(task.chat_id, f"🔄 Задача #{task.id} ({task.kind}) выполняется…")
        started = time.time()
        executor = self._executors.get(task.kind)
        try:
            if executor is None:
                # Foundation stub: no AI wired yet. Marks the plumbing healthy.
                time.sleep(1.0)
                result = f"(оркестратор: исполнитель для '{task.kind}' ещё не подключён)"
                self._q.record_run(task.id, "stub", duration_s=time.time() - started, ok=True)
            else:
                result = executor(task)
            self._q.mark_done(task.id, result)
            self._safe_report(task.chat_id, f"✅ Задача #{task.id} готова:\n{result}")
        except Exception as e:
            logger.error("task #%d failed: %s", task.id, e)
            self._q.record_run(task.id, task.kind if executor else "stub",
                               duration_s=time.time() - started, ok=False)
            self._q.mark_failed(task.id, str(e))
            self._safe_report(task.chat_id, f"❌ Задача #{task.id} упала: {e}")

    def _safe_report(self, chat_id: int, text: str) -> None:
        try:
            self._report(chat_id, text)
        except Exception as e:
            logger.warning("report failed for task chat %s: %s", chat_id, e)
