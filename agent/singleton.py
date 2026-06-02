"""Single-instance guard for the Dispatch agent.

Prevents two agent processes from polling Telegram `getUpdates` with the same
token simultaneously — Telegram rejects that with
"Conflict: terminated by other getUpdates request" (seen in agent.log on
2026-05-27 and 2026-06-01). The race is most likely when watchdog.py spawns a
fresh agent during the old one's self-restart window.

Mechanism: a Windows named mutex. The OS releases it automatically when the
holding process dies — even on a hard kill — so there are no stale lock files
to clean up. Falls back to a POSIX file lock on non-Windows, and fails *open*
(allows start) if the guard primitive is unavailable, since wedging the agent
shut is worse than a rare duplicate.
"""
from __future__ import annotations

import sys
import logging

logger = logging.getLogger("singleton")

# Global\ namespace → visible across sessions and elevation, so an elevated
# watchdog-spawned agent still sees a non-elevated one (and vice versa).
_MUTEX_NAME = "Global\\DispatchAgentSingleton"

# Held for the entire process lifetime so the handle/fd isn't garbage-collected
# (which would release the lock). Never reassigned after a successful acquire.
_handle = None


def acquire() -> bool:
    """Return True if this process is the sole agent instance.

    False means another agent already holds the lock — the caller should log and
    exit. On any unexpected error we fail open (return True) and log a warning.
    """
    global _handle

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]

            ERROR_ALREADY_EXISTS = 183
            handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
            last_err = ctypes.get_last_error() if False else kernel32.GetLastError()

            if not handle:
                logger.warning("CreateMutexW returned NULL (err=%s) — allowing start", last_err)
                return True  # fail open
            if last_err == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            _handle = handle  # hold for lifetime
            return True
        except Exception as e:
            logger.warning("named-mutex guard unavailable (%s) — allowing start", e)
            return True

    # Non-Windows fallback: exclusive, non-blocking POSIX file lock.
    try:
        import os
        from agent.paths import DATA_DIR

        lock_path = DATA_DIR / "agent.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(fd)
            return False
        _handle = fd  # hold for lifetime
        return True
    except Exception as e:
        logger.warning("file-lock guard unavailable (%s) — allowing start", e)
        return True
