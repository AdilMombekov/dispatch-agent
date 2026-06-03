"""Computer-use loop (P3.12) — Haiku drives the desktop via the computer tool.

Safety layers:
  * pre-flight: destructive-looking task prompts go through the SonnetReviewer
    (injected) before the loop starts;
  * per-step: should_abort() (emergency stop / budget) is checked between steps;
  * pyautogui FAILSAFE (corner-slam) and a hard max_steps cap.

Live testing is the user's job — this moves the real mouse/keyboard.
"""
from __future__ import annotations

import base64
import logging
from typing import Callable, Optional

import requests

logger = logging.getLogger("orchestrator.computer")

API_URL = "https://api.anthropic.com/v1/messages"
BETA_HEADER = "computer-use-2025-01-24"
TOOL_TYPE = "computer_20250124"


class ComputerUseClient:
    def __init__(self, api_key: str, model: str, desktop, *,
                 reviewer=None, is_destructive: Optional[Callable[[str], bool]] = None,
                 should_abort: Optional[Callable[[], bool]] = None,
                 on_step: Optional[Callable[[str], None]] = None,
                 max_steps: int = 25):
        self._key = api_key
        self._model = model
        self._d = desktop
        self._reviewer = reviewer
        self._is_destructive = is_destructive or (lambda s: False)
        self._should_abort = should_abort or (lambda: False)
        self._on_step = on_step or (lambda s: None)
        self._max_steps = max_steps

    def run(self, prompt: str) -> dict:
        """Returns {summary, steps, usage:{input_tokens,output_tokens}, aborted, denied}."""
        # Pre-flight safety review for destructive-looking goals.
        if self._reviewer is not None and self._is_destructive(prompt):
            verdict = self._reviewer.review(f"GUI-задача: {prompt}",
                                            context="агент будет кликать/печатать на ПК")
            if verdict.get("verdict") == "deny":
                return {"summary": f"заблокировано ревьюером: {verdict.get('reason')}",
                        "steps": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
                        "aborted": False, "denied": True}

        tools = [{
            "type": TOOL_TYPE, "name": "computer",
            "display_width_px": self._d.width,
            "display_height_px": self._d.height,
            "display_number": 1,
        }]
        messages = [{"role": "user", "content": prompt}]
        usage_total = {"input_tokens": 0, "output_tokens": 0}
        summary_parts: list[str] = []

        for step in range(self._max_steps):
            if self._should_abort():
                return self._result(summary_parts, step, usage_total, aborted=True)

            data = self._post(messages, tools)
            u = data.get("usage", {}) or {}
            usage_total["input_tokens"] += u.get("input_tokens", 0)
            usage_total["output_tokens"] += u.get("output_tokens", 0)

            content = data.get("content", []) or []
            tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    summary_parts.append(b["text"])
                    self._on_step(b["text"])

            messages.append({"role": "assistant", "content": content})

            if data.get("stop_reason") != "tool_use" or not tool_uses:
                return self._result(summary_parts, step + 1, usage_total)

            # Execute each requested tool action and return results (with a fresh
            # screenshot) so the model can decide the next step.
            tool_results = []
            for tu in tool_uses:
                result_block = self._exec_tool(tu)
                tool_results.append(result_block)
            messages.append({"role": "user", "content": tool_results})

        return self._result(summary_parts, self._max_steps, usage_total, capped=True)

    # ── internals ────────────────────────────────────────────────────────────

    def _post(self, messages: list, tools: list) -> dict:
        resp = requests.post(
            API_URL,
            headers={
                "x-api-key": self._key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": BETA_HEADER,
                "content-type": "application/json",
            },
            json={"model": self._model, "max_tokens": 1024,
                  "tools": tools, "messages": messages},
            timeout=120,
        )
        data = resp.json()
        if isinstance(data, dict) and data.get("type") == "error":
            raise RuntimeError((data.get("error") or {}).get("message", "computer-use API error"))
        return data

    def _exec_tool(self, tu: dict) -> dict:
        """Run one computer action; return a tool_result block with a screenshot."""
        inp = tu.get("input", {}) or {}
        action = inp.get("action")
        try:
            self._dispatch(action, inp)
            err = None
        except Exception as e:
            logger.warning("action %s failed: %s", action, e)
            err = str(e)

        block = {"type": "tool_result", "tool_use_id": tu.get("id")}
        if err:
            block["content"] = [{"type": "text", "text": f"action failed: {err}"}]
            block["is_error"] = True
        else:
            shot = base64.b64encode(self._d.screenshot()).decode()
            block["content"] = [{
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": shot},
            }]
        return block

    def _dispatch(self, action: str, inp: dict) -> None:
        coord = inp.get("coordinate") or [0, 0]
        x, y = int(coord[0]), int(coord[1])
        if action == "screenshot":
            return
        elif action == "mouse_move":
            self._d.mouse_move(x, y)
        elif action == "left_click":
            self._d.left_click(x, y)
        elif action == "right_click":
            self._d.right_click(x, y)
        elif action == "middle_click":
            self._d.middle_click(x, y)
        elif action == "double_click":
            self._d.double_click(x, y)
        elif action == "left_click_drag":
            self._d.left_click_drag(x, y)
        elif action == "type":
            self._d.type(inp.get("text", ""))
        elif action == "key":
            self._d.key(inp.get("text", ""))
        elif action == "scroll":
            self._d.scroll(x, y, inp.get("scroll_direction", "down"),
                           int(inp.get("scroll_amount", 3)))
        elif action in ("wait",):
            self._d.wait(float(inp.get("duration", 1)))
        elif action == "cursor_position":
            return
        else:
            raise ValueError(f"unsupported action: {action}")

    def _result(self, parts, steps, usage, *, aborted=False, capped=False, denied=False) -> dict:
        summary = "\n".join(p for p in parts if p).strip()
        if aborted:
            summary = (summary + "\n⛔ Прервано.").strip()
        elif capped:
            summary = (summary + f"\n⚠️ Достигнут лимит {self._max_steps} шагов.").strip()
        return {"summary": summary or "(без текстового резюме)", "steps": steps,
                "usage": usage, "aborted": aborted, "denied": denied}
