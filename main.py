"""Dispatch Agent — system tray entry point."""
import sys
import threading
import logging
from pathlib import Path
from io import BytesIO

from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem, Menu

from agent.config import ensure_config
from agent.poller import Poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "agent.log", encoding="utf-8"),
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
_poller: Poller | None = None


# ── Tray icon image ────────────────────────────────────────────────────────

def _make_icon(color: tuple) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color + (255,))
    return img


# ── Menu actions ───────────────────────────────────────────────────────────

def _show_status(icon, item):
    cfg = ensure_config()
    status = _poller.status if _poller else "stopped"
    import tkinter.messagebox as mb
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    mb.showinfo(
        "Dispatch Agent Status",
        f"Status: {status}\nRailway URL: {cfg.get('railway_url') or '(not set)'}\nToken: {cfg.get('agent_token', '')[:8]}...",
    )
    root.destroy()


def _open_settings(icon, item):
    from ui.settings_window import open_settings
    threading.Thread(target=open_settings, daemon=True).start()


def _open_log(icon, item):
    import subprocess
    log_path = Path(__file__).parent / "agent.log"
    subprocess.Popen(["notepad.exe", str(log_path)])


def _exit_app(icon, item):
    if _poller:
        _poller.stop()
    icon.stop()


# ── Status change callback ─────────────────────────────────────────────────

def _on_status_change(status: str):
    if _icon is None:
        return
    color = _STATUS_COLORS.get(status, _STATUS_COLORS["stopped"])
    _icon.icon = _make_icon(color)
    _icon.title = f"Dispatch Agent — {status}"
    logger.info(f"Status: {status}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    global _icon, _poller

    ensure_config()

    _poller = Poller(on_status_change=_on_status_change)
    _poller.start()

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
