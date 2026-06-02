"""Task routing — decide which executor `kind` handles a prompt.

Step 1 (P3.9): cheap keyword heuristic. Step 2 (P3.10) may add a Haiku
classifier fallback for ambiguous prompts. Kinds:
  click_gui — operate the desktop GUI (open apps, click, type into windows)
  code      — work with files / repos / git
  qa        — everything else (answer a question, search, translate)
"""
from __future__ import annotations

# Substrings (Russian + English) that signal each kind. Lowercased match.
_GUI_HINTS = (
    "открой", "запусти", "нажми", "кликни", "закрой", "сверни", "переключись",
    "введи в", "набери в", "скриншот", "screenshot", "open ", "launch ", "click ",
    "type into", "окно", "вкладк",
)
_CODE_HINTS = (
    "закоммить", "коммит", "commit", "git ", "пуш", "push", "рефактор", "refactor",
    "файл", "функци", "функция", "репозитор", "repo", "багфикс", "bug", "тест",
    "напиши код", "code", "скрипт", "script", "класс ", "метод ",
)


def classify(prompt: str) -> str:
    p = (prompt or "").lower()
    if any(h in p for h in _GUI_HINTS):
        return "click_gui"
    if any(h in p for h in _CODE_HINTS):
        return "code"
    return "qa"
