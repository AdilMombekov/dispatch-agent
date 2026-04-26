"""Command handlers — each returns {"success": bool, "data": ..., "error": str|None}."""
import base64
import io
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import psutil
from PIL import Image


def _ok(data) -> dict:
    return {"success": True, "data": data, "error": None}


def _err(msg: str) -> dict:
    return {"success": False, "data": None, "error": str(msg)}


# ── Screenshot ──────────────────────────────────────────────────────────────

def handle_screenshot(_payload: dict) -> dict:
    try:
        import mss
        import mss.tools

        with mss.mss() as sct:
            monitor = sct.monitors[1]  # primary monitor
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        max_size = 1280
        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return _ok({"image": b64, "width": img.width, "height": img.height})
    except Exception as e:
        return _err(f"screenshot failed: {e}")


# ── Terminal ─────────────────────────────────────────────────────────────────

def handle_terminal(payload: dict) -> dict:
    cmd = payload.get("command", "")
    if not cmd:
        return _err("no command provided")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            encoding="utf-8",
            errors="replace",
        )
        output = result.stdout + result.stderr
        if len(output) > 3000:
            file_b64 = base64.b64encode(output.encode("utf-8")).decode()
            preview = output[:300].rstrip()
            return _ok({
                "output": preview + f"\n\n… ({len(output)} chars — full output attached)",
                "output_file_b64": file_b64,
                "filename": "terminal_output.txt",
                "returncode": result.returncode,
                "send_as_file": True,
            })
        return _ok({"output": output, "returncode": result.returncode, "send_as_file": False})
    except subprocess.TimeoutExpired:
        return _err("command timed out after 60s")
    except Exception as e:
        return _err(f"terminal error: {e}")


# ── Launch App ────────────────────────────────────────────────────────────────

def handle_launch_app(payload: dict, apps: dict) -> dict:
    app_name = payload.get("app", "")
    app_path = apps.get(app_name, "")
    if not app_path:
        return _err(f"app '{app_name}' not configured or not found")
    try:
        subprocess.Popen(
            [app_path],
            creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
        )
        return _ok(f"Launched {app_name}")
    except Exception as e:
        return _err(f"could not launch {app_name}: {e}")


# ── Chrome Profile ────────────────────────────────────────────────────────────

def handle_chrome_profile(payload: dict, apps: dict) -> dict:
    profile_dir = payload.get("profile_directory", "")
    chrome_path = apps.get("chrome", "")
    if not chrome_path:
        return _err("Chrome not found in config")
    if not profile_dir:
        return _err("no profile_directory provided")
    try:
        subprocess.Popen(
            [chrome_path, f"--profile-directory={profile_dir}"],
            creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
        )
        return _ok(f"Launched Chrome with profile: {profile_dir}")
    except Exception as e:
        return _err(f"could not launch Chrome profile: {e}")


def handle_chrome_profiles_list(_payload: dict) -> dict:
    from agent.config import get_chrome_profiles
    return _ok(get_chrome_profiles())


# ── Install ────────────────────────────────────────────────────────────────────

def handle_install(payload: dict) -> dict:
    url = payload.get("url", "")
    if not url:
        return _err("no url provided")

    suffix = Path(url.split("?")[0]).suffix.lower()
    if suffix not in (".exe", ".msi", ".zip"):
        return _err(f"unsupported installer type: {suffix}")

    tmp_dir = Path(tempfile.mkdtemp())
    dest = tmp_dir / f"installer{suffix}"

    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:
        return _err(f"download failed: {e}")

    try:
        if suffix == ".exe":
            subprocess.run(
                [str(dest), "/S", "/silent", "/quiet"],
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        elif suffix == ".msi":
            subprocess.run(
                ["msiexec", "/i", str(dest), "/quiet", "/norestart"],
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        elif suffix == ".zip":
            import zipfile
            extract_to = tmp_dir / "extracted"
            with zipfile.ZipFile(dest) as z:
                z.extractall(extract_to)
            return _ok(f"Extracted to {extract_to}")
        return _ok(f"Install initiated for {dest.name}")
    except Exception as e:
        return _err(f"install error: {e}")


# ── System Info ───────────────────────────────────────────────────────────────

def handle_system_info(_payload: dict) -> dict:
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")
        return _ok({
            "cpu_percent": cpu,
            "ram_used_gb": round(ram.used / 1e9, 2),
            "ram_total_gb": round(ram.total / 1e9, 2),
            "ram_percent": ram.percent,
            "disk_used_gb": round(disk.used / 1e9, 2),
            "disk_total_gb": round(disk.total / 1e9, 2),
            "disk_percent": disk.percent,
        })
    except Exception as e:
        return _err(f"system-info error: {e}")


# ── Obsidian Log ──────────────────────────────────────────────────────────────

def handle_obsidian_log(payload: dict, obsidian_cfg: dict) -> dict:
    """Append a log entry to an Obsidian note via the Local REST API plugin.

    payload keys:
      text      – required, the line to append
      note      – optional, override note name (default from config)
    """
    import urllib.request as _req
    import urllib.error as _uerr
    from datetime import datetime

    text = payload.get("text", "").strip()
    if not text:
        return _err("no text provided")

    host    = obsidian_cfg.get("host", "http://localhost:27123")
    token   = obsidian_cfg.get("token", "")
    note    = payload.get("note") or obsidian_cfg.get("note", "claude97")
    vault   = obsidian_cfg.get("vault", "")

    # Build timestamped markdown line
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"- `{ts}` {text}\n"

    # Obsidian Local REST API: PATCH /vault/<note>.md  → appends content
    vault_path = f"/vault/{vault}/" if vault else "/vault/"
    url = f"{host}{vault_path}{note}.md"

    try:
        request = _req.Request(
            url,
            data=line.encode("utf-8"),
            method="PATCH",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/markdown",
            },
        )
        with _req.urlopen(request, timeout=5) as r:
            r.read()
        return _ok(f"Logged to Obsidian note '{note}': {text[:80]}")
    except _uerr.HTTPError as e:
        # 404 on PATCH = note doesn't exist yet; create it with PUT
        if e.code == 404:
            try:
                header_line = f"# {note}\n\n"
                put_req = _req.Request(
                    url,
                    data=(header_line + line).encode("utf-8"),
                    method="PUT",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "text/markdown",
                    },
                )
                with _req.urlopen(put_req, timeout=5) as r:
                    r.read()
                return _ok(f"Created & logged to Obsidian note '{note}'")
            except Exception as e2:
                return _err(f"obsidian create failed: {e2}")
        return _err(f"obsidian http {e.code}: {e}")
    except Exception as e:
        return _err(f"obsidian error: {e}")


def handle_obsidian_read(payload: dict, obsidian_cfg: dict) -> dict:
    """Read the last N lines of an Obsidian note."""
    import urllib.request as _req

    note  = payload.get("note") or obsidian_cfg.get("note", "claude97")
    lines = int(payload.get("lines", 20))
    host  = obsidian_cfg.get("host", "http://localhost:27123")
    token = obsidian_cfg.get("token", "")
    vault = obsidian_cfg.get("vault", "")

    vault_path = f"/vault/{vault}/" if vault else "/vault/"
    url = f"{host}{vault_path}{note}.md"

    try:
        request = _req.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        with _req.urlopen(request, timeout=5) as r:
            content = r.read().decode("utf-8")
        tail = "\n".join(content.splitlines()[-lines:])
        return _ok({"note": note, "content": tail})
    except Exception as e:
        return _err(f"obsidian read error: {e}")


# ── Dispatcher ────────────────────────────────────────────────────────────────

def dispatch(command: dict, apps: dict, obsidian_cfg: dict | None = None) -> dict:
    cmd_type = command.get("type", "")
    payload  = command.get("payload", {})
    obs_cfg  = obsidian_cfg or {}

    if cmd_type == "screenshot":
        return handle_screenshot(payload)
    elif cmd_type == "terminal":
        return handle_terminal(payload)
    elif cmd_type == "launch-app":
        return handle_launch_app(payload, apps)
    elif cmd_type == "chrome-profile":
        return handle_chrome_profile(payload, apps)
    elif cmd_type == "chrome-profiles-list":
        return handle_chrome_profiles_list(payload)
    elif cmd_type == "install":
        return handle_install(payload)
    elif cmd_type == "system-info":
        return handle_system_info(payload)
    elif cmd_type == "obsidian-log":
        return handle_obsidian_log(payload, obs_cfg)
    elif cmd_type == "obsidian-read":
        return handle_obsidian_read(payload, obs_cfg)
    else:
        return _err(f"unknown command type: {cmd_type}")
