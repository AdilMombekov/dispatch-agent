"""SonnetReviewer — a safety gate for destructive actions.

The orchestrator's executors run with real power (claude -p trusted; later,
mouse/keyboard control). Before a *destructive* action, the executor asks
Sonnet for a quick allow/deny verdict. Cheap actions skip the reviewer
(is_destructive() returns False) so we don't pay for trivial reviews.

Decoupled from the bot via an injected `ask(system, user) -> str` callable
(wired to the bot's _anthropic on ANALYST_MODEL).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable

logger = logging.getLogger("orchestrator.reviewer")

# Substrings that mark an action as risky enough to warrant a Sonnet review.
# Lowercased match against the action/prompt text.
_DESTRUCTIVE_HINTS = (
    "rm -rf", "rm -r", "remove-item", "rmdir", "del /", "format ", "mkfs",
    "drop table", "drop database", "truncate ",
    "git push --force", "push --force", "force push", "--force-with-lease",
    "git reset --hard", "git clean -", "checkout --",
    "shutdown", "reboot", "taskkill", "kill -9",
    ".env", "secret", "token", "password", "credential", "private key",
    "curl ", "wget ", "invoke-webrequest", "iwr ", "downloadstring",
    "registry", "reg add", "reg delete", "regedit",
    "send money", "transfer", "payment", "оплат", "перевод", "перевести деньг",
    "удали все", "снеси", "формат",
)

_VERDICT_SYSTEM = (
    "Ты — страж безопасности для агента, управляющего личным ПК пользователя. "
    "Тебе показывают предлагаемое действие. Оцени риск НЕОБРАТИМОГО или опасного "
    "эффекта: удаление/перезапись данных, отправка денег, утечка секретов "
    "(.env, токены, пароли), force-push, выключение системы, запуск "
    "недоверенного кода из интернета. "
    "Ответь СТРОГО одним JSON-объектом без пояснений: "
    '{\"verdict\": \"allow\" | \"deny\", \"reason\": \"кратко по-русски\"}. '
    "allow — обычная безопасная работа. deny — есть реальный риск необратимого "
    "вреда без явного намерения пользователя."
)


def is_destructive(text: str) -> bool:
    """Cheap pre-filter: should this action be sent to Sonnet for review?"""
    t = (text or "").lower()
    return any(h in t for h in _DESTRUCTIVE_HINTS)


class SonnetReviewer:
    def __init__(self, ask: Callable[[str, str], str]):
        # ask(system, user) -> model text reply
        self._ask = ask

    def review(self, action_desc: str, context: str = "") -> dict:
        """Return {"verdict": "allow"|"deny", "reason": str}.

        Fails CLOSED on any error (deny) — if we can't verify safety of a
        flagged-destructive action, we don't run it.
        """
        user = f"Действие:\n{action_desc}"
        if context:
            user += f"\n\nКонтекст:\n{context}"
        try:
            raw = self._ask(_VERDICT_SYSTEM, user) or ""
        except Exception as e:
            logger.warning("reviewer call failed: %s", e)
            return {"verdict": "deny", "reason": f"ревью недоступно ({e})"}

        verdict = self._parse(raw)
        if verdict is None:
            logger.warning("reviewer returned unparseable verdict: %r", raw[:200])
            return {"verdict": "deny", "reason": "не удалось разобрать вердикт ревьюера"}
        return verdict

    @staticmethod
    def _parse(raw: str) -> dict | None:
        # Extract the first {...} JSON object even if wrapped in prose/fences.
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        v = str(data.get("verdict", "")).lower().strip()
        if v not in ("allow", "deny"):
            return None
        return {"verdict": v, "reason": str(data.get("reason", "")).strip()}
