"""Desktop control surface for computer-use (P3.12).

Wraps pyautogui. The model is shown a downscaled screenshot (computer-use works
better and cheaper at ~1280px wide); coordinates it returns are scaled back up
to the real screen. pyautogui FAILSAFE is on: slamming the cursor into a screen
corner aborts the run.
"""
from __future__ import annotations

import io
import logging
import time

logger = logging.getLogger("orchestrator.desktop")

# xdotool-style key names (what computer-use emits) → pyautogui names.
_KEYMAP = {
    "return": "enter", "enter": "enter", "tab": "tab", "escape": "esc", "esc": "esc",
    "backspace": "backspace", "delete": "delete", "space": "space",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "page_up": "pageup", "page_down": "pagedown",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
    "super": "win", "cmd": "win", "win": "win",
}


class Desktop:
    def __init__(self, target_width: int = 1280):
        import pyautogui
        self._pg = pyautogui
        self._pg.FAILSAFE = True
        self._pg.PAUSE = 0.05
        self.real_w, self.real_h = self._pg.size()
        self.scale = self.real_w / float(target_width)
        self.width = target_width
        self.height = round(self.real_h / self.scale)

    # ── perception ───────────────────────────────────────────────────────────

    def screenshot(self) -> bytes:
        """PNG bytes at model resolution (self.width x self.height)."""
        img = self._pg.screenshot()  # PIL Image at real resolution
        if img.size != (self.width, self.height):
            from PIL import Image
            img = img.resize((self.width, self.height), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _to_real(self, x: int, y: int) -> tuple[int, int]:
        return round(x * self.scale), round(y * self.scale)

    # ── actions (coords are in MODEL space) ───────────────────────────────────

    def mouse_move(self, x: int, y: int) -> None:
        rx, ry = self._to_real(x, y)
        self._pg.moveTo(rx, ry)

    def left_click(self, x: int, y: int) -> None:
        rx, ry = self._to_real(x, y)
        self._pg.click(rx, ry)

    def right_click(self, x: int, y: int) -> None:
        rx, ry = self._to_real(x, y)
        self._pg.click(rx, ry, button="right")

    def middle_click(self, x: int, y: int) -> None:
        rx, ry = self._to_real(x, y)
        self._pg.click(rx, ry, button="middle")

    def double_click(self, x: int, y: int) -> None:
        rx, ry = self._to_real(x, y)
        self._pg.doubleClick(rx, ry)

    def left_click_drag(self, x: int, y: int) -> None:
        rx, ry = self._to_real(x, y)
        self._pg.dragTo(rx, ry, duration=0.3)

    def type(self, text: str) -> None:
        self._pg.write(text, interval=0.01)

    def key(self, combo: str) -> None:
        """Translate an xdotool-style combo ('ctrl+s', 'Return') to pyautogui."""
        parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
        mapped = [_KEYMAP.get(p, p) for p in parts]
        if len(mapped) == 1:
            self._pg.press(mapped[0])
        elif mapped:
            self._pg.hotkey(*mapped)

    def scroll(self, x: int, y: int, direction: str, clicks: int = 3) -> None:
        rx, ry = self._to_real(x, y)
        self._pg.moveTo(rx, ry)
        amount = clicks * 100
        if direction == "down":
            amount = -amount
        if direction in ("left", "right"):
            self._pg.hscroll(amount if direction == "right" else -amount, rx, ry)
        else:
            self._pg.scroll(amount, rx, ry)

    def wait(self, seconds: float = 1.0) -> None:
        time.sleep(min(seconds, 5.0))
