"""Background polling thread — polls Railway n8n every N seconds."""
import threading
import time
import logging
from typing import Callable

import requests

from agent.config import load_config, save_config
from agent.handlers import dispatch

logger = logging.getLogger("poller")

_BACKOFF_STEPS = [2, 4, 8, 16, 30, 60]


class Poller:
    def __init__(self, on_status_change: Callable[[str], None] = None):
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_status_change = on_status_change or (lambda s: None)
        self._error_count = 0
        self._status = "stopped"  # stopped | idle | active | error

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="DispatchPoller")
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    @property
    def status(self) -> str:
        return self._status

    # ── Core loop ──────────────────────────────────────────────────────────

    def _run(self):
        self._set_status("idle")
        while not self._stop_event.is_set():
            cfg = load_config()
            url = cfg.get("railway_url", "").rstrip("/")
            token = cfg.get("agent_token", "")

            if not url or not token:
                self._set_status("idle")
                self._stop_event.wait(5)
                continue

            interval_active = cfg.get("poll_interval_active", 2)
            interval_idle = cfg.get("poll_interval_idle", 10)

            command = self._poll(url, token)
            if command is None:
                # network / config error — already logged, backoff
                sleep = self._backoff_interval()
                self._stop_event.wait(sleep)
                continue

            self._error_count = 0

            if command:
                self._set_status("active")
                self._execute(command, url, token, cfg.get("apps", {}))
                sleep = interval_active
            else:
                self._set_status("idle")
                sleep = interval_idle

            self._stop_event.wait(sleep)

        self._set_status("stopped")

    # ── Poll ───────────────────────────────────────────────────────────────

    def _poll(self, url: str, token: str) -> dict | None:
        try:
            resp = requests.get(
                f"{url}/webhook/agent-poll",
                headers={"X-Agent-Token": token},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                return {}
            return data.get("data") or {}
        except requests.exceptions.ConnectionError:
            logger.warning("poll: connection error")
            self._set_status("error")
            self._error_count += 1
            return None
        except requests.exceptions.Timeout:
            logger.warning("poll: timeout")
            self._set_status("error")
            self._error_count += 1
            return None
        except Exception as e:
            logger.error(f"poll error: {e}")
            self._set_status("error")
            self._error_count += 1
            return None

    # ── Execute & post result ─────────────────────────────────────────────

    def _execute(self, command: dict, url: str, token: str, apps: dict):
        command_id = command.get("command_id", "")
        chat_id = command.get("chat_id", "")

        try:
            result = dispatch(command, apps)
        except Exception as e:
            result = {"success": False, "data": None, "error": str(e)}

        self._post_result(url, token, command_id, chat_id, command, result)

    def _post_result(
        self,
        url: str,
        token: str,
        command_id: str,
        chat_id: str,
        command: dict,
        result: dict,
    ):
        payload = {
            "command_id": command_id,
            "chat_id": chat_id,
            "command_type": command.get("type", ""),
            "result": result,
        }
        try:
            resp = requests.post(
                f"{url}/webhook/agent-result",
                json=payload,
                headers={"X-Agent-Token": token},
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"post result error: {e}")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _set_status(self, status: str):
        if self._status != status:
            self._status = status
            self._on_status_change(status)

    def _backoff_interval(self) -> int:
        idx = min(self._error_count - 1, len(_BACKOFF_STEPS) - 1)
        return _BACKOFF_STEPS[max(idx, 0)]
