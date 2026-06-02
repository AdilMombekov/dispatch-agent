"""Dispatch Agent watchdog — keeps main.py alive.

Runs forever, every 60 s checks if `pythonw.exe E:\\dispatch\\main.py` is alive.
If not — spawns it (detached). Logs to watchdog.log.

Install: add to HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run as
`AgentWatchdog`, pointing at `pythonw.exe "E:\\dispatch\\watchdog.py"`.
Then any agent crash gets healed within ~60 s.

Why not use self-restart only? The agent's internal self-restart uses
`cmd /c "timeout 3 && start /B ..."` which sometimes loses the child to a race
on Windows. Watchdog is the safety net.
"""
from __future__ import annotations
import os
import sys
import time
import subprocess
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MAIN_PY = BASE_DIR / "main.py"
LOG_PATH = BASE_DIR / "watchdog.log"

CHECK_INTERVAL = 60        # seconds between health checks
STARTUP_GRACE = 15         # after launching, give it this long before next check
FAILS_BEFORE_SPAWN = 2     # require this many consecutive "down" checks before respawning.
                           # Debounce: the agent's own self-restart (os._exit → ~3s → relaunch)
                           # leaves a brief window with no process. Without debounce the watchdog
                           # would spawn a *second* agent during that window → two getUpdates
                           # pollers on one token → "Conflict: terminated by other getUpdates".
MIN_RESTART_GAP = 300      # seconds: don't spawn again within this window of the last spawn.
                           # Stops a crash-looping agent from being respawned every minute and
                           # gives a self-restart time to complete on its own.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # Rotate at 5 MB, keep 3 old files. Watchdog logs little, so smaller caps.
        RotatingFileHandler(str(LOG_PATH), maxBytes=5 * 1024 * 1024,
                            backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")


def find_agent_processes() -> list[int]:
    """Return PIDs of pythonw/python processes whose command-line includes our main.py path."""
    needle = str(MAIN_PY).lower()
    try:
        # WMIC is deprecated but reliable; fall back to PowerShell if it disappears.
        out = subprocess.run(
            ["wmic", "process", "where",
             "name='pythonw.exe' or name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=0x08000000,
        ).stdout
    except Exception:
        out = ""
    pids: list[int] = []
    for line in out.splitlines():
        if "main.py" not in line.lower() or needle not in line.lower():
            continue
        # CSV: Node,CommandLine,ProcessId
        parts = line.rsplit(",", 1)
        if len(parts) != 2:
            continue
        try:
            pids.append(int(parts[1].strip()))
        except ValueError:
            pass
    if pids:
        return pids
    # WMIC empty / disabled — fall back to PowerShell Get-CimInstance.
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter "
            "\"Name = 'pythonw.exe' OR Name = 'python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*main.py*' -and "
            "$_.CommandLine -like '*dispatch*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        ).stdout
        for tok in out.split():
            try:
                pids.append(int(tok))
            except ValueError:
                pass
    except Exception as e:
        log.warning("PowerShell fallback failed: %s", e)
    return pids


def spawn_agent() -> int | None:
    """Launch pythonw E:\\dispatch\\main.py detached. Returns PID or None."""
    py_path = Path(sys.executable)
    pyw = py_path.with_name("pythonw.exe")
    py = str(pyw if pyw.exists() else sys.executable)
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    try:
        proc = subprocess.Popen(
            [py, str(MAIN_PY)],
            cwd=str(BASE_DIR),
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            close_fds=True,
        )
        return proc.pid
    except Exception as e:
        log.error("spawn failed: %s", e)
        return None


def main():
    log.info("watchdog starting · main=%s · interval=%ds · debounce=%d · cooldown=%ds",
             MAIN_PY, CHECK_INTERVAL, FAILS_BEFORE_SPAWN, MIN_RESTART_GAP)
    consecutive_failures = 0
    last_spawn = 0.0
    while True:
        try:
            pids = find_agent_processes()
            if pids:
                if consecutive_failures:
                    log.info("agent back · pids=%s (after %d down checks)", pids, consecutive_failures)
                consecutive_failures = 0
                # quiet success — only log every 10 minutes to keep log small
                # (each interval is 60s; every 10th is 10 min)
                # tracked via process attribute on the function for simplicity
                main._counter = (getattr(main, "_counter", 0) + 1) % 10
                if main._counter == 0:
                    log.info("ok · agent alive · pids=%s", pids)
            else:
                consecutive_failures += 1
                # Debounce: ignore a single transient miss (likely a self-restart window).
                if consecutive_failures < FAILS_BEFORE_SPAWN:
                    log.warning("agent not found (check #%d/%d) — waiting before acting",
                                consecutive_failures, FAILS_BEFORE_SPAWN)
                    time.sleep(CHECK_INTERVAL)
                    continue
                # Cooldown: don't fight a self-restart or hammer a crash-looping agent.
                since = time.time() - last_spawn
                if since < MIN_RESTART_GAP:
                    log.warning("agent down (#%d) but last spawn was %.0fs ago (<%ds) — holding off",
                                consecutive_failures, since, MIN_RESTART_GAP)
                    time.sleep(CHECK_INTERVAL)
                    continue
                log.warning("agent down (#%d consecutive) — spawning fresh process",
                            consecutive_failures)
                pid = spawn_agent()
                last_spawn = time.time()
                if pid:
                    log.info("spawned pid=%d, waiting %ds grace", pid, STARTUP_GRACE)
                    consecutive_failures = 0
                    time.sleep(STARTUP_GRACE)
                    continue
                log.error("spawn failed — will retry after cooldown (%ds)", MIN_RESTART_GAP)
        except Exception as e:
            log.error("loop error: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
