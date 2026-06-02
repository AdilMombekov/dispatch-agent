"""Dispatch Agent — system tray entry point."""
import sys
import threading
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from io import BytesIO

from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem, Menu

from agent.config import ensure_config
from agent.paths import LOG_PATH as _LOG_PATH
from agent.singleton import acquire as _acquire_singleton
from agent.telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        # Rotate at 10 MB, keep 5 old files (agent.log, agent.log.1 … .5).
        RotatingFileHandler(_LOG_PATH, maxBytes=10 * 1024 * 1024,
                            backupCount=5, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")

_STATUS_COLORS = {
    "idle": (80, 200, 80),      # green
    "active": (255, 200, 0),    # yellow
    "error": (220, 50, 50),     # red
    "stopped": (120, 120, 120), # grey
}

_icon: pystray.Icon | None = None
_bot: TelegramBot | None = None


# ── Tray icon image ────────────────────────────────────────────────────────

def _make_icon(color: tuple) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color + (255,))
    return img


# ── Menu actions ───────────────────────────────────────────────────────────

def _show_status(icon, item):
    cfg = ensure_config()
    status = _bot.status if _bot else "stopped"
    bot_token = cfg.get("telegram_bot_token", "")
    import tkinter.messagebox as mb
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    mb.showinfo(
        "Dispatch Agent Status",
        f"Status: {status}\nTelegram bot: {'set' if bot_token else '(not set)'}\n"
        f"AI accounts: {len(cfg.get('anthropic_api_keys') or [])}",
    )
    root.destroy()


def _open_settings(icon, item):
    """Launch the Electron Control Center.

    Resolution order:
      1) packaged exe: dist/Agent-OS Control Center.exe (or similar) next to project
      2) dev mode: `npm start` inside agent_os_app/
      3) fallback: legacy customtkinter window (ui/settings_window.py)
    """
    import subprocess, os
    base = Path(__file__).resolve().parent
    app_dir = base / "agent_os_app"

    # 1) packaged build
    for cand in (app_dir / "dist" / "Agent-OS Control Center.exe",
                 app_dir / "dist" / "win-unpacked" / "Agent-OS Control Center.exe"):
        if cand.exists():
            subprocess.Popen([str(cand)], cwd=str(cand.parent))
            return

    # 2) dev mode (npm start) — needs Node installed and `npm install` done
    if (app_dir / "node_modules" / "electron").exists():
        npm = "npm.cmd" if os.name == "nt" else "npm"
        subprocess.Popen([npm, "start"], cwd=str(app_dir), shell=False,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return

    # 3) legacy fallback
    try:
        from ui.settings_window import open_settings
        threading.Thread(target=open_settings, daemon=True).start()
    except Exception as e:
        logger.error(f"No Settings UI available: {e}. Run `npm install` inside agent_os_app/.")


def _open_log(icon, item):
    import subprocess
    subprocess.Popen(["notepad.exe", str(_LOG_PATH)])


def _exit_app(icon, item):
    if _bot:
        _bot.stop()
    icon.stop()


# ── Status change callback ─────────────────────────────────────────────────

def _on_status_change(status: str):
    logger.info(f"Status: {status}")
    if _icon is None:
        return
    color = _STATUS_COLORS.get(status, _STATUS_COLORS["stopped"])
    _icon.icon = _make_icon(color)
    _icon.title = f"Dispatch Agent — {status}"


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    global _icon, _bot

    # Single-instance guard: refuse to start if another agent already runs.
    # Two agents on one Telegram token cause "Conflict: terminated by other
    # getUpdates request". This is the authoritative fix; watchdog debounce
    # (P0.2) just reduces how often the guard is exercised.
    if not _acquire_singleton():
        logger.error("another agent instance is already running — exiting")
        sys.exit(1)

    ensure_config()

    _bot = TelegramBot(on_status_change=_on_status_change)
    _bot.start()

    menu = Menu(
        MenuItem("Status", _show_status),
        MenuItem("Settings", _open_settings),
        MenuItem("Open Log", _open_log),
        Menu.SEPARATOR,
        MenuItem("Exit", _exit_app),
    )

    _icon = pystray.Icon(
        "DispatchAgent",
        _make_icon(_STATUS_COLORS["idle"]),
        "Dispatch Agent",
        menu,
    )

    logger.info("Dispatch Agent starting")
    _icon.run()


if __name__ == "__main__":
    main()
