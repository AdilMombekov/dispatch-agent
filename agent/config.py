import json
import uuid
import os
import shutil
import sys
from pathlib import Path

# Single-source data location: dev → project root, frozen → %APPDATA%\AgentOS.
from agent.paths import CONFIG_PATH

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


def _start_menu_roots() -> list:
    return [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]


def scan_start_menu_apps() -> dict:
    """Discover installed apps from Start Menu .lnk shortcuts -> {name: exe_path}.

    Resolves shortcut targets via WScript.Shell (PowerShell). Returns only
    shortcuts that point to an existing .exe.
    """
    import subprocess

    roots = [str(r) for r in _start_menu_roots() if r.exists()]
    if not roots:
        return {}
    roots_ps = ",".join(f"'{r}'" for r in roots)
    script = f"""
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$sh = New-Object -ComObject WScript.Shell
Get-ChildItem -Path {roots_ps} -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {{
  $t = $sh.CreateShortcut($_.FullName).TargetPath
  if ($t -and $t.ToLower().EndsWith('.exe') -and (Test-Path $t)) {{ Write-Output ("$($_.BaseName)`t$t") }}
}}
"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).stdout
    except Exception:
        return {}

    found = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, _, target = line.partition("\t")
        name, target = name.strip(), target.strip()
        if name and target and name not in found:
            found[name] = target
    return found


def rescan_apps() -> dict:
    """Re-detect installed apps and merge findings into config.json.

    - Refreshes curated apps (fills empty paths, repairs paths that no longer exist).
    - Discovers other installed apps from the Start Menu into 'discovered_apps'.
    Never overwrites a still-valid user-set path. Returns a change summary.
    """
    cfg = load_config()
    apps = dict(cfg.get("apps", {}))
    discovered = dict(cfg.get("discovered_apps", {}))
    added, updated = {}, {}

    for key, path in detect_app_paths().items():
        if not path:
            continue
        current = apps.get(key, "")
        if not current:
            apps[key] = path
            added[key] = path
        elif current != path and not Path(current).exists():
            apps[key] = path
            updated[key] = path

    for name, target in scan_start_menu_apps().items():
        if name in apps or name in discovered:
            continue
        discovered[name] = target
        added[name] = target

    if added or updated:
        cfg["apps"] = apps
        cfg["discovered_apps"] = discovered
        save_config(cfg)

    return {"added": added, "updated": updated, "total_apps": len(apps) + len(discovered)}


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
        "apps": detect_app_paths(),
        "autostart": False,
        "telegram_bot_token": "",
        "anthropic_api_keys": [],
        "api_limit_usd": 5.0,
        # If set (list of absolute paths), /claude shows ONLY these folders
        # instead of scanning the drive. Empty/missing = scan-mode (legacy behavior).
        "claude_dev_folders": [],
        # Drive/folder to scan for project folders when allowlist is empty.
        # Defaults to E:\ for the original setup; set to e.g. "D:\\dev" to override.
        "claude_dev_root": "",
        # When True, after each AI turn the bot deletes status pings / intermediate
        # screenshots / recovered errors and keeps only the final answer.
        "telegram_auto_clean": True,
        # Skills/tools turned off from Haiku's toolbox. Names match either top-level
        # tool ("queue_agent_command", "control_computer", "analyze",
        # "run_claude_code") or queue_agent_command sub-types ("terminal",
        # "screenshot", "obsidian-log", "install", ...).
        "skill_disabled": [],
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
