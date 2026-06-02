"""Single source of truth for where the agent stores its data.

Dev (running from source): use the project root (E:\\dispatch).
Frozen (PyInstaller exe):  use %APPDATA%\\AgentOS so the install dir stays
read-only and the user's config survives reinstalls.

The Electron Control Center mirrors this with `app.isPackaged` in
`agent_os_app/main.js`. Both sides MUST resolve to the same directory in
prod or the agent and CC will see different state files.
"""
import os
import sys
from pathlib import Path


def _resolve_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "AgentOS"
    # dev: project root (agent/paths.py is two levels deep from project root)
    return Path(__file__).resolve().parent.parent


DATA_DIR = _resolve_data_dir()
# Ensure the data dir exists on import — the very first read on a fresh
# install would otherwise race with the first write.
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH         = DATA_DIR / "config.json"
STATE_PATH          = DATA_DIR / "agent_state.json"
LOG_PATH            = DATA_DIR / "agent.log"
CHATS_PATH          = DATA_DIR / "chats.json"
CLAUDE_TASK_PATH    = DATA_DIR / "claude_task.json"
# Append-only history of every /claude run (one JSON per line). Surfaced via
# /history in Telegram so the user can see what they ran yesterday and how it ended.
CLAUDE_RUNS_PATH    = DATA_DIR / "claude_runs.jsonl"
INBOX_DIR           = DATA_DIR / "inbox"
