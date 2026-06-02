"""SQLite-backed task queue for the Dispatch orchestrator.

Two tables:
  tasks — one row per requested task (queued → running → done/failed/…)
  runs  — one row per model invocation against a task (for cost/audit)

Thread-safe: the Telegram bot thread enqueues while the Dispatcher worker
thread claims/updates. A single connection (check_same_thread=False) guarded by
one lock is plenty for this workload (a handful of tasks/min).

Status lifecycle:
  queued    — waiting for the dispatcher
  running   — claimed, being executed
  done      — finished OK (result populated)
  failed    — finished with error (result holds the error)
  cancelled — user cancelled before/while running
  scheduled — template row for a cron task (never executed directly; the
              scheduler enqueues fresh copies). See P3.14.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent.paths import DATA_DIR

DB_PATH = DATA_DIR / "tasks.db"

# Terminal states a task can't leave.
_TERMINAL = {"done", "failed", "cancelled"}


@dataclass
class Task:
    id: int
    chat_id: int
    kind: str
    prompt: str
    status: str
    created_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    result: Optional[str]
    cron: Optional[str]

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            id=row["id"], chat_id=row["chat_id"], kind=row["kind"],
            prompt=row["prompt"], status=row["status"],
            created_at=row["created_at"], started_at=row["started_at"],
            finished_at=row["finished_at"], result=row["result"], cron=row["cron"],
        )


class TaskQueue:
    def __init__(self, db_path: Path | str = DB_PATH):
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._recover_orphans()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     INTEGER NOT NULL,
                    kind        TEXT    NOT NULL,
                    prompt      TEXT    NOT NULL,
                    status      TEXT    NOT NULL DEFAULT 'queued',
                    created_at  REAL    NOT NULL,
                    started_at  REAL,
                    finished_at REAL,
                    result      TEXT,
                    cron        TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id     INTEGER NOT NULL REFERENCES tasks(id),
                    model       TEXT,
                    tokens_in   INTEGER,
                    tokens_out  INTEGER,
                    cost_usd    REAL,
                    duration_s  REAL,
                    ok          INTEGER,
                    created_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_queued ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
                """
            )
            self._conn.commit()

    def _recover_orphans(self) -> None:
        """A crash/restart can leave tasks stuck in 'running'. Requeue them so
        they aren't lost (idempotent executors assumed; for non-idempotent work
        the user can cancel)."""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status='queued', started_at=NULL "
                "WHERE status='running'"
            )
            self._conn.commit()

    # ── write ────────────────────────────────────────────────────────────────

    def enqueue(self, chat_id: int, kind: str, prompt: str,
                cron: Optional[str] = None) -> int:
        status = "scheduled" if cron else "queued"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks (chat_id, kind, prompt, status, created_at, cron) "
                "VALUES (?,?,?,?,?,?)",
                (chat_id, kind, prompt, status, time.time(), cron),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def claim_next(self) -> Optional[Task]:
        """Atomically pick the oldest queued task and mark it running."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE status='queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE tasks SET status='running', started_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            self._conn.commit()
            task = Task._from_row(row)
            task.status = "running"
            return task

    def mark_done(self, task_id: int, result: str) -> None:
        self._finish(task_id, "done", result)

    def mark_failed(self, task_id: int, error: str) -> None:
        self._finish(task_id, "failed", error)

    def _finish(self, task_id: int, status: str, result: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, finished_at=?, result=? WHERE id=?",
                (status, time.time(), result, task_id),
            )
            self._conn.commit()

    def cancel(self, task_id: int) -> bool:
        """Cancel a task unless it already reached a terminal state. Returns
        True if it was cancelled."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row or row["status"] in _TERMINAL:
                return False
            self._conn.execute(
                "UPDATE tasks SET status='cancelled', finished_at=? WHERE id=?",
                (time.time(), task_id),
            )
            self._conn.commit()
            return True

    def record_run(self, task_id: int, model: str, *, tokens_in: int = 0,
                   tokens_out: int = 0, cost_usd: float = 0.0,
                   duration_s: float = 0.0, ok: bool = True) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (task_id, model, tokens_in, tokens_out, "
                "cost_usd, duration_s, ok, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, model, tokens_in, tokens_out, cost_usd, duration_s,
                 int(ok), time.time()),
            )
            self._conn.commit()

    # ── read ─────────────────────────────────────────────────────────────────

    def get(self, task_id: int) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return Task._from_row(row) if row else None

    def list(self, status: Optional[str] = None, limit: int = 20) -> list[Task]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [Task._from_row(r) for r in rows]

    def cost_since(self, since_ts: float) -> float:
        """Total cost_usd of runs created at/after since_ts. Used by budget.py."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS c FROM runs WHERE created_at >= ?",
                (since_ts,),
            ).fetchone()
            return float(row["c"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
