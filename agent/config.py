import json
import uuid
import os
import shutil
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

DEFAULT_APPS = {
    "cursor": "",
    "vscode": "",
    "claude-code": "",
    "antigravity": "",
    "terminal": "cmd.exe",
    "chrome": "",
}

KNOWN_APP_PATHS = {
    "cursor": [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "cursor" / "Cursor.exe",
        Path("C:/Program Files/Cursor/Cursor.exe"),
    ],
    "vscode": [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe",
        Path("C:/Program Files/Microsoft VS Code/Code.exe"),
    ],
    "claude-code": [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude-code" / "Claude Code.exe",
        Path("C:/Program Files/Claude Code/Claude Code.exe"),
    ],
    "antigravity": [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "antigravity" / "Antigravity.exe",
        Path("C:/Program Files/Antigravity/Antigravity.exe"),
    ],
    "chrome": [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ],
}


def detect_app_paths() -> dict:
    paths = dict(DEFAULT_APPS)
    for app, candidates in KNOWN_APP_PATHS.items():
        for p in candidates:
            if p.exists():
                paths[app] = str(p)
                break
    terminal = shutil.which("wt") or shutil.which("cmd")
    if terminal:
        paths["terminal"] = terminal
    return paths


def get_chrome_profiles() -> list:
    user_data = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    profiles = []
    if not user_data.exists():
        return profiles

    for entry in sorted(user_data.iterdir()):
        if entry.name == "Default" or entry.name.startswith("Profile "):
            prefs_file = entry / "Preferences"
            if not prefs_file.exists():
                continue
            email = ""
            name = entry.name
            try:
                data = json.loads(prefs_file.read_text(encoding="utf-8", errors="ignore"))
                account_info = data.get("account_info", [])
                if account_info and isinstance(account_info, list):
                    email = account_info[0].get("email", "")
                if not email:
                    email = (
                        data.get("profile", {}).get("gaia_name", "")
                        or data.get("profile", {}).get("name", "")
                    )
            except Exception:
                pass

            display = email if email else f"{name} (not signed in)"
            profiles.append({"directory": entry.name, "display": display, "email": email})

    return profiles


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _default_config()


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _default_config() -> dict:
    return {
        "railway_url": "",
        "agent_token": str(uuid.uuid4()),
        "poll_interval_active": 2,
        "poll_interval_idle": 10,
        "apps": detect_app_paths(),
        "autostart": False,
    }


def ensure_config() -> dict:
    cfg = load_config()
    changed = False
    if not cfg.get("agent_token"):
        cfg["agent_token"] = str(uuid.uuid4())
        changed = True
    for key, val in _default_config().items():
        if key not in cfg:
            cfg[key] = val
            changed = True
    if changed:
        save_config(cfg)
    return cfg
