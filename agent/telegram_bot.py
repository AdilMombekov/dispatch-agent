"""Telegram bot — runs inside the agent and replaces the n8n relay entirely.

Long-polls the Telegram Bot API, routes updates, executes commands via
handlers.dispatch, delivers results (photo / document / text) back to the chat,
and offers an Anthropic-powered chat with tool-calling. Exposes the same
status interface as the old Poller so the tray in main.py is unaffected.
"""
import base64
import html
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import requests

from agent.config import load_config, save_config, get_chrome_profiles, rescan_apps
from agent.handlers import dispatch, run_claude_code_stream
from agent.orchestrator.queue import TaskQueue
from agent.orchestrator.dispatcher import Dispatcher
from agent.orchestrator.router import classify
from agent.orchestrator.budget import Budget, DEFAULT_DAILY_USD
from agent.orchestrator.reviewer import SonnetReviewer, is_destructive
from agent.orchestrator.scheduler import Scheduler, spec_is_valid
from agent.paths import (
    DATA_DIR, STATE_PATH, CHATS_PATH, CLAUDE_TASK_PATH, CLAUDE_RUNS_PATH, INBOX_DIR,
)

logger = logging.getLogger("telegram_bot")

# CODE_DIR — where main.py / VERSION / source files live. Separate from DATA_DIR
# in frozen mode (exe parent vs %APPDATA%\AgentOS). Used for subprocess relaunch
# and reading the bundled VERSION file at startup.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

# DEV drive — where user keeps projects to run claude -p against. Configurable
# via `claude_dev_root` in config.json (path string); default E:\ for legacy
# installs. Allowlist (`claude_dev_folders`) takes precedence when set.
DEFAULT_DEV_ROOT = Path("E:/")
# Subfolders the picker should NEVER offer (system + agent metadata).
DEV_BLOCKLIST = {"$RECYCLE.BIN", "System Volume Information", "Recovery", "$Windows.~BT"}

# Model picker labels → claude -p --model values
CLAUDE_MODELS = [
    ("opus",   "Opus 4.7 · самый умный"),
    ("sonnet", "Sonnet 4.6 · баланс"),
    ("haiku",  "Haiku 4.5 · быстрый"),
]

# Tiered brain for max economy:
#   ROUTER  (Haiku)  — cheap orchestrator: triage, simple actions, routing.
#   ANALYST (Sonnet) — data analysis + writing detailed prompts for Claude Code.
#   heavy work       — delegated to `claude -p` on the SUBSCRIPTION (flat-rate).
ROUTER_MODEL = "claude-haiku-4-5-20251001"
ANALYST_MODEL = "claude-sonnet-4-6"

# ($ per input token, $ per output token)
MODEL_PRICING = {
    ROUTER_MODEL:  (0.80 / 1_000_000, 4.00 / 1_000_000),
    ANALYST_MODEL: (3.00 / 1_000_000, 15.00 / 1_000_000),
}

# System prompt for orchestrator qa-tasks (Haiku, no tools). Kept terse so
# answers fit Telegram without walls of caveats.
QA_SYSTEM_PROMPT = (
    "Ты ассистент Dispatch. Отвечай кратко и по делу, на русском. "
    "Без лишних оговорок и предисловий. Если не уверен — так и скажи."
)

# If the user has been silent in Telegram for this long, the next interaction
# triggers a "refresh agent?" prompt so they don't keep talking to a stale
# in-memory copy that missed source edits made while they were away.
IDLE_OFFER_UPDATE_SEC = 2 * 3600          # 2 hours
UPDATE_OFFER_COOLDOWN_SEC = 10 * 60       # don't re-prompt more than once per 10min

# Substrings (lowercased) that signal a Claude Code subscription usage/rate limit,
# used to offer the user a switch to the next account.
LIMIT_MARKERS = (
    "usage limit", "rate limit", "rate_limit", "ratelimit", "quota", "429",
    "limit reached", "limit exceeded", "out of usage", "превыс", "лимит",
)

SYSTEM_PROMPT = (
    "Ты — Dispatch Agent, ИИ-оркестратор, управляющий локальным Windows ПК пользователя "
    "через Telegram. Твоя задача — выполнять поручения, автоматизировать рутину, писать код "
    "и управлять файловой системой через инструмент queue_agent_command.\n\n"
    "ИНСТРУМЕНТЫ. Доступ к ПК через queue_agent_command с типами: terminal (PowerShell/CMD), "
    "screenshot (снимок экрана для анализа состояния), system-info, launch-app, install, "
    "obsidian-read/obsidian-context (чтение заметок и логов), obsidian-log (запись отчётов), "
    "send-file (отправить файл пользователю), read-document (извлечь текст из ЛЮБОГО документа: "
    "pdf, docx, xlsx, pptx, html, xml, rtf, csv, txt и пр. — используй для чтения присланных/локальных "
    "файлов), chrome-profiles-list. Если задача требует действий на ПК — всегда вызывай инструмент, "
    "а не отвечай текстом.\n\n"
    "ГРАФИЧЕСКИЙ ИНТЕРФЕЙС. Для задач, где нужны клики мышью, окна, формы или визуальная проверка, "
    "используй инструмент control_computer: сначала action='screenshot' чтобы увидеть экран, затем "
    "клики/ввод по шагам (left_click, double_click, type, key, scroll), каждый раз сверяясь с новым "
    "скриншотом. Координаты указывай в пикселях полученного скриншота. Предпочитай терминал, когда "
    "задачу можно решить командой; GUI — когда без интерфейса не обойтись.\n\n"
    "ПРАВИЛА СТАБИЛЬНОСТИ:\n"
    "- Терминал headless: ты НЕ можешь нажимать Y/Enter. Всегда добавляй флаги авто-подтверждения "
    "(-y, --yes, --force, -Force и т.п.). Избегай pause и команд, ждущих ввода.\n"
    "- Большие файлы читай через tail/head/Select-String, чтобы не перегружать контекст.\n"
    "- НЕ меняй кодировку консоли (chcp) — вывод терминала декодируется автоматически.\n\n"
    "ТРЁХУРОВНЕВАЯ ЭКОНОМИЯ (важно). Ты сам — дешёвая модель-роутер (Haiku). Действуй по уровням:\n"
    "1) ПРОСТОЕ (скриншот, команда, запуск, факт) — делай сразу через queue_agent_command, без эскалации.\n"
    "2) СЛОЖНОЕ ПОНИМАНИЕ или нужен качественный промпт — вызови analyze (это Sonnet): передай цель и "
    "собранный контекст, получи анализ или готовый детальный промпт. Не дёргай analyze по мелочам — он дороже.\n"
    "3) ТЯЖЁЛАЯ РАБОТА (написание/правка кода, работа с файлами проекта, многошаговые операции) — делегируй "
    "run_claude_code (это `claude -p` на ПОДПИСКЕ, НЕ тратит платные API-токены, работает автономно). Давай "
    "максимально подробный промпт (при необходимости составь его через analyze). У каждого запуска есть "
    "фиксированный оверхед, поэтому объединяй работу в ОДИН содержательный запуск, а не дроби на много мелких.\n"
    "ВАЖНО: если задача про код или файлы проекта — НЕ редактируй сам через terminal и НЕ лезь в GUI/скриншоты. "
    "Сразу делегируй run_claude_code с полным промптом (включая чтение файла, правку и запуск/проверку в одном "
    "задании). control_computer — только для задач, которые реально требуют мыши/окон, не для кода.\n\n"
    "АЛГОРИТМ: пойми задачу → выбери минимально достаточный уровень → выполни → проверь результат → "
    "кратко отчитайся, не дублируя большие логи. Всегда предпочитай самый дешёвый уровень, которого хватает.\n\n"
    "СТИЛЬ: по-русски, кратко и проактивно. Не спрашивай разрешения на безопасные действия. "
    "Опасные действия (удаление файлов, остановка служб) — сначала подтверди у пользователя."
)

ANALYST_SYSTEM = (
    "Ты — аналитик и составитель промптов для Claude Code. Тебе дают ЦЕЛЬ и собранный КОНТЕКСТ. "
    "Верни либо чёткий аналитический вывод, либо ГОТОВЫЙ подробный промпт для исполнителя: с конкретными "
    "шагами, путями к файлам, критериями готовности и проверки. Без воды, по делу, по-русски."
)

TOOLS = [{
    "name": "queue_agent_command",
    "description": (
        "Run a command on the local Windows PC. Use for screenshots, terminal "
        "commands, launching apps, installing software, system info, or Obsidian notes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["screenshot", "terminal", "obsidian-log", "obsidian-read",
                         "obsidian-context", "system-info", "launch-app", "install",
                         "chrome-profiles-list", "send-file", "read-document"],
                "description": "Command type",
            },
            "payload": {
                "type": "object",
                "description": "Command parameters",
                "properties": {
                    "cmd":      {"type": "string", "description": "terminal: shell command"},
                    "text":     {"type": "string", "description": "obsidian-log: text to append"},
                    "lines":    {"type": "number", "description": "obsidian-read: number of lines"},
                    "note":     {"type": "string", "description": "obsidian: note name"},
                    "app":      {"type": "string", "description": "launch-app: app key from config"},
                    "url":      {"type": "string", "description": "install: download URL"},
                    "filepath": {"type": "string",
                                 "description": "send-file / read-document: absolute path. "
                                                "read-document извлекает текст из pdf/docx/xlsx/"
                                                "pptx/html/xml/rtf/txt и пр."},
                },
            },
        },
        "required": ["type"],
    },
}, {
    "name": "control_computer",
    "description": (
        "Control the PC's mouse and keyboard and see the screen, like a human. "
        "Coordinates (x, y) are in the pixel space of the screenshots you receive "
        "(not the real resolution). After every action you get a fresh screenshot. "
        "Start with action 'screenshot' to see the screen, then click/type step by step."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["screenshot", "left_click", "right_click", "double_click",
                         "move", "type", "key", "scroll"],
                "description": "What to do",
            },
            "x": {"type": "number", "description": "X in screenshot pixels (click/move)"},
            "y": {"type": "number", "description": "Y in screenshot pixels (click/move)"},
            "text": {"type": "string", "description": "Text to type (action=type)"},
            "keys": {"type": "string", "description": "Key or hotkey e.g. 'enter', 'ctrl+c' (action=key)"},
            "amount": {"type": "number", "description": "Scroll amount, negative = down (action=scroll)"},
        },
        "required": ["action"],
    },
}, {
    "name": "analyze",
    "description": (
        "Глубокий анализ и/или написание подробного промпта для Claude Code. Работает на Sonnet "
        "(дороже тебя-Haiku — вызывай только для действительно сложных задач). Передай цель и собранный "
        "контекст; вернётся анализ или готовый детальный промпт, который дальше отдаётся в run_claude_code."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Что проанализировать или какой промпт составить"},
            "context": {"type": "string", "description": "Собранные данные/факты/выдержки файлов (необязательно)"},
        },
        "required": ["goal"],
    },
}, {
    "name": "run_claude_code",
    "description": (
        "Запусти тяжёлую задачу (написание/правка кода, работа с файлами проекта, многошаговые операции) "
        "в Claude Code через `claude -p`. Работает на ПОДПИСКЕ — не тратит платные API-токены и выполняет "
        "всё АВТОНОМНО (правит файлы и запускает команды сам). Дай максимально подробный промпт с путями, "
        "шагами и критериями готовности (лучше — составленный через analyze). У каждого запуска фиксированный "
        "оверхед, поэтому объединяй работу в один содержательный запуск, а не дроби."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Подробный промпт-задание для Claude Code"},
            "cwd": {"type": "string", "description": "Рабочая папка проекта (абсолютный путь), если применимо"},
            "model": {"type": "string", "enum": ["sonnet", "opus"],
                      "description": "sonnet (по умолчанию) или opus для самых сложных задач"},
        },
        "required": ["prompt"],
    },
}]

# Top-level menu: 4 sections. Navigation is ephemeral (edit-in-place) so the
# chat doesn't fill with stale menus — see _edit_menu / nav:* callbacks.
MAIN_MENU = {"inline_keyboard": [
    [{"text": "💬 Чат", "callback_data": "nav:chat"},
     {"text": "🤝 Коворк", "callback_data": "nav:cowork"}],
    [{"text": "💻 Код", "callback_data": "nav:code"},
     {"text": "⚙️ Настройки", "callback_data": "nav:settings"}],
]}

MAIN_MENU_TEXT = "🤖 *Dispatch Agent* — выбери раздел:"

# Code / PC-control section — reuses existing action callbacks.
CODE_MENU = {"inline_keyboard": [
    [{"text": "🤖 Запустить Claude Code", "callback_data": "claude:start"}],
    [{"text": "⌨️ Терминал", "callback_data": "menu:terminal"}],
    [{"text": "🚀 Приложения", "callback_data": "menu:apps"},
     {"text": "📦 Установить ПО", "callback_data": "menu:install"}],
    [{"text": "📸 Скриншот", "callback_data": "cmd:screenshot"},
     {"text": "💻 О системе", "callback_data": "cmd:system-info"}],
    [{"text": "🌐 Профили Chrome", "callback_data": "menu:chrome"}],
    [{"text": "⬅️ Назад", "callback_data": "nav:main"}],
]}

CHAT_TEXT = (
    "💬 *Чат*\n\nПросто пиши задачу текстом — Haiku-роутер сам поймёт: ответ, "
    "терминал, скриншот, чтение файла, клики мышью. Тяжёлое уходит в `claude -p`. "
    "Картинки распознаются через vision."
)
CHAT_MENU = {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "nav:main"}]]}

APPS_MENU = {"inline_keyboard": [
    [{"text": "Cursor", "callback_data": "app:cursor"},
     {"text": "VS Code", "callback_data": "app:vscode"}],
    [{"text": "Claude Code", "callback_data": "app:claude-code"},
     {"text": "Antigravity", "callback_data": "app:antigravity"}],
    [{"text": "Terminal", "callback_data": "app:terminal"}],
]}

# Single source of truth for slash commands. Feeds BOTH Telegram's quick-command
# menu (setMyCommands) and /help, so they can never drift apart.
# (command_without_slash, description, help_group)
BOT_COMMANDS = [
    ("start",      "главное меню",                                  "Основные"),
    ("help",       "все команды (это сообщение)",                   "Основные"),
    ("clean",      "убрать неважные сообщения (Haiku решает)",      "Основные"),
    ("q",          "задача в очередь — Haiku сам определит тип",    "Оркестратор"),
    ("c",          "code-задача через claude CLI: /c <что сделать>", "Оркестратор"),
    ("click",      "GUI-задача (computer-use): /click <что сделать>", "Оркестратор"),
    ("stop",       "⛔ прервать текущую GUI-задачу",                 "Оркестратор"),
    ("tasks",      "список задач и статусы",                        "Оркестратор"),
    ("cancel",     "отменить задачу: /cancel <id>",                 "Оркестратор"),
    ("dispatch",   "оркестратор вкл/выкл: /dispatch on|off",        "Оркестратор"),
    ("every",      "повтор: /every 30m <задача>",                   "Расписание"),
    ("daily",      "ежедневно: /daily 08:00 <задача>",              "Расписание"),
    ("crons",      "список расписаний",                             "Расписание"),
    ("uncron",     "убрать расписание: /uncron <id>",               "Расписание"),
    ("claude",     "Claude Code в папке проекта (стрим)",           "Claude Code"),
    ("history",    "последние 10 запусков Claude Code",             "Claude Code"),
    ("skills",     "включить/выключить инструменты AI",             "Claude Code"),
    ("spend",      "траты по Anthropic API + лимит",                "Деньги"),
    ("setlimit",   "лимит трат: /setlimit 10.0",                    "Деньги"),
    ("resetspend", "обнулить счётчик трат",                         "Деньги"),
    ("rescan",     "найти новые установленные приложения",          "Сервис"),
    ("update",     "перезапустить агента (свежий код)",             "Сервис"),
]


class TelegramBot:
    def __init__(self, on_status_change=None):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_status_change = on_status_change or (lambda s: None)
        self._status = "stopped"
        self._cfg: dict = {}
        self._api = ""
        self._history: dict[str, list] = {}    # chat_id -> message list (AI)
        self._pending: dict[str, dict] = {}     # chat_id -> {"type": ...}
        self._pending_delegation: dict[str, dict] = {}  # chat_id -> delegation to retry after switch
        self._last_file: dict[str, str] = {}    # chat_id -> path of last uploaded file
        # When an image is uploaded, stash the bytes for the NEXT AI turn so the
        # model gets it as a real vision block (read-document on a JPG is useless).
        # One-shot: popped after the next turn so it doesn't bloat history.
        self._pending_image: dict[str, dict] = {}  # chat_id -> {"b64", "media_type", "name"}
        self._chat_locks: dict[str, threading.Lock] = {}  # chat_id -> lock (serialize AI turns)
        # Idle-refresh: track last interaction so a 2h+ gap triggers a "restart?" offer
        # (the in-memory bot won't pick up source edits otherwise).
        self._last_seen: dict[str, float] = {}
        self._update_offered_at: dict[str, float] = {}
        # End-of-turn message cleanup: collect (msg_id, role) for everything sent
        # during an AI turn; at the end we keep the user-facing answer and delete the rest.
        # Roles: 'text' (model text), 'result' (tool output), 'status', 'error'.
        self._turn_track: dict[str, list[tuple[int, str]]] = {}
        # Rolling log of recent message ids (bot + user) per chat, for /clean.
        # In a private chat a bot may delete both its own and incoming messages.
        self._msg_log: dict[str, deque] = {}
        # Tracks the active `claude -p` subprocess per chat so the user can hit
        # ❌ Отменить on the streamed message and kill it without waiting for
        # the watchdog (30-minute default).
        self._active_claude_proc: dict[str, object] = {}
        self._state = self._load_state()

        # Orchestrator: task queue + worker. Executors (Haiku/CLI/computer-use)
        # are registered in later steps; until then the dispatcher stub-completes
        # tasks so the queue/commands are usable. Gated by state.dispatch_enabled.
        self._queue = TaskQueue()
        # Daily orchestrator budget (separate from monthly chat meter). Limit
        # read live from config so /setlimit-style changes apply without restart.
        self._budget = Budget(
            self._queue,
            limit_provider=lambda: float(
                (self._cfg.get("daily_budget_usd") if self._cfg else None) or DEFAULT_DAILY_USD),
        )
        self._reviewer = SonnetReviewer(ask=self._review_ask)
        self._dispatcher = Dispatcher(
            self._queue,
            is_enabled=lambda: bool(self._state.get("dispatch_enabled", False)),
            report=lambda chat_id, text: self._send_message(chat_id, text),
            budget_ok=self._budget.allowed,
            on_budget_block=self._notify_budget_block,
        )
        # qa → Haiku (P3.10), code → claude CLI (P3.11). click_gui/scheduled
        # wired in later steps; until then the dispatcher stub-completes them.
        self._dispatcher.register("qa", self._qa_executor)
        self._dispatcher.register("code", self._code_executor)
        self._dispatcher.register("click_gui", self._click_executor)
        # Recurring tasks (P3.14): own lightweight scheduler, no extra dependency.
        self._scheduler = Scheduler(self._queue, classify=classify)

    # ── State persistence (spend, active account) ───────────────────────────

    def _load_state(self) -> dict:
        defaults = {
            "spend": {"total_usd": 0.0, "limit_usd": 5.0, "anthropic_usd": 0.0, "openai_usd": 0.0},
            "active_account": 1,
            # Orchestrator master switch. OFF by default: the user opts in with
            # /dispatch on. While OFF, /q tasks queue up but nothing executes.
            "dispatch_enabled": False,
        }
        try:
            if STATE_PATH.exists():
                loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                defaults.update(loaded)
                defaults["spend"] = {**{"total_usd": 0.0, "limit_usd": 5.0,
                                        "anthropic_usd": 0.0, "openai_usd": 0.0},
                                     **loaded.get("spend", {})}
        except Exception as e:
            logger.warning(f"state load failed: {e}")
        return defaults

    def _save_state(self):
        try:
            STATE_PATH.write_text(json.dumps(self._state, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception as e:
            logger.warning(f"state save failed: {e}")

    def _persist_chats_for_ui(self):
        """Dump a UI-friendly view of recent chat history for the Electron Control Center.

        Debounced: coalesce writes that arrive within 2s of the last persist
        (the UI polls on ~5s intervals, so missing one mid-burst is invisible).
        """
        now = time.time()
        if now - getattr(self, "_chats_last_write", 0.0) < 2.0:
            return
        self._chats_last_write = now
        try:
            chats = []
            for cid, hist in self._history.items():
                msgs = []
                for m in hist[-12:]:
                    txt = m.get("content")
                    if isinstance(txt, list):  # tool-use / multi-block — flatten to plain text
                        parts = []
                        for blk in txt:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                parts.append(blk.get("text", ""))
                        txt = " ".join(p for p in parts if p).strip()
                    if not isinstance(txt, str):
                        txt = str(txt)
                    msgs.append({"role": m.get("role", "user"), "text": txt[:600]})
                chats.append({"chat_id": str(cid), "messages": msgs})
            payload = {"chats": chats, "updated_at": time.time()}
            CHATS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception as e:
            logger.warning(f"chats persist failed: {e}")

    # ── Public API (mirrors Poller) ──────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="TelegramBot")
        self._thread.start()
        self._sched_thread = threading.Thread(target=self._scheduler_run, daemon=True,
                                              name="AppScanScheduler")
        self._sched_thread.start()
        self._dispatcher.start()
        self._scheduler.start()

    def stop(self):
        self._stop.set()
        try:
            self._dispatcher.stop()
        except Exception as e:
            logger.warning(f"dispatcher stop failed: {e}")
        try:
            self._scheduler.stop()
        except Exception as e:
            logger.warning(f"scheduler stop failed: {e}")

    @property
    def status(self) -> str:
        return self._status

    # ── Core loop ─────────────────────────────────────────────────────────────

    def _run(self):
        self._cfg = load_config()
        # sync spend limit from config if present
        try:
            self._state["spend"]["limit_usd"] = float(
                self._cfg.get("api_limit_usd", self._state["spend"]["limit_usd"]))
        except Exception:
            pass

        token = (self._cfg.get("telegram_bot_token") or "").strip()
        if not token:
            logger.error("no telegram_bot_token in config — bot cannot start")
            self._set_status("error")
            return
        self._api = f"https://api.telegram.org/bot{token}"

        # switch the bot from any webhook to long-polling
        try:
            self._tg("deleteWebhook", {"drop_pending_updates": False})
        except Exception as e:
            logger.warning(f"deleteWebhook failed: {e}")

        # Register the quick-command menu (the "/" list in Telegram) so the user
        # doesn't have to remember/type commands.
        self._set_bot_commands()

        self._set_status("idle")
        logger.info("Telegram bot polling started")
        # After a /update self-restart, greet the user so they can see the new
        # process is live (the old one died silently after "вернусь через ~5 секунд").
        self._maybe_send_welcome_back()
        # Persist offset across restarts. Without this, os._exit() in
        # _self_restart drops the in-memory offset, the next instance refetches
        # the same callback, and we loop forever on a stale "Update agent" click.
        offset = self._state.get("tg_offset")
        while not self._stop.is_set():
            try:
                params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                resp = self._tg("getUpdates", params, timeout=35)
                for upd in resp.get("result", []):
                    offset = upd["update_id"] + 1
                    self._state["tg_offset"] = offset
                    try:
                        self._save_state()
                    except Exception:
                        pass
                    try:
                        self._handle_update(upd)
                    except Exception as e:
                        logger.error(f"update handling error: {e}")
            except requests.exceptions.Timeout:
                continue
            except requests.exceptions.ConnectionError:
                logger.warning("getUpdates: connection error")
                self._set_status("error")
                self._stop.wait(3)
                self._set_status("idle")
            except Exception as e:
                logger.error(f"getUpdates error: {e}")
                self._stop.wait(3)

        self._set_status("stopped")

    # ── Telegram API helpers ──────────────────────────────────────────────────

    def _tg(self, method: str, params: dict | None = None, timeout: int = 15, files=None) -> dict:
        url = f"{self._api}/{method}"
        if files:
            resp = requests.post(url, data=params or {}, files=files, timeout=timeout)
        else:
            resp = requests.post(url, json=params or {}, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            logger.warning(f"tg {method} -> {data.get('description')}")
        return data

    def _log_msg(self, chat_id, msg_id, role: str, text: str) -> None:
        """Append a message to the per-chat rolling buffer used by /clean."""
        if not msg_id:
            return
        buf = self._msg_log.setdefault(str(chat_id), deque(maxlen=60))
        buf.append({"id": int(msg_id), "role": role, "text": (text or "")[:160]})

    def _send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        p = {"chat_id": chat_id, "text": text}
        if reply_markup:
            p["reply_markup"] = reply_markup
        if parse_mode:
            p["parse_mode"] = parse_mode
        resp = self._tg("sendMessage", p)
        try:
            self._log_msg(chat_id, (resp.get("result") or {}).get("message_id"), "bot", text)
        except Exception:
            pass
        return resp

    def _edit_menu(self, chat_id, message_id, text, reply_markup=None,
                   parse_mode="Markdown"):
        """Edit a menu message in place (ephemeral navigation: one message that
        morphs instead of a pile of stale menus). Falls back to sending a new
        message if the edit fails (e.g. message too old / identical)."""
        p = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            p["reply_markup"] = reply_markup
        if parse_mode:
            p["parse_mode"] = parse_mode
        r = self._tg("editMessageText", p)
        if isinstance(r, dict) and r.get("ok"):
            return r
        # Fallback: couldn't edit — send fresh so the user still gets the menu.
        return self._send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)

    def _send_photo(self, chat_id, jpeg_bytes, caption=None):
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        files = {"photo": ("screenshot.jpg", jpeg_bytes, "image/jpeg")}
        return self._tg("sendPhoto", data, files=files)

    def _send_document(self, chat_id, file_bytes, filename, caption=None,
                       mime="application/octet-stream"):
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1000]
        files = {"document": (filename, file_bytes, mime)}
        return self._tg("sendDocument", data, files=files)

    def _answer_callback(self, cb_id, text=None):
        """Returns (ok: bool, expired: bool). expired=True if Telegram says the
        query is too old — caller should skip the associated action so we don't
        re-trigger a destructive op (e.g. self-restart) from a stale click."""
        p = {"callback_query_id": cb_id}
        if text:
            p["text"] = text
        try:
            r = self._tg("answerCallbackQuery", p)
        except Exception:
            return (False, False)
        if isinstance(r, dict) and r.get("ok"):
            return (True, False)
        desc = ""
        if isinstance(r, dict):
            desc = str(r.get("description", "")).lower()
        expired = ("query is too old" in desc) or ("query id is invalid" in desc)
        return (False, expired)

    # ── End-of-turn message cleanup ─────────────────────────────────────────────

    def _track(self, chat_id, role: str, resp: dict | None) -> dict | None:
        """Record a sent message for end-of-turn cleanup. Returns resp unchanged."""
        if not isinstance(resp, dict):
            return resp
        try:
            mid = (resp.get("result") or {}).get("message_id")
        except Exception:
            mid = None
        if mid:
            self._turn_track.setdefault(str(chat_id), []).append((int(mid), role))
        return resp

    def _delete_message(self, chat_id, msg_id) -> None:
        """Best-effort: deleteMessage often fails on messages >48h old or already gone."""
        try:
            self._tg("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        except Exception as e:
            logger.debug(f"deleteMessage {msg_id} failed: {e}")

    def _cleanup_turn(self, chat_id) -> None:
        """Sweep an AI turn: keep the message that constitutes the answer, drop the rest.

        Priority for the kept message:
          1) the LAST text message  (model summarised) — most common case
          2) else, the LAST tool_result (model didn't talk, the screenshot/output IS the answer)
          3) else, the LAST error (so the user sees why the turn produced nothing)

        Disabled when telegram_auto_clean=false in config (default: on).
        """
        if not (self._cfg.get("telegram_auto_clean", True)):
            self._turn_track.pop(str(chat_id), None)
            return
        msgs = self._turn_track.pop(str(chat_id), [])
        if not msgs:
            return
        keep_idx = None
        for pref in ("text", "result", "error"):
            last = -1
            for i, (_, r) in enumerate(msgs):
                if r == pref:
                    last = i
            if last >= 0:
                keep_idx = last
                break
        if keep_idx is None:
            return
        for i, (mid, _) in enumerate(msgs):
            if i != keep_idx:
                self._delete_message(chat_id, mid)

    # ── Update routing ─────────────────────────────────────────────────────────

    def _is_authorized(self, chat_id) -> bool:
        """Owner gate. config 'owner_chat_id' (if set) is authoritative; otherwise
        trust-on-first-use: the first chat to message claims ownership (persisted)."""
        configured = self._cfg.get("owner_chat_id")
        if configured:
            return chat_id == configured
        owner = self._state.get("owner_chat_id")
        if owner is None:
            self._state["owner_chat_id"] = chat_id
            self._save_state()
            logger.info(f"owner chat claimed (trust-on-first-use): {chat_id}")
            return True
        return chat_id == owner

    def _handle_update(self, upd: dict):
        chat_id = None
        if "callback_query" in upd:
            chat_id = upd["callback_query"]["message"]["chat"]["id"]
        elif "message" in upd:
            chat_id = upd["message"]["chat"]["id"]
        if chat_id is None:
            return
        # Owner gate: only the authorized chat may control the PC. Anyone else is
        # silently ignored (the bot has full terminal/file access — never obey strangers).
        if not self._is_authorized(chat_id):
            logger.warning(f"ignoring update from unauthorized chat {chat_id}")
            return

        # Idle-refresh: detect a long silence BEFORE handling the update so we can
        # tack on the "restart?" offer after the message is processed normally.
        now = time.time()
        scid = str(chat_id)
        prev_seen = self._last_seen.get(scid)
        self._last_seen[scid] = now
        should_offer_refresh = (
            prev_seen is not None
            and (now - prev_seen) > IDLE_OFFER_UPDATE_SEC
            and (now - self._update_offered_at.get(scid, 0)) > UPDATE_OFFER_COOLDOWN_SEC
        )

        if "callback_query" in upd:
            self._handle_callback(upd["callback_query"])
        elif "message" in upd:
            msg = upd["message"]
            # Log the incoming user message for /clean (id + text preview).
            self._log_msg(chat_id, msg.get("message_id"), "user", msg.get("text", ""))
            if "voice" in msg:
                self._send_message(msg["chat"]["id"],
                                   "🎤 Голос пока не поддерживается — напиши текстом или /start.")
            elif any(k in msg for k in ("document", "photo", "audio", "video")):
                self._handle_incoming_file(msg)
            elif "text" in msg:
                text = msg["text"]
                if text.startswith("/"):
                    self._handle_command(msg)
                else:
                    self._handle_text(msg)

        if should_offer_refresh:
            self._update_offered_at[scid] = now
            try:
                self._offer_agent_refresh(chat_id, idle_hours=(now - prev_seen) / 3600.0)
            except Exception as e:
                logger.warning(f"offer_agent_refresh failed: {e}")

    # ── Slash commands ─────────────────────────────────────────────────────────

    def _handle_command(self, msg: dict):
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()
        sp = self._state["spend"]
        # Any slash command interrupts a half-finished input flow (e.g. user opened
        # /claude but then typed /spend instead of picking a folder). Without this,
        # the next free-text message would be misrouted into the stale pending bucket.
        self._pending.pop(str(chat_id), None)

        # Strict first-token match — `/spendmore` shouldn't fall into /spend, and
        # `/updates` (typo) shouldn't silently trigger a self-restart.
        parts = text.split()
        cmd = parts[0].lower() if parts else "/start"

        if cmd == "/setlimit":
            try:
                val = float(parts[1])
                if val <= 0:
                    raise ValueError
                sp["limit_usd"] = val
                self._save_state()
                self._send_message(chat_id, f"✅ Лимит трат: ${val:.2f}")
            except Exception:
                self._send_message(chat_id, "❌ Использование: /setlimit 10.0")
        elif cmd == "/resetspend":
            sp["total_usd"] = sp["anthropic_usd"] = sp["openai_usd"] = 0.0
            self._save_state()
            self._send_message(chat_id, "✅ Счётчик трат обнулён ($0.00)")
        elif cmd == "/spend":
            limit = sp["limit_usd"] or 1
            pct = min(sp["total_usd"] / limit * 100, 100)
            filled = int(pct // 10)
            bar = "█" * filled + "░" * (10 - filled)
            self._send_message(
                chat_id,
                f"💰 Траты\n{bar} {pct:.1f}%\n"
                f"Всего: ${sp['total_usd']:.4f} / ${sp['limit_usd']:.2f}\n"
                f"Anthropic: ${sp['anthropic_usd']:.4f}")
        elif cmd == "/rescan":
            self._trigger_rescan(chat_id)
        elif cmd == "/dispatch":
            self._cmd_dispatch_toggle(chat_id, parts[1].lower() if len(parts) > 1 else None)
        elif cmd == "/q":
            self._cmd_enqueue(chat_id, text[len(parts[0]):].strip())
        elif cmd == "/c":
            self._cmd_enqueue(chat_id, text[len(parts[0]):].strip(), force_kind="code")
        elif cmd == "/click":
            self._cmd_enqueue(chat_id, text[len(parts[0]):].strip(), force_kind="click_gui")
        elif cmd == "/stop":
            self._state["dispatch_emergency_stop"] = True
            self._save_state()
            self._send_message(chat_id, "⛔ Стоп-флаг поставлен — текущая GUI-задача прервётся между шагами.")
        elif cmd == "/tasks":
            self._cmd_list_tasks(chat_id)
        elif cmd == "/clean":
            self._cmd_clean(chat_id)
        elif cmd == "/cancel":
            self._cmd_cancel_task(chat_id, parts[1] if len(parts) > 1 else None)
        elif cmd == "/every":
            # /every 30m <prompt>  |  /every 2h <prompt>
            self._cmd_schedule(chat_id, "every", parts[1:] )
        elif cmd == "/daily":
            # /daily 08:00 <prompt>
            self._cmd_schedule(chat_id, "daily", parts[1:])
        elif cmd == "/crons":
            self._cmd_list_crons(chat_id)
        elif cmd == "/uncron":
            self._cmd_uncron(chat_id, parts[1] if len(parts) > 1 else None)
        elif cmd == "/claude":
            self._claude_show_folders(chat_id)
        elif cmd == "/update":
            self._self_restart(chat_id)
        elif cmd == "/help":
            self._show_help(chat_id)
        elif cmd == "/history":
            self._show_claude_history(chat_id)
        elif cmd == "/skills":
            self._show_skills_toggle(chat_id)
        elif cmd == "/start":
            ver = self._get_version()
            self._send_message(
                chat_id,
                "🤖 *Dispatch Agent*\nУправление этим ПК.\n\n"
                "Нажми кнопку или просто напиши задачу текстом.\n\n"
                f"🔖 _v {ver}_",
                reply_markup=MAIN_MENU, parse_mode="Markdown")
        else:
            # Unknown /command — explicit reply instead of silently showing the menu.
            self._send_message(
                chat_id,
                f"❌ Неизвестная команда: `{cmd}`\n\n"
                "Доступно: /start /q /c /tasks /cancel /dispatch /claude /history /skills "
                "/update /spend /setlimit /resetspend /rescan /help",
                parse_mode="Markdown")

    # ── Orchestrator commands (P3.9) ─────────────────────────────────────────

    def _cmd_dispatch_toggle(self, chat_id, arg: str | None):
        """/dispatch [on|off] — master switch for the task orchestrator."""
        if arg in ("on", "вкл", "1", "true"):
            self._state["dispatch_enabled"] = True
            self._save_state()
            self._send_message(chat_id, "🟢 Dispatch ВКЛЮЧЕН — задачи из очереди выполняются.")
        elif arg in ("off", "выкл", "0", "false"):
            self._state["dispatch_enabled"] = False
            self._save_state()
            self._send_message(chat_id, "⏸ Dispatch ВЫКЛЮЧЕН — задачи копятся, но не выполняются.")
        else:
            on = bool(self._state.get("dispatch_enabled", False))
            queued = len(self._queue.list(status="queued", limit=100))
            spent, limit = self._budget.today_spent(), self._budget.limit()
            self._send_message(
                chat_id,
                f"🤖 Dispatch: {'🟢 ВКЛ' if on else '⏸ ВЫКЛ'}\n"
                f"В очереди: {queued}\n"
                f"Бюджет сегодня: ${spent:.4f} / ${limit:.2f}\n\n"
                "Команды: /dispatch on · /dispatch off")

    def _cmd_enqueue(self, chat_id, prompt: str, force_kind: str | None = None):
        """/q <prompt> — classify and queue a task. force_kind bypasses the
        router (used by /c → code)."""
        if not prompt:
            self._send_message(chat_id, "Использование: /q <что сделать>\nНапример: /q сколько будет 2+2")
            return
        kind = force_kind or classify(prompt)
        task_id = self._queue.enqueue(chat_id, kind, prompt)
        enabled = bool(self._state.get("dispatch_enabled", False))
        tail = "" if enabled else "\n⏸ Dispatch выключен — включи /dispatch on, чтобы выполнить."
        self._send_message(chat_id, f"🟢 Задача #{task_id} ({kind}) в очереди.{tail}")

    _STATUS_EMOJI = {
        "queued": "⏳", "running": "🔄", "done": "✅",
        "failed": "❌", "cancelled": "🚫", "scheduled": "📅",
    }

    def _cmd_list_tasks(self, chat_id):
        """/tasks — show the last 10 tasks with status."""
        tasks = self._queue.list(limit=10)
        if not tasks:
            self._send_message(chat_id, "Очередь пуста. Поставить задачу: /q <что сделать>")
            return
        lines = ["📋 Последние задачи:"]
        for t in tasks:
            em = self._STATUS_EMOJI.get(t.status, "•")
            prompt = (t.prompt[:40] + "…") if len(t.prompt) > 40 else t.prompt
            lines.append(f"{em} #{t.id} [{t.kind}] {prompt}")
        lines.append("\nОтменить: /cancel <id>")
        self._send_message(chat_id, "\n".join(lines))

    def _cmd_cancel_task(self, chat_id, arg: str | None):
        """/cancel <id> — cancel a queued/running task."""
        try:
            task_id = int(arg)
        except (TypeError, ValueError):
            self._send_message(chat_id, "Использование: /cancel <номер задачи>")
            return
        t = self._queue.get(task_id)
        if not t:
            self._send_message(chat_id, f"Задача #{task_id} не найдена.")
            return
        if t.chat_id != chat_id:
            self._send_message(chat_id, f"Задача #{task_id} не твоя.")
            return
        if self._queue.cancel(task_id):
            self._send_message(chat_id, f"🚫 Задача #{task_id} отменена.")
        else:
            self._send_message(chat_id, f"Задача #{task_id} уже завершена ({t.status}), отменять нечего.")

    def _cmd_clean(self, chat_id):
        """/clean — Haiku picks unimportant messages from the rolling buffer and
        we delete them (menus, status pings, acks, stale errors). Substantive
        Q&A and results are kept."""
        buf = list(self._msg_log.get(str(chat_id), []))
        if len(buf) < 2:
            self._send_message(chat_id, "Чистить нечего — буфер пуст.")
            return
        try:
            noise = self._clean_classify(buf)
        except Exception as e:
            logger.warning("clean classify failed: %s", e)
            self._send_message(chat_id, f"Не смог понять что чистить: {e}")
            return
        if not noise:
            self._send_message(chat_id, "🧹 Haiku не нашёл явного шума — ничего не удалил.")
            return
        deleted = 0
        for entry in buf:
            if entry["id"] in noise:
                self._delete_message(chat_id, entry["id"])
                deleted += 1
        # Drop deleted ids from the buffer so a second /clean doesn't retry them.
        self._msg_log[str(chat_id)] = deque(
            (e for e in buf if e["id"] not in noise), maxlen=60)
        self._send_message(chat_id, f"🧹 Убрал {deleted} неважных сообщений.")

    def _clean_classify(self, buf: list) -> set[int]:
        """Ask Haiku which message ids in the buffer are noise. Returns a set of
        ids; empty set on unparseable output (fail-safe: delete nothing)."""
        api_key = self._active_anthropic_key()
        if not api_key:
            raise RuntimeError("нет anthropic_api_keys")
        listing = "\n".join(f'{e["id"]} [{e["role"]}] {e["text"]}' for e in buf)
        system = (
            "Ты чистишь личный Telegram-чат от шума. Дан список недавних сообщений "
            "в формате '<id> [кто] текст'. Верни СТРОГО JSON-массив id (числа), "
            "которые НЕважны и их можно удалить: меню и кнопки, статусы "
            "(🔄/⏳/✅ без содержания), приветствия, подтверждения, устаревшие "
            "ошибки, служебные команды вроде /start. НЕ удаляй содержательные "
            "вопросы пользователя и полезные ответы/результаты. "
            "Ответ — только JSON-массив чисел, без пояснений.")
        resp = self._anthropic(
            api_key, [{"role": "user", "content": listing}],
            model=ROUTER_MODEL, system=system, tools=None, max_tokens=500)
        usage = resp.get("usage", {}) or {}
        self._add_cost(ROUTER_MODEL, usage)
        self._save_state()
        raw = "\n".join(
            b.get("text", "") for b in (resp.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "text")
        lo, hi = raw.find("["), raw.rfind("]")
        if lo < 0 or hi <= lo:
            return set()
        try:
            ids = json.loads(raw[lo:hi + 1])
        except json.JSONDecodeError:
            return set()
        out: set[int] = set()
        for x in ids:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                pass
        return out

    def _cmd_schedule(self, chat_id, mode: str, parts: list):
        """/every <N>m|h <prompt> and /daily <HH:MM> <prompt> — create a
        recurring template (P3.14)."""
        if len(parts) < 2:
            ex = "/every 30m проверь почту" if mode == "every" else "/daily 08:00 пришли сводку"
            self._send_message(chat_id, f"Использование: {ex}")
            return
        spec = f"{mode} {parts[0]}"
        prompt = " ".join(parts[1:]).strip()
        if not spec_is_valid(spec):
            hint = ("интервал: 30m или 2h" if mode == "every" else "время: 08:00")
            self._send_message(chat_id, f"❌ Неверный формат ({hint}).")
            return
        if not prompt:
            self._send_message(chat_id, "❌ Пустая задача.")
            return
        tid = self._queue.enqueue(chat_id, classify(prompt), prompt, cron=spec)
        self._scheduler.reload()
        self._send_message(chat_id, f"📅 Расписание #{tid} создано: `{spec}` → {prompt}",
                           parse_mode="Markdown")

    def _cmd_list_crons(self, chat_id):
        templates = self._queue.list(status="scheduled", limit=50)
        mine = [t for t in templates if t.chat_id == chat_id]
        if not mine:
            self._send_message(chat_id, "Расписаний нет. Создать: /every 30m <…> или /daily 08:00 <…>")
            return
        lines = ["📅 Расписания:"]
        for t in mine:
            prompt = (t.prompt[:40] + "…") if len(t.prompt) > 40 else t.prompt
            lines.append(f"#{t.id} `{t.cron}` — {prompt}")
        lines.append("\nУбрать: /uncron <id>")
        self._send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_uncron(self, chat_id, arg: str | None):
        try:
            tid = int(arg)
        except (TypeError, ValueError):
            self._send_message(chat_id, "Использование: /uncron <id>")
            return
        t = self._queue.get(tid)
        if not t or t.status != "scheduled" or t.chat_id != chat_id:
            self._send_message(chat_id, f"Расписание #{tid} не найдено.")
            return
        self._queue.cancel(tid)
        self._scheduler.reload()
        self._send_message(chat_id, f"🗑 Расписание #{tid} убрано.")

    # ── Orchestrator executors (P3.10+) ──────────────────────────────────────

    def _active_anthropic_key(self) -> str | None:
        """Return the API key for the active account, or None if unconfigured.
        The dispatcher may run before _run() loads _cfg, so fall back to a fresh
        config read."""
        keys = self._cfg.get("anthropic_api_keys") or []
        if not keys:
            try:
                keys = (load_config().get("anthropic_api_keys") or [])
            except Exception:
                keys = []
        if not keys:
            return None
        idx = int(self._state.get("active_account", 1)) - 1
        if not (0 <= idx < len(keys)):
            idx = 0
        return keys[idx]

    def _qa_executor(self, task) -> str:
        """Dispatcher executor for kind=qa: answer via Haiku (no tools).

        Reuses the bot's _anthropic HTTP path + _add_cost meter so there is one
        spend tracker, and also records a per-task run for /tasks accounting.
        """
        sp = self._state["spend"]
        if sp.get("limit_usd", 0) > 0 and sp.get("total_usd", 0) >= sp["limit_usd"]:
            raise RuntimeError(f"лимит трат ${sp['limit_usd']:.2f} достигнут")
        api_key = self._active_anthropic_key()
        if not api_key:
            raise RuntimeError("нет anthropic_api_keys в config.json")
        t0 = time.time()
        resp = self._anthropic(
            api_key,
            [{"role": "user", "content": task.prompt}],
            model=ROUTER_MODEL,
            system=QA_SYSTEM_PROMPT,
            tools=None,
            max_tokens=1024,
        )
        usage = resp.get("usage", {}) or {}
        parts = [
            b.get("text", "") for b in (resp.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        answer = "\n".join(p for p in parts if p).strip() or "(пустой ответ)"
        # Spend meter (shared with chat) + per-task run record.
        self._add_cost(ROUTER_MODEL, usage)
        self._save_state()
        cin, cout = MODEL_PRICING[ROUTER_MODEL]
        cost = usage.get("input_tokens", 0) * cin + usage.get("output_tokens", 0) * cout
        self._queue.record_run(
            task.id, ROUTER_MODEL,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            cost_usd=cost, duration_s=time.time() - t0, ok=True,
        )
        return answer

    def _code_executor(self, task) -> str:
        """Dispatcher executor for kind=code: run `claude -p` headless under the
        active account, in the configured dev root. Flat-rate subscription, not
        metered API.

        NOTE: runs trusted (can modify files). The SonnetReviewer gate (P3.13)
        is not wired yet — until then, code tasks execute unguarded. dispatch is
        OFF by default and opt-in, so this only runs when the user enables it.
        """
        # Safety gate (P3.13): destructive-looking prompts get a Sonnet verdict
        # before claude runs trusted. Cheap/safe prompts skip the (paid) review.
        if is_destructive(task.prompt):
            verdict = self._reviewer.review(
                f"claude -p (trusted): {task.prompt}",
                context=f"cwd={self._claude_dev_root()}")
            if verdict.get("verdict") == "deny":
                self._queue.record_run(task.id, "claude-cli", ok=False)
                raise RuntimeError(f"заблокировано ревьюером: {verdict.get('reason')}")
        payload = {
            "prompt": task.prompt,
            "model": "sonnet",
            "cwd": str(self._claude_dev_root()),
            "config_dir": self._active_config_dir(),
            "trusted": True,
            "timeout": 1800,
        }
        t0 = time.time()
        res = run_claude_code_stream(payload, on_event=None, on_proc=None)
        dur = time.time() - t0
        if not res.get("success"):
            self._queue.record_run(task.id, "claude-cli", duration_s=dur, ok=False)
            raise RuntimeError(res.get("error") or "claude CLI failed")
        data = res.get("data") or {}
        self._queue.record_run(
            task.id, "claude-cli",
            cost_usd=float(data.get("cost_usd") or 0.0), duration_s=dur, ok=True,
        )
        out = (data.get("result") or "").strip() or "(claude вернул пустой результат)"
        # Telegram hard-caps messages near 4096 chars; trim long CLI output.
        return out[:3500] + ("…" if len(out) > 3500 else "")

    def _click_executor(self, task) -> str:
        """Dispatcher executor for kind=click_gui: Haiku drives the desktop via
        computer-use. Pre-flight review on destructive goals; /stop aborts."""
        api_key = self._active_anthropic_key()
        if not api_key:
            raise RuntimeError("нет anthropic_api_keys")
        # Lazy import: pyautogui pulls a GUI backend — only load when we click.
        from agent.orchestrator.desktop import Desktop
        from agent.orchestrator.computer import ComputerUseClient
        # Clear any stale emergency-stop flag before starting.
        self._state["dispatch_emergency_stop"] = False
        self._save_state()
        desktop = Desktop()
        client = ComputerUseClient(
            api_key, ROUTER_MODEL, desktop,
            reviewer=self._reviewer, is_destructive=is_destructive,
            should_abort=lambda: bool(self._state.get("dispatch_emergency_stop")),
            on_step=lambda t: self._send_message(task.chat_id, f"🖱 {t[:200]}"),
            max_steps=25)
        t0 = time.time()
        res = client.run(task.prompt)
        usage = res.get("usage", {}) or {}
        self._add_cost(ROUTER_MODEL, usage)
        self._save_state()
        cin, cout = MODEL_PRICING[ROUTER_MODEL]
        cost = usage.get("input_tokens", 0) * cin + usage.get("output_tokens", 0) * cout
        self._queue.record_run(
            task.id, "haiku-cu",
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            cost_usd=cost, duration_s=time.time() - t0,
            ok=not res.get("denied"))
        return res.get("summary", "(пусто)")

    def _review_ask(self, system: str, user: str) -> str:
        """Sonnet call for the SonnetReviewer. Reuses _anthropic + spend meter."""
        api_key = self._active_anthropic_key()
        if not api_key:
            raise RuntimeError("нет anthropic_api_keys для ревью")
        resp = self._anthropic(
            api_key, [{"role": "user", "content": user}],
            model=ANALYST_MODEL, system=system, tools=None, max_tokens=300)
        usage = resp.get("usage", {}) or {}
        self._add_cost(ANALYST_MODEL, usage)
        self._save_state()
        parts = [
            b.get("text", "") for b in (resp.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()

    def _notify_budget_block(self) -> None:
        """One-shot ping when the daily orchestrator budget is exhausted."""
        owner = self._state.get("owner_chat_id")
        if not owner:
            return
        try:
            self._send_message(
                owner,
                f"🚫 Дневной лимит оркестратора ${self._budget.limit():.2f} исчерпан "
                f"(потрачено ${self._budget.today_spent():.4f}). Задачи ждут до "
                "полуночи или подними `daily_budget_usd` в config.json.")
        except Exception as e:
            logger.warning(f"budget-block notify failed: {e}")

    # All skill names the bot routes (top-level tools + queue_agent_command sub-types).
    # Used by /skills to render the toggle keyboard; must stay in sync with TOOLS.
    _ALL_SKILLS = (
        "queue_agent_command",  # top-level (dropping disables ALL sub-types)
        "control_computer",
        "analyze",
        "run_claude_code",
        # queue_agent_command sub-types:
        "screenshot", "terminal", "launch-app", "install", "chrome-profile",
        "system-info", "obsidian-log", "obsidian-read", "obsidian-context",
        "send-file", "read-document",
    )

    def _show_skills_toggle(self, chat_id):
        """Inline keyboard listing every skill with ✅/⛔ state.

        Tapping a skill flips its presence in config.skill_disabled and writes
        the updated config back. Takes effect on the next AI turn (no restart
        needed — _build_tools is called per turn).
        """
        self._cfg = load_config()
        disabled = set(self._cfg.get("skill_disabled") or [])
        rows = []
        for s in self._ALL_SKILLS:
            on = s not in disabled
            label = f"{'✅' if on else '⛔'} {s}"
            rows.append([{"text": label, "callback_data": f"skill:toggle:{s}"}])
        rows.append([{"text": "Готово", "callback_data": "skill:done"}])
        self._send_message(
            chat_id,
            "🧰 *Инструменты AI-роутера* — нажми чтобы переключить:\n"
            "_✅ = доступен Haiku  ·  ⛔ = выключен в config.skill_disabled_",
            reply_markup={"inline_keyboard": rows}, parse_mode="Markdown")

    def _toggle_skill(self, chat_id, skill_name: str):
        """Add/remove a skill from config.skill_disabled and re-render the keyboard."""
        if skill_name not in self._ALL_SKILLS:
            return
        cfg = load_config()
        disabled = list(cfg.get("skill_disabled") or [])
        if skill_name in disabled:
            disabled.remove(skill_name)
            verb = "включил"
        else:
            disabled.append(skill_name)
            verb = "выключил"
        cfg["skill_disabled"] = disabled
        try:
            save_config(cfg)
            self._cfg = cfg
            self._send_message(chat_id, f"✅ {verb} `{skill_name}`", parse_mode="Markdown")
            self._show_skills_toggle(chat_id)
        except Exception as e:
            self._send_message(chat_id, f"❌ Не сохранилось: {e}")

    def _set_bot_commands(self) -> None:
        """Register the slash-command list as Telegram quick commands (the '/'
        menu). Single source: BOT_COMMANDS."""
        cmds = [{"command": c, "description": d} for c, d, _g in BOT_COMMANDS]
        try:
            self._tg("setMyCommands", {"commands": cmds})
            logger.info("registered %d bot commands", len(cmds))
        except Exception as e:
            logger.warning(f"setMyCommands failed: {e}")

    def _show_help(self, chat_id):
        """Print every available slash command, grouped. Built from BOT_COMMANDS
        so it never drifts from the registered quick commands."""
        ver = self._get_version()
        groups: dict[str, list[str]] = {}
        for cmd, desc, group in BOT_COMMANDS:
            groups.setdefault(group, []).append(f"`/{cmd}` — {desc}")
        lines = ["*🤖 Dispatch Agent — справка*", ""]
        for group, items in groups.items():
            lines.append(f"*{group}:*")
            lines.extend(items)
            lines.append("")
        lines.append(
            "*Просто пиши задачу текстом* — Haiku-роутер сам поймёт: терминал, "
            "скриншот, чтение файла, делегирование в `claude -p`, клики мышью. "
            "Картинки распознаются через vision.")
        lines.append(f"\n🔖 _v {ver}_")
        self._send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    # ── Menu builders (dynamic sections) ─────────────────────────────────────

    def _active_account_label(self) -> str:
        accounts = self._cfg.get("claude_accounts") or []
        idx = int(self._state.get("active_account", 1)) - 1
        if 0 <= idx < len(accounts):
            return accounts[idx].get("email") or f"#{idx + 1}"
        return "(не настроен)"

    def _cowork_menu(self):
        """Коворк = рабочее пространство оркестратора (async-задачи)."""
        on = bool(self._state.get("dispatch_enabled", False))
        queued = len(self._queue.list(status="queued", limit=100))
        spent, limit = self._budget.today_spent(), self._budget.limit()
        text = (
            "🤝 *Коворк* — рабочее пространство\n\n"
            f"Dispatch: {'🟢 ВКЛ' if on else '⏸ ВЫКЛ'}\n"
            f"В очереди: {queued}\n"
            f"Бюджет сегодня: ${spent:.4f} / ${limit:.2f}\n\n"
            "Ставь задачи: `/q <текст>` или `/c <код-задача>`")
        kb = {"inline_keyboard": [
            [{"text": ("⏸ Выключить Dispatch" if on else "🟢 Включить Dispatch"),
              "callback_data": "dispatch:toggle"}],
            [{"text": "📋 Мои задачи", "callback_data": "tasks:show"}],
            [{"text": "⬅️ Назад", "callback_data": "nav:main"}],
        ]}
        return text, kb

    def _settings_menu(self):
        text = f"⚙️ *Настройки*\n\nАккаунт Claude: *{self._active_account_label()}*"
        kb = {"inline_keyboard": [
            [{"text": "🔑 Аккаунт / переключить", "callback_data": "menu:accounts"}],
            [{"text": "🧰 Скилы", "callback_data": "settings:skills"},
             {"text": "💰 Траты", "callback_data": "settings:spend"}],
            [{"text": "🔄 Обновить агента", "callback_data": "agent:update"},
             {"text": "🔧 Пересканировать", "callback_data": "sys:rescan"}],
            [{"text": "⬅️ Назад", "callback_data": "nav:main"}],
        ]}
        return text, kb

    # ── Callback (button) handling ──────────────────────────────────────────────

    def _handle_callback(self, cb: dict):
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")
        _ok, expired = self._answer_callback(cb["id"])
        if expired:
            # Stale click from a previous bot lifetime. Acting on it (e.g.
            # agent:update) would self-restart, drop the offset, fetch the same
            # stale callback again — infinite loop. Drop it.
            logger.info(f"callback expired, skipping action: {data!r}")
            return

        # Ephemeral menu navigation: edit the SAME message in place so the chat
        # doesn't accumulate stale menus.
        if data.startswith("nav:") or data in ("dispatch:toggle", "tasks:show",
                                               "settings:skills", "settings:spend"):
            mid = cb["message"]["message_id"]
            if data == "nav:main":
                self._edit_menu(chat_id, mid, MAIN_MENU_TEXT, MAIN_MENU)
            elif data == "nav:chat":
                self._edit_menu(chat_id, mid, CHAT_TEXT, CHAT_MENU)
            elif data == "nav:code":
                self._edit_menu(chat_id, mid, "💻 *Код*", CODE_MENU)
            elif data == "nav:cowork":
                text, kb = self._cowork_menu()
                self._edit_menu(chat_id, mid, text, kb)
            elif data == "nav:settings":
                text, kb = self._settings_menu()
                self._edit_menu(chat_id, mid, text, kb)
            elif data == "dispatch:toggle":
                self._state["dispatch_enabled"] = not bool(self._state.get("dispatch_enabled", False))
                self._save_state()
                text, kb = self._cowork_menu()
                self._edit_menu(chat_id, mid, text, kb)
            elif data == "tasks:show":
                self._cmd_list_tasks(chat_id)
            elif data == "settings:skills":
                self._show_skills_toggle(chat_id)
            elif data == "settings:spend":
                self._handle_command({"chat": {"id": chat_id}, "text": "/spend"})
            return

        if data.startswith("cmd:"):
            self._execute_and_reply({"type": data[4:], "payload": {}}, chat_id)
        elif data.startswith("app:"):
            self._execute_and_reply({"type": "launch-app", "payload": {"app": data[4:]}}, chat_id)
        elif data.startswith("chrome:"):
            self._execute_and_reply(
                {"type": "chrome-profile", "payload": {"profile_directory": data[7:]}}, chat_id)
        elif data == "menu:apps":
            self._send_message(chat_id, "🚀 Выбери приложение:", reply_markup=APPS_MENU)
        elif data == "menu:chrome":
            profiles = get_chrome_profiles()
            if not profiles:
                self._send_message(chat_id, "🌐 Профили Chrome не найдены.")
            else:
                kb = {"inline_keyboard": [
                    [{"text": p["display"], "callback_data": f"chrome:{p['directory']}"}]
                    for p in profiles]}
                self._send_message(chat_id, "🌐 Профили Chrome:", reply_markup=kb)
        elif data == "menu:terminal":
            self._pending[str(chat_id)] = {"type": "terminal"}
            self._send_message(chat_id, "⌨️ Пришли команду одним сообщением:")
        elif data == "menu:install":
            self._pending[str(chat_id)] = {"type": "install"}
            self._send_message(chat_id, "📦 Пришли ссылку (.exe / .msi / .zip):")
        elif data == "sys:rescan":
            self._trigger_rescan(chat_id)
        elif data == "agent:update":
            self._self_restart(chat_id)
        elif data == "agent:noupdate":
            self._send_message(chat_id, "Ок, оставил как есть. Можно вызвать вручную: /update")
        elif data == "menu:accounts":
            self._cfg = load_config()
            accounts = self._cfg.get("claude_accounts") or []
            active = self._state.get("active_account", 1)
            if not accounts:
                self._send_message(
                    chat_id,
                    "🔑 Аккаунты Claude Code не настроены. Добавь их в config.json "
                    "(поле \"claude_accounts\").")
            else:
                rows = [[{"text": f"{a.get('email', f'аккаунт {i}')}"
                                  f"{' ✓' if active == i else ''}",
                          "callback_data": f"account:{i}"}]
                        for i, a in enumerate(accounts, start=1)]
                self._send_message(
                    chat_id,
                    f"🔑 Аккаунт Claude Code (активен #{active}).\n"
                    "Тяжёлые задачи (claude -p) пойдут под выбранным аккаунтом.",
                    reply_markup={"inline_keyboard": rows})
        elif data.startswith("account:"):
            n = int(data[8:])
            accounts = self._cfg.get("claude_accounts") or []
            self._state["active_account"] = n
            self._save_state()
            email = accounts[n - 1].get("email") if 0 < n <= len(accounts) else f"#{n}"
            self._send_message(chat_id, f"✅ Активный аккаунт Claude Code: {email}")
        elif data.startswith("claude:"):
            self._handle_claude_callback(chat_id, data[7:])
        elif data.startswith("skill:toggle:"):
            self._toggle_skill(chat_id, data[len("skill:toggle:"):])
        elif data == "skill:done":
            self._send_message(chat_id, "Ок. Изменения подхватятся со следующего сообщения AI.")
        elif data.startswith("acctretry:"):
            arg = data[10:]
            pend = self._pending_delegation.pop(str(chat_id), None)
            if arg == "cancel":
                self._send_message(chat_id, "Ок, оставил как есть.")
            elif not pend:
                self._send_message(chat_id, "Нет задачи для повтора.")
            else:
                n = int(arg)
                self._state["active_account"] = n
                self._save_state()
                accounts = self._cfg.get("claude_accounts") or []
                email = accounts[n - 1].get("email", f"#{n}") if 0 < n <= len(accounts) else f"#{n}"
                self._send_message(chat_id, f"🔁 Переключился на {email}, повторяю задачу…")
                threading.Thread(target=self._retry_delegation,
                                 args=(chat_id, pend["payload"]), daemon=True,
                                 name="RetryDelegation").start()

    # ── Incoming files (documents / photos / audio / video) ─────────────────────

    def _download_file(self, file_id: str, fname: str | None = None):
        """Download a Telegram file to E:\\dispatch\\inbox\\. Bot API caps this at 20 MB."""
        r = self._tg("getFile", {"file_id": file_id})
        if not r.get("ok"):
            return None
        file_path = r["result"]["file_path"]
        token = (self._cfg.get("telegram_bot_token") or "").strip()
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        inbox = INBOX_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        # SECURITY: file_name comes from the sender — keep only the basename so a
        # crafted name (..\\, absolute path) can't escape the inbox dir.
        safe_name = Path(fname).name if fname else ""
        if not safe_name:
            safe_name = Path(file_path).name or f"file_{int(time.time())}"
        dest = inbox / safe_name
        if dest.exists():
            dest = inbox / f"{dest.stem}_{int(time.time())}{dest.suffix}"
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest

    def _handle_incoming_file(self, msg: dict):
        chat_id = msg["chat"]["id"]
        caption = (msg.get("caption") or "").strip()
        self._cfg = load_config()

        file_id, fname = None, None
        if "document" in msg:
            file_id = msg["document"]["file_id"]
            fname = msg["document"].get("file_name")
        elif "photo" in msg:
            file_id = msg["photo"][-1]["file_id"]  # largest size
            fname = f"photo_{int(time.time())}.jpg"
        elif "audio" in msg:
            file_id = msg["audio"]["file_id"]
            fname = msg["audio"].get("file_name", f"audio_{int(time.time())}.mp3")
        elif "video" in msg:
            file_id = msg["video"]["file_id"]
            fname = f"video_{int(time.time())}.mp4"
        if not file_id:
            return

        self._set_status("active")
        try:
            path = self._download_file(file_id, fname)
        except Exception as e:
            logger.error(f"file download failed: {e}")
            path = None
        self._set_status("idle")

        if not path:
            self._send_message(
                chat_id,
                "❌ Не смог скачать файл (возможно, больше 20 МБ — это лимит Telegram "
                "Bot API на скачивание). Положи файл на ПК и дай путь текстом.")
            return

        size_kb = path.stat().st_size // 1024
        self._send_message(chat_id, f"📥 Файл сохранён: {path.name} ({size_kb} КБ)\n{path}")

        scid = str(chat_id)
        # Route images and documents differently: images go as a vision block
        # (Haiku can SEE them — much more useful than guessing at read-document).
        # Anything else stays as a path hint for read-document.
        suffix = path.suffix.lower()
        IMAGE_TYPES = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        }
        AUDIO_VIDEO_EXTS = {
            ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac",
            ".mp4", ".mov", ".avi", ".mkv", ".webm",
        }
        # Audio/video isn't routed anywhere yet (no Whisper, no video understanding) —
        # be explicit instead of pretending we'll do read-document on a .mp4.
        if suffix in AUDIO_VIDEO_EXTS:
            kind = "🎤 аудио" if suffix in {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac"} else "🎬 видео"
            self._send_message(
                chat_id,
                f"{kind} получено и сохранено в inbox, но обрабатывать пока не умею "
                "(нужен Whisper для аудио). Если хочешь — расшифруй текстом и пришли.")
            # Drop any stale image/file hint to avoid confusing the next AI turn.
            self._pending_image.pop(scid, None)
            self._last_file.pop(scid, None)
            return
        if suffix in IMAGE_TYPES:
            try:
                b64 = base64.b64encode(path.read_bytes()).decode("ascii")
                self._pending_image[scid] = {
                    "b64": b64, "media_type": IMAGE_TYPES[suffix], "name": path.name,
                }
                # Drop any stale doc hint so we don't double-route on the same turn.
                self._last_file.pop(scid, None)
            except Exception as e:
                logger.warning(f"failed to stash image as vision: {e}")
                self._last_file[scid] = str(path)
        else:
            self._last_file[scid] = str(path)

        if caption:
            threading.Thread(target=self._ai_turn, args=(chat_id, caption),
                             daemon=True, name="AITurn").start()
        else:
            hint = ("Что сделать с этой картинкой? (опиши / найди текст / посчитай / …) — "
                    "я её вижу." if suffix in IMAGE_TYPES else
                    "Напиши, что с ним сделать (прочитать / саммари / перевести / "
                    "извлечь текст…) — я прочитаю любой формат.")
            self._send_message(chat_id, hint)

    # ── Free text -> pending input or AI ────────────────────────────────────────

    def _handle_text(self, msg: dict):
        chat_id = msg["chat"]["id"]
        scid = str(chat_id)
        text = msg["text"]

        pending = self._pending.pop(scid, None)
        if pending:
            ptype = pending.get("type")
            if ptype == "terminal":
                self._execute_and_reply({"type": "terminal", "payload": {"cmd": text}}, chat_id)
            elif ptype == "install":
                self._execute_and_reply({"type": "install", "payload": {"url": text}}, chat_id)
            elif ptype == "claude_new_folder":
                self._claude_create_folder(chat_id, text)
            elif ptype == "claude_task":
                threading.Thread(
                    target=self._claude_run,
                    args=(chat_id, pending["folder"], pending["model"], text),
                    daemon=True, name="ClaudeRun").start()
            elif ptype == "claude_choosing":
                # User typed text instead of clicking a folder/model button. Don't
                # eat the message silently — guide them back to the picker.
                self._send_message(
                    chat_id,
                    "Я жду клик по кнопке (папка или модель). Открой меню заново: /claude")
            else:
                # Unknown pending type — log and fall through to AI so the message
                # isn't lost.
                logger.warning(f"unknown pending type: {ptype!r}, falling through to AI")
                threading.Thread(target=self._ai_turn, args=(chat_id, text),
                                 daemon=True, name="AITurn").start()
            return

        # Run the AI turn off the polling thread so a long claude -p delegation
        # doesn't freeze getUpdates (the bot stays responsive to other messages).
        threading.Thread(target=self._ai_turn, args=(chat_id, text),
                         daemon=True, name="AITurn").start()

    # ── Execute a command and deliver result ─────────────────────────────────────

    def _execute_and_reply(self, command: dict, chat_id) -> dict:
        command.setdefault("payload", {})
        self._cfg = load_config()  # fresh apps/discovered_apps/config
        self._set_status("active")
        try:
            result = dispatch(command, self._apps(), self._cfg.get("obsidian", {}))
        except Exception as e:
            result = {"success": False, "data": None, "error": str(e)}
        self._deliver(chat_id, command.get("type", ""), result)
        self._set_status("idle")
        return result

    def _deliver(self, chat_id, ctype: str, result: dict) -> dict | None:
        """Render a dispatch result back to the user. Returns the Telegram API
        response of the primary send so callers can track the message id."""
        if not result.get("success"):
            return self._send_message(chat_id, f"❌ Ошибка: {result.get('error')}")

        data = result.get("data")

        if ctype == "screenshot" and isinstance(data, dict) and data.get("image"):
            return self._send_photo(chat_id, base64.b64decode(data["image"]),
                                    caption=f"📸 {data.get('width')}×{data.get('height')}")
        elif ctype == "computer" and isinstance(data, dict) and data.get("image"):
            return self._send_photo(chat_id, base64.b64decode(data["image"]),
                                    caption=f"🖥 {data.get('action', '')}")
        elif ctype == "terminal" and isinstance(data, dict):
            if data.get("send_as_file") and data.get("output_file_b64"):
                return self._send_document(
                    chat_id, base64.b64decode(data["output_file_b64"]),
                    data.get("filename", "output.txt"),
                    caption=data.get("output", ""), mime="text/plain")
            out = (data.get("output") or "").strip() or "(пустой вывод)"
            return self._send_message(chat_id, f"```\n{out[:3900]}\n```", parse_mode="Markdown")
        elif ctype == "system-info" and isinstance(data, dict):
            return self._send_message(
                chat_id,
                f"💻 Система\n"
                f"CPU: {data.get('cpu_percent')}%\n"
                f"RAM: {data.get('ram_used_gb')} / {data.get('ram_total_gb')} ГБ "
                f"({data.get('ram_percent')}%)\n"
                f"Диск C: {data.get('disk_used_gb')} / {data.get('disk_total_gb')} ГБ "
                f"({data.get('disk_percent')}%)")
        elif ctype == "send-file" and isinstance(data, dict) and data.get("file_base64"):
            return self._send_document(chat_id, base64.b64decode(data["file_base64"]),
                                       data.get("filename", "file"),
                                       mime=data.get("mime_type", "application/octet-stream"))
        elif ctype == "chrome-profiles-list" and isinstance(data, list):
            lines = "\n".join(f"• {p.get('display')}" for p in data) or "(нет профилей)"
            return self._send_message(chat_id, f"🌐 Профили Chrome:\n{lines}")
        elif ctype == "obsidian-context" and isinstance(data, dict):
            notes = data.get("notes", {})
            body = "\n\n".join(f"*{n}*\n{c}" for n, c in notes.items()) or "(пусто)"
            return self._send_message(chat_id, body[:3900])
        elif ctype == "run-claude-code" and isinstance(data, dict):
            res = (data.get("result") or "").strip() or "(пусто)"
            cost = data.get("cost_usd")
            tag = f" • ~${cost:.3f} (подписка)" if isinstance(cost, (int, float)) else ""
            return self._send_message(
                chat_id, f"🤖 Claude Code [{data.get('model')}]{tag}:\n{res[:3800]}")
        elif ctype == "read-document" and isinstance(data, dict):
            body = (data.get("text") or "").strip() or "(пустой документ)"
            tail = " …(обрезано)" if data.get("truncated") else ""
            via = " · через Claude Code" if data.get("via") else ""
            return self._send_message(
                chat_id,
                f"📄 {data.get('filename')} ({data.get('chars')} симв.{via}):\n\n{body[:3800]}{tail}")
        elif isinstance(data, dict):
            return self._send_message(
                chat_id,
                f"```\n{json.dumps(data, ensure_ascii=False, indent=2)[:3800]}\n```",
                parse_mode="Markdown")
        else:
            return self._send_message(chat_id, f"✅ {data}")

    # ── AI chat with tool-calling ────────────────────────────────────────────────

    def _ai_turn(self, chat_id, user_text: str):
        # Serialize turns for the same chat so concurrent messages don't interleave
        # shared state (_history, spend). Different chats still run in parallel.
        lock = self._chat_locks.setdefault(str(chat_id), threading.Lock())
        with lock:
            self._ai_turn_locked(chat_id, user_text)

    def _ai_turn_locked(self, chat_id, user_text: str):
        scid = str(chat_id)
        self._cfg = load_config()  # pick up key/config edits without restart
        keys = self._cfg.get("anthropic_api_keys") or []
        idx = self._state.get("active_account", 1) - 1
        api_key = (keys[idx] if 0 <= idx < len(keys) else "") or (keys[0] if keys else "")

        if not api_key:
            self._send_message(
                chat_id,
                "🤖 AI-чат не настроен. Добавь ключ Anthropic в config.json "
                "(поле \"anthropic_api_keys\"), либо пользуйся кнопками — /start.")
            return

        sp = self._state["spend"]
        if sp["total_usd"] >= sp["limit_usd"]:
            self._send_message(
                chat_id,
                f"🚫 Достигнут лимит трат ${sp['limit_usd']:.2f}. "
                f"/setlimit <сумма> или /resetspend.")
            return

        self._set_status("active")
        # Fresh tracker for this turn — _cleanup_turn at the end keeps the answer
        # and deletes the status/screenshot/error noise.
        self._turn_track[scid] = []
        hist = self._history.setdefault(scid, [])
        # Pop one-shot per-turn context: a recently uploaded image (vision input)
        # OR a recently uploaded document path (hint for read-document). Image
        # wins if both are present — it's the more direct signal.
        pending_img = self._pending_image.pop(scid, None)
        recent_file = None if pending_img else self._last_file.pop(scid, None)

        live_text = user_text
        if recent_file:
            live_text = (f"{user_text}\n\n[Контекст: недавно загружен файл «{recent_file}». "
                         "Если задача про этот файл — прочитай его через read-document.]")

        if pending_img:
            user_content = [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": pending_img["media_type"],
                    "data": pending_img["b64"],
                }},
                {"type": "text",
                 "text": (live_text or
                          f"Посмотри картинку «{pending_img['name']}» и опиши/обработай по контексту.")},
            ]
        else:
            user_content = live_text
        # work on a local copy so screenshots (vision) don't bloat persistent history
        convo = list(hist) + [{"role": "user", "content": user_content}]
        final_text = ""
        active_tools = self._build_tools()
        disabled_set = set(self._cfg.get("skill_disabled") or [])
        try:
            for _ in range(12):  # bounded tool-calling / computer-use loop
                # Strip image blocks from old tool_results so the convo doesn't
                # balloon when computer-use takes many screenshots in one turn.
                self._prune_old_screenshots(convo, keep_last=3)
                resp = self._anthropic(api_key, convo, model=ROUTER_MODEL, tools=active_tools)
                self._add_cost(ROUTER_MODEL, resp.get("usage", {}) or {})
                self._save_state()

                content = resp.get("content", []) or []
                text_out = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                tool_uses = [b for b in content if b.get("type") == "tool_use"]
                convo.append({"role": "assistant", "content": content})

                if text_out.strip():
                    self._track(chat_id, "text", self._send_message(chat_id, text_out))
                    final_text = text_out

                if not tool_uses:
                    if not text_out.strip():
                        self._track(chat_id, "status",
                                    self._send_message(chat_id, "(пустой ответ)"))
                    break
                if sp["total_usd"] >= sp["limit_usd"]:
                    self._track(chat_id, "error", self._send_message(
                        chat_id,
                        f"🚫 Лимит трат ${sp['limit_usd']:.2f} достигнут — останавливаюсь."))
                    break

                tool_results = []
                for tu in tool_uses:
                    name = tu.get("name", "")
                    inp = tu.get("input", {}) or {}

                    # ANALYST layer (Sonnet): analysis / prompt writing, not a PC action
                    if name == "analyze":
                        self._track(chat_id, "status",
                                    self._send_message(chat_id, "🧠 Анализирую (Sonnet)…"))
                        try:
                            text = self._sonnet_analyze(api_key, inp.get("goal", ""),
                                                        inp.get("context", ""))
                        except Exception as e:
                            text = f"Error: {e}"
                        tool_results.append({"type": "tool_result",
                                             "tool_use_id": tu["id"], "content": text})
                        continue

                    # heavy work delegated to Claude Code on the subscription
                    if name == "run_claude_code":
                        cwd = inp.get("cwd")
                        trusted = self._cwd_is_trusted(cwd)
                        self._track(chat_id, "status", self._send_message(
                            chat_id,
                            "🚀 Делегирую в Claude Code (подписка)…"
                            + ("" if trusted else " · safe-mode (acceptEdits)")))
                        payload = {
                            "prompt": inp.get("prompt", ""),
                            "cwd": cwd,
                            "model": inp.get("model", "sonnet"),
                            "config_dir": self._active_config_dir(),
                            "trusted": trusted,
                        }
                        cmd = {"type": "run-claude-code", "payload": payload}
                        try:
                            result = dispatch(cmd, self._apps(), self._cfg.get("obsidian", {}))
                        except Exception as e:
                            result = {"success": False, "data": None, "error": str(e)}
                        if not result.get("success") and self._is_limit_error(result.get("error")):
                            self._suggest_switch(chat_id, payload)
                            tool_results.append({
                                "type": "tool_result", "tool_use_id": tu["id"],
                                "content": "Делегирование приостановлено: активный аккаунт достиг лимита. "
                                           "Пользователю предложено переключение на другой аккаунт. "
                                           "Не повторяй вызов сам — заверши ход кратким сообщением."})
                            continue
                        role = "result" if result.get("success") else "error"
                        self._track(chat_id, role, self._deliver(chat_id, cmd["type"], result))
                        tool_results.append(self._tool_result_block(tu["id"], cmd, result))
                        continue

                    if name == "control_computer":
                        cmd = {"type": "computer", "payload": inp}
                    else:
                        cmd = {"type": inp.get("type"), "payload": inp.get("payload", {}) or {}}

                    # Safety net: if the model still called a tool the user disabled
                    # (race with config edit, or schema-ignoring model), refuse here.
                    if name in disabled_set or cmd.get("type") in disabled_set:
                        result = {"success": False, "data": None,
                                  "error": f"скилл '{cmd.get('type') or name}' выключен в настройках"}
                    else:
                        try:
                            result = dispatch(cmd, self._apps(), self._cfg.get("obsidian", {}))
                        except Exception as e:
                            result = {"success": False, "data": None, "error": str(e)}
                    role = "result" if result.get("success") else "error"
                    self._track(chat_id, role, self._deliver(chat_id, cmd["type"], result))
                    tool_results.append(self._tool_result_block(tu["id"], cmd, result))
                convo.append({"role": "user", "content": tool_results})

            # persist a clean, image-free summary for future turns. Always pair the
            # user msg with an assistant msg (placeholder if the turn produced no
            # text) so roles strictly alternate — two consecutive user msgs would
            # make the next Anthropic call 400.
            hist.append({"role": "user", "content": user_text})
            hist.append({"role": "assistant",
                         "content": final_text or "(выполнено без текстового ответа)"})
            if len(hist) > 20:
                self._history[scid] = hist[-20:]
            self._persist_chats_for_ui()
        except Exception as e:
            logger.error(f"ai error: {e}")
            self._track(chat_id, "error",
                        self._send_message(chat_id, f"🤖 Ошибка AI: {e}"))
        finally:
            self._set_status("idle")
            # Sweep ephemeral noise (status pings, intermediate screenshots, recovered
            # errors) so the chat stays as a clean log of just the answers.
            try:
                self._cleanup_turn(chat_id)
            except Exception as e:
                logger.warning(f"turn cleanup failed: {e}")

    @staticmethod
    def _prune_old_screenshots(convo: list, keep_last: int = 3) -> int:
        """Replace screenshot image blocks in older tool_results with a stub.

        Computer-use can take 8-12 screenshots in one turn; each ~80-150 KB JPEG
        translates to ~30-50k input tokens. Past a couple of iterations Haiku
        only needs the LATEST screen state — older ones bloat context for no
        gain. Keeps the most recent `keep_last` images; replaces earlier ones
        with a text breadcrumb.

        Returns the number of images stripped (for logging if needed).
        """
        locations = []  # list of (msg_idx, content_idx, block_idx)
        for mi, msg in enumerate(convo):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for ci, item in enumerate(content):
                if not (isinstance(item, dict) and item.get("type") == "tool_result"):
                    continue
                tc = item.get("content")
                if not isinstance(tc, list):
                    continue
                for bi, b in enumerate(tc):
                    if isinstance(b, dict) and b.get("type") == "image":
                        locations.append((mi, ci, bi))
        if len(locations) <= keep_last:
            return 0
        to_strip = locations[:-keep_last]
        for mi, ci, bi in to_strip:
            convo[mi]["content"][ci]["content"][bi] = {
                "type": "text",
                "text": "[earlier screenshot pruned — use latest images for current state]",
            }
        return len(to_strip)

    @staticmethod
    def _tool_result_block(tool_use_id: str, cmd: dict, result: dict) -> dict:
        """Build a tool_result; include the screenshot as an image so Claude can SEE the screen."""
        data = result.get("data")
        if result.get("success") and isinstance(data, dict) and data.get("image"):
            label = cmd.get("payload", {}).get("action") or cmd.get("type")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [
                    {"type": "text", "text": (
                        f"{label}: ok. screenshot {data.get('width')}x{data.get('height')} px; "
                        f"real screen {data.get('screen_width', '?')}x{data.get('screen_height', '?')}. "
                        "Coordinates are in screenshot pixels.")},
                    {"type": "image", "source": {"type": "base64",
                                                  "media_type": "image/jpeg", "data": data["image"]}},
                ],
            }
        if result.get("success"):
            rc = data[:3000] if isinstance(data, str) else json.dumps(data, ensure_ascii=False)[:3000]
        else:
            rc = f"Error: {result.get('error')}"
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": rc,
                "is_error": not result.get("success", False)}

    def _build_tools(self) -> list:
        """Filter TOOLS by config.skill_disabled.

        Names can be top-level (queue_agent_command, control_computer, analyze,
        run_claude_code) or queue_agent_command sub-types (screenshot, terminal,
        obsidian-log, install, …). When all sub-types of queue_agent_command are
        disabled, the tool itself is dropped so Haiku doesn't see a degenerate
        schema with an empty enum.
        """
        disabled = set(self._cfg.get("skill_disabled") or [])
        if not disabled:
            return TOOLS
        out = []
        for tool in TOOLS:
            if tool["name"] in disabled:
                continue
            if tool["name"] == "queue_agent_command":
                schema = json.loads(json.dumps(tool))  # deep copy
                enum = schema["input_schema"]["properties"]["type"]["enum"]
                enum = [e for e in enum if e not in disabled]
                if not enum:
                    continue
                schema["input_schema"]["properties"]["type"]["enum"] = enum
                out.append(schema)
            else:
                out.append(tool)
        return out

    def _anthropic(self, api_key: str, messages: list, model: str = ROUTER_MODEL,
                   system: str = SYSTEM_PROMPT, tools: list | None = TOOLS,
                   max_tokens: int = 4096) -> dict:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = {"type": "auto"}
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=120,
        )
        data = resp.json()
        if isinstance(data, dict) and data.get("type") == "error":
            err = data.get("error", {})
            raise RuntimeError(err.get("message", str(err)))
        return data

    def _add_cost(self, model: str, usage: dict) -> None:
        """Add an API call's cost to the spend tracker, priced per model.

        On a new calendar month, zero the running counters first so /spend
        shows a fresh meter each month (the limit_usd is per-month by intent).
        """
        cin, cout = MODEL_PRICING.get(model, MODEL_PRICING[ROUTER_MODEL])
        sp = self._state["spend"]
        import datetime
        now_month = datetime.date.today().strftime("%Y-%m")
        if sp.get("month") != now_month:
            sp["anthropic_usd"] = 0.0
            sp["openai_usd"]    = 0.0
            sp["total_usd"]     = 0.0
            sp["month"]         = now_month
            logger.info(f"spend: rolled over to month {now_month}")
        sp["anthropic_usd"] += (usage.get("input_tokens", 0) * cin
                                + usage.get("output_tokens", 0) * cout)
        sp["total_usd"] = sp["anthropic_usd"] + sp.get("openai_usd", 0)

    def _sonnet_analyze(self, api_key: str, goal: str, context: str = "") -> str:
        """ANALYST layer: one-shot Sonnet call for analysis / prompt writing."""
        msgs = [{"role": "user",
                 "content": f"ЦЕЛЬ:\n{goal}\n\nКОНТЕКСТ:\n{context.strip() or '(нет)'}"}]
        resp = self._anthropic(api_key, msgs, model=ANALYST_MODEL,
                               system=ANALYST_SYSTEM, tools=None, max_tokens=2048)
        self._add_cost(ANALYST_MODEL, resp.get("usage", {}) or {})
        self._save_state()
        return "".join(b.get("text", "") for b in resp.get("content", [])
                       if b.get("type") == "text").strip() or "(пусто)"

    # ── App rescan (manual button / /rescan, and daily at 23:00) ────────────────

    def _apps(self) -> dict:
        """Curated apps + Start-Menu-discovered apps (curated wins on name clash)."""
        return {**(self._cfg.get("discovered_apps") or {}), **(self._cfg.get("apps") or {})}

    def _active_config_dir(self):
        """CLAUDE_CONFIG_DIR of the active Claude Code account, or None for default.

        Accounts live in config 'claude_accounts' = [{email, config_dir}, ...];
        active_account is 1-based. None => use Claude Code's default login.
        """
        accounts = self._cfg.get("claude_accounts") or []
        idx = self._state.get("active_account", 1) - 1
        if 0 <= idx < len(accounts):
            return accounts[idx].get("config_dir") or None
        return None

    @staticmethod
    def _is_limit_error(error: str | None) -> bool:
        t = (error or "").lower()
        return any(m in t for m in LIMIT_MARKERS)

    def _next_account(self, current: int, accounts: list) -> int | None:
        """Next account index (1-based) after `current`, wrapping; None if <2 accounts."""
        n = len(accounts)
        if n < 2:
            return None
        return (current % n) + 1

    def _suggest_switch(self, chat_id, payload: dict):
        """Offer to switch to the next account and retry the delegation."""
        accounts = self._cfg.get("claude_accounts") or []
        cur = self._state.get("active_account", 1)
        nxt = self._next_account(cur, accounts)
        if not nxt:
            self._send_message(chat_id,
                               "⚠️ Аккаунт достиг лимита, но других настроенных аккаунтов нет.")
            return
        self._pending_delegation[str(chat_id)] = {"payload": payload, "switch_to": nxt}
        cur_email = accounts[cur - 1].get("email", f"#{cur}") if cur <= len(accounts) else f"#{cur}"
        nxt_email = accounts[nxt - 1].get("email", f"#{nxt}")
        kb = {"inline_keyboard": [[
            {"text": f"✅ Переключить на {nxt_email}", "callback_data": f"acctretry:{nxt}"},
            {"text": "✖️ Отмена", "callback_data": "acctretry:cancel"}]]}
        self._send_message(
            chat_id,
            f"⚠️ Похоже, аккаунт {cur_email} достиг лимита.\n"
            f"Переключить на {nxt_email} и повторить задачу?",
            reply_markup=kb)

    def _retry_delegation(self, chat_id, payload: dict):
        """Re-run a delegation under the now-active account (after a confirmed switch)."""
        self._set_status("active")
        payload = dict(payload)
        payload["config_dir"] = self._active_config_dir()
        cmd = {"type": "run-claude-code", "payload": payload}
        try:
            result = dispatch(cmd, self._apps(), self._cfg.get("obsidian", {}))
        except Exception as e:
            result = {"success": False, "data": None, "error": str(e)}
        if not result.get("success") and self._is_limit_error(result.get("error")):
            self._suggest_switch(chat_id, payload)  # chain to the next account
        else:
            self._deliver(chat_id, "run-claude-code", result)
        self._set_status("idle")

    # ── /claude flow: pick folder on DEV → pick model → enter task → stream ─────

    def _dev_top_folders(self) -> list[Path]:
        """Folders to offer in /claude.

        If `claude_dev_folders` is set in config (list of absolute paths) we
        return exactly those (existing dirs, in given order) — that's the
        allowlist mode. Otherwise we fall back to scanning DEV_ROOT and
        returning top-level dirs sorted by mtime (newest first).

        Also sets `self._dev_source` to 'allowlist' or 'scan' so the picker
        header can show the user which mode is in effect.
        """
        allow = self._cfg.get("claude_dev_folders") or []
        if allow:
            picked = []
            for entry in allow:
                try:
                    p = Path(entry)
                    if p.exists() and p.is_dir():
                        picked.append(p)
                except Exception:
                    continue
            self._dev_source = "allowlist"
            return picked
        dev_root_cfg = (self._cfg.get("claude_dev_root") or "").strip()
        dev_root = Path(dev_root_cfg) if dev_root_cfg else DEFAULT_DEV_ROOT
        if not dev_root.exists():
            logger.warning(f"dev root {dev_root} not found")
            self._dev_source = "scan"
            return []
        try:
            folders = []
            for p in dev_root.iterdir():
                if not p.is_dir():
                    continue
                if p.name in DEV_BLOCKLIST or p.name.startswith("."):
                    continue
                try:
                    folders.append((p, p.stat().st_mtime))
                except OSError:
                    continue
            folders.sort(key=lambda x: x[1], reverse=True)
            self._dev_source = "scan"
            return [p for p, _ in folders]
        except Exception as e:
            logger.warning(f"dev scan failed: {e}")
            self._dev_source = "scan"
            return []

    @staticmethod
    def _folder_preview(folder: Path, limit: int = 12) -> str:
        try:
            entries = list(folder.iterdir())
        except Exception as e:
            return f"(не удалось прочитать: {e})"
        if not entries:
            return "(папка пуста)"
        entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = []
        for p in entries[:limit]:
            if p.is_dir():
                lines.append(f"📁 {p.name}/")
            else:
                try:
                    sz = p.stat().st_size
                    sz_s = f"{sz/1048576:.1f} МБ" if sz >= 1048576 else f"{max(1, sz//1024)} КБ"
                except OSError:
                    sz_s = "?"
                lines.append(f"📄 {p.name} · {sz_s}")
        more = len(entries) - limit
        if more > 0:
            lines.append(f"… и ещё {more}")
        return "\n".join(lines)

    def _claude_show_folders(self, chat_id):
        scid = str(chat_id)
        folders = self._dev_top_folders()
        # Keep the per-chat list so callbacks can index back into it.
        self._pending[scid] = {"type": "claude_choosing", "folders": [str(p) for p in folders]}
        rows = [[{"text": f"📁 {p.name}", "callback_data": f"claude:f:{i}"}]
                for i, p in enumerate(folders[:20])]
        rows.append([{"text": "➕ Новая папка…", "callback_data": "claude:f:new"}])
        rows.append([{"text": "✖ Отмена", "callback_data": "claude:cancel"}])
        src = getattr(self, "_dev_source", "scan")
        if src == "allowlist":
            head = (f"🤖 *Claude Code* — выбери папку проекта "
                    f"(allowlist в config):\n_{len(folders)} разрешённых_")
        else:
            head = (f"🤖 *Claude Code* — выбери папку проекта в `{self._claude_dev_root()}`:\n"
                    f"_показаны {len(folders[:20])} последних из {len(folders)}_")
        self._send_message(chat_id, head, reply_markup={"inline_keyboard": rows},
                           parse_mode="Markdown")

    def _claude_show_models(self, chat_id, folder: Path):
        scid = str(chat_id)
        pend = self._pending.get(scid) or {}
        pend["folder"] = str(folder)
        pend["type"] = "claude_choosing"
        self._pending[scid] = pend
        preview = self._folder_preview(folder)
        active = self._state.get("active_account", 1)
        accounts = (self._cfg.get("claude_accounts") or [])
        active_email = (accounts[active - 1].get("email") if 0 < active <= len(accounts) else f"#{active}")
        rows = [[{"text": label, "callback_data": f"claude:m:{key}"}]
                for key, label in CLAUDE_MODELS]
        rows.append([{"text": f"🔑 Сменить аккаунт (сейчас {active_email})",
                      "callback_data": "menu:accounts"}])
        rows.append([{"text": "✖ Отмена", "callback_data": "claude:cancel"}])
        msg = (f"📂 Папка: `{folder}`\n\n*Содержимое:*\n{preview}\n\n"
               f"*Аккаунт:* {active_email}\n\nВыбери модель:")
        self._send_message(chat_id, msg, reply_markup={"inline_keyboard": rows},
                           parse_mode="Markdown")

    def _claude_ask_task(self, chat_id, folder: str, model: str):
        scid = str(chat_id)
        self._pending[scid] = {"type": "claude_task", "folder": folder, "model": model}
        label = next((lbl for k, lbl in CLAUDE_MODELS if k == model), model)
        self._send_message(
            chat_id,
            f"✅ {label}\n📂 {folder}\n\nТеперь пришли *задачу одним сообщением* — что должен сделать Claude в этой папке.",
            parse_mode="Markdown")

    def _claude_dev_root(self) -> Path:
        """Configured dev root for /claude, with fallback to DEFAULT_DEV_ROOT."""
        cfg_root = (self._cfg.get("claude_dev_root") or "").strip()
        return Path(cfg_root) if cfg_root else DEFAULT_DEV_ROOT

    def _cwd_is_trusted(self, cwd: str | None) -> bool:
        """True iff cwd is inside the configured claude_dev_folders allowlist.

        Empty/missing allowlist = nothing is auto-trusted: claude -p delegations
        from the AI tool will use acceptEdits (file edits but no auto-shell).
        Interactive /claude flow always passes trusted=True itself — the user
        explicitly picked the folder.
        """
        if not cwd:
            return False
        try:
            cwd_p = Path(cwd).resolve()
        except Exception:
            return False
        for entry in (self._cfg.get("claude_dev_folders") or []):
            try:
                base = Path(entry).resolve()
                if cwd_p == base or base in cwd_p.parents:
                    return True
            except Exception:
                continue
        return False

    def _claude_create_folder(self, chat_id, name: str):
        scid = str(chat_id)
        # Sanitize: take basename only, strip risky chars; reject empty/dotted.
        safe = "".join(c for c in Path(name.strip()).name if c not in '<>:"/\\|?*').strip()
        if not safe or safe in (".", ".."):
            self._send_message(chat_id, "❌ Некорректное имя. Попробуй ещё раз через /claude.")
            self._pending.pop(scid, None)
            return
        target = self._claude_dev_root() / safe
        try:
            target.mkdir(parents=False, exist_ok=True)
        except Exception as e:
            self._send_message(chat_id, f"❌ Не удалось создать папку: {e}")
            self._pending.pop(scid, None)
            return
        self._send_message(chat_id, f"✅ Создал `{target}`", parse_mode="Markdown")
        self._claude_show_models(chat_id, target)

    def _handle_claude_callback(self, chat_id, action: str):
        scid = str(chat_id)
        if action == "start":
            self._cfg = load_config()
            self._claude_show_folders(chat_id)
            return
        if action == "cancel":
            self._pending.pop(scid, None)
            self._send_message(chat_id, "Отменил.")
            return
        if action.startswith("f:"):
            arg = action[2:]
            if arg == "new":
                self._pending[scid] = {"type": "claude_new_folder"}
                self._send_message(chat_id,
                    f"📁 Пришли *имя новой папки* — создам в `{self._claude_dev_root()}`.",
                    parse_mode="Markdown")
                return
            try:
                idx = int(arg)
            except ValueError:
                return
            pend = self._pending.get(scid) or {}
            folders = pend.get("folders") or []
            if not (0 <= idx < len(folders)):
                self._send_message(chat_id, "Список папок устарел. Открой меню заново: /claude")
                return
            self._claude_show_models(chat_id, Path(folders[idx]))
            return
        if action.startswith("m:"):
            model = action[2:]
            pend = self._pending.get(scid) or {}
            folder = pend.get("folder")
            if not folder:
                self._send_message(chat_id, "Выбор папки сброшен. Открой меню заново: /claude")
                return
            self._claude_ask_task(chat_id, folder, model)
            return
        if action == "cancel-task":
            proc = self._active_claude_proc.get(scid)
            if proc is None:
                self._send_message(chat_id, "Нет активной задачи Claude Code — нечего отменять.")
                return
            try:
                proc.kill()
                self._send_message(chat_id, "❌ Отменил задачу Claude Code.")
                # Persist for the Control Center so the dashboard tile flips to "ошибка".
                self._persist_claude_task(
                    status="failed", error="отменено пользователем",
                    finished_at=time.time())
            except Exception as e:
                self._send_message(chat_id, f"Не смог убить процесс: {e}")
            return

    @staticmethod
    def _append_claude_run(record: dict) -> None:
        """Append one record to claude_runs.jsonl (one JSON per line).

        Append-only — never rewritten. The file grows ~200 bytes per run; even
        at 100 runs/day it's tiny. Truncation can be done manually if needed.
        """
        try:
            line = json.dumps(record, ensure_ascii=False)
            with CLAUDE_RUNS_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.warning(f"claude_runs append failed: {e}")

    @staticmethod
    def _read_claude_history(limit: int = 10) -> list[dict]:
        """Return the last `limit` records from claude_runs.jsonl, newest first."""
        try:
            if not CLAUDE_RUNS_PATH.exists():
                return []
            raw = CLAUDE_RUNS_PATH.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out = []
        # Walk lines from the end so we don't parse the whole file.
        for line in reversed(raw):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                break
        return out

    def _show_claude_history(self, chat_id):
        """Pretty-print the last 10 /claude runs."""
        runs = self._read_claude_history(limit=10)
        if not runs:
            self._send_message(
                chat_id,
                "📜 История пуста — ни одного запуска `/claude` ещё не было.",
                parse_mode="Markdown")
            return
        lines = ["📜 *Последние запуски Claude Code:*", ""]
        for r in runs:
            status = r.get("status", "?")
            icon = "✅" if status == "done" else "❌" if status == "failed" else "⏳"
            try:
                when = time.strftime("%d.%m %H:%M",
                                     time.localtime(float(r.get("started_at", 0))))
            except Exception:
                when = "—"
            folder_basename = Path(r.get("folder", "")).name or "?"
            elapsed = int(r.get("elapsed_sec") or 0)
            cost = r.get("total_cost_usd")
            cost_s = f" · ~${cost:.3f}" if isinstance(cost, (int, float)) else ""
            task_preview = (r.get("task") or "").strip().replace("\n", " ")[:80]
            err = r.get("error")
            err_s = f"\n     ⚠️ {err[:80]}" if (status == "failed" and err) else ""
            lines.append(f"{icon} `{when}` · 📂 `{folder_basename}` · {r.get('model', '?')} · {elapsed}s{cost_s}")
            lines.append(f"     _{task_preview}_{err_s}")
            lines.append("")
        self._send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _persist_claude_task(self, **fields):
        """Merge-write claude_task.json so the Control Center can show live status."""
        try:
            cur = {}
            if CLAUDE_TASK_PATH.exists():
                try:
                    cur = json.loads(CLAUDE_TASK_PATH.read_text(encoding="utf-8"))
                except Exception:
                    cur = {}
            cur.update(fields)
            CLAUDE_TASK_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        except Exception as e:
            logger.warning(f"claude_task persist failed: {e}")

    def _claude_run(self, chat_id, folder: str, model: str, task: str):
        """Run claude -p with streaming output → live-edit a Telegram message."""
        scid = str(chat_id)
        self._cfg = load_config()
        accounts = self._cfg.get("claude_accounts") or []
        active = self._state.get("active_account", 1)
        active_email = (accounts[active - 1].get("email") if 0 < active <= len(accounts) else f"#{active}")
        config_dir = self._active_config_dir()
        model_label = next((lbl for k, lbl in CLAUDE_MODELS if k == model), model)
        started = time.time()

        self._set_status("active")
        # Placeholder message we'll edit as chunks arrive. HTML mode because the
        # streamed body is raw model output (could contain *, _, [, `, etc. — any
        # of which would make Markdown reject the edit and silently drop the run).
        # The Cancel button stays put on this message so the user can kill a run
        # that's already streamed past the original prompt.
        cancel_kb = {"inline_keyboard": [
            [{"text": "❌ Отменить", "callback_data": "claude:cancel-task"}],
        ]}
        first = self._send_message(
            chat_id,
            f"🤖 <b>Claude Code</b> <code>{html.escape(model)}</code> запущен\n"
            f"📂 <code>{html.escape(folder)}</code>\n"
            f"🔑 {html.escape(active_email)}\n\n<i>ждём первый ответ…</i>",
            reply_markup=cancel_kb, parse_mode="HTML")
        msg_id = (first or {}).get("result", {}).get("message_id")
        if not msg_id:
            logger.warning(f"claude run: initial sendMessage failed, resp={first}")
        self._persist_claude_task(
            status="running", folder=folder, model=model, model_label=model_label,
            account_email=active_email, task=task, started_at=started,
            finished_at=None, elapsed_sec=0, total_cost_usd=None,
            last_output_tail="", error=None, chat_id=chat_id, message_id=msg_id)

        # Streaming state
        buf = {"text": "", "last_edit": 0.0, "msg_id": msg_id, "chunk_n": 1}

        def header(extra: str = "") -> str:
            elapsed = int(time.time() - started)
            return (f"🤖 Claude Code [<code>{html.escape(model)}</code>] · {elapsed}s\n"
                    f"📂 <code>{html.escape(folder)}</code> · 🔑 {html.escape(active_email)}"
                    f"{html.escape(extra)}\n\n")

        def flush(force: bool = False):
            now = time.time()
            if not force and now - buf["last_edit"] < 2.5:
                return
            buf["last_edit"] = now
            body = buf["text"]
            # Telegram: ~4096 char/message. Reserve ~400 for header. Spill into a new
            # message every ~3500 chars so the run keeps making visible progress.
            if len(body) > 3500:
                head = header(f" · часть {buf['chunk_n']}")
                trimmed = body[:3500]
                if buf["msg_id"]:
                    self._tg("editMessageText", {
                        "chat_id": chat_id, "message_id": buf["msg_id"],
                        "text": head + html.escape(trimmed) + "\n…(продолжение ↓)",
                        "parse_mode": "HTML"})
                buf["chunk_n"] += 1
                buf["text"] = body[3500:]
                new = self._send_message(
                    chat_id,
                    header(f" · часть {buf['chunk_n']}") + html.escape(buf["text"]),
                    parse_mode="HTML")
                buf["msg_id"] = (new or {}).get("result", {}).get("message_id") or buf["msg_id"]
            else:
                if buf["msg_id"]:
                    rendered = html.escape(body) if body else "<i>(ждём вывод…)</i>"
                    self._tg("editMessageText", {
                        "chat_id": chat_id, "message_id": buf["msg_id"],
                        "text": header() + rendered,
                        "parse_mode": "HTML"})
            # Mirror tail to claude_task.json (last ~600 chars) for the Control Center.
            self._persist_claude_task(
                elapsed_sec=int(time.time() - started),
                last_output_tail=(buf["text"] or "")[-600:])

        def on_event(evt: dict):
            t = evt.get("type")
            if t == "assistant":
                msg = evt.get("message") or {}
                for blk in (msg.get("content") or []):
                    if isinstance(blk, dict):
                        if blk.get("type") == "text":
                            buf["text"] += blk.get("text", "")
                        elif blk.get("type") == "tool_use":
                            # Name is appended raw; escape() runs later in flush() on the whole buf.
                            buf["text"] += f"\n🔧 [{blk.get('name', 'tool')}]\n"
                flush()
            elif t == "system":
                # init messages — keep silent to avoid noise.
                pass

        # /claude is user-driven (folder was picked or just created interactively),
        # so we trust it for full autonomy (shell + edits).
        payload = {"prompt": task, "model": model, "cwd": folder,
                   "config_dir": config_dir, "trusted": True}

        def on_proc(p):
            # Make the running subprocess reachable from the callback handler so
            # ❌ Отменить can SIGKILL it instead of waiting on the watchdog.
            self._active_claude_proc[scid] = p

        try:
            result = run_claude_code_stream(payload, on_event=on_event, on_proc=on_proc)
        finally:
            self._active_claude_proc.pop(scid, None)
        elapsed = int(time.time() - started)

        if not result.get("success"):
            err = result.get("error", "unknown error")
            self._persist_claude_task(status="failed", error=err,
                                      finished_at=time.time(), elapsed_sec=elapsed)
            self._append_claude_run({
                "started_at": started, "finished_at": time.time(), "elapsed_sec": elapsed,
                "folder": folder, "model": model, "account_email": active_email,
                "task": task, "status": "failed", "error": err, "total_cost_usd": None,
            })
            # Reactive limit handling — same UX as the AI-tool delegation flow.
            if self._is_limit_error(err):
                self._send_message(chat_id, f"⚠️ {err}\n_(см. предложение ниже о смене аккаунта)_")
                self._suggest_switch(chat_id, payload)
            else:
                flush(force=True)
                self._send_message(chat_id, f"❌ Claude Code: {err}")
            self._set_status("idle")
            return

        data = result.get("data") or {}
        cost = data.get("cost_usd")
        cost_s = f" · ~${cost:.3f}" if isinstance(cost, (int, float)) else ""
        final_text = (data.get("result") or buf["text"] or "(пусто)").strip()
        buf["text"] = final_text
        flush(force=True)
        self._send_message(
            chat_id,
            f"✅ Готово за {elapsed}s{cost_s} · {data.get('num_turns') or 0} ходов")
        self._persist_claude_task(
            status="done", finished_at=time.time(), elapsed_sec=elapsed,
            total_cost_usd=cost, last_output_tail=final_text[-600:])
        self._append_claude_run({
            "started_at": started, "finished_at": time.time(), "elapsed_sec": elapsed,
            "folder": folder, "model": model, "account_email": active_email,
            "task": task, "status": "done", "error": None,
            "total_cost_usd": cost, "num_turns": data.get("num_turns"),
        })
        self._set_status("idle")

    # ── Self-restart (refresh in-memory code after source edits) ───────────────

    def _get_version(self) -> str:
        """Short identifier of the running build. Cached after first call.

        Dev: `<git-hash> · <commit-date>` from BASE_DIR (the repo).
        Frozen: contents of a `VERSION` file next to the .exe if present,
        otherwise the .exe's mtime as a date. Returns 'dev' on any failure.
        """
        if hasattr(self, "_version_cache"):
            return self._version_cache
        v = "dev"
        try:
            if getattr(sys, "frozen", False):
                vf = BASE_DIR / "VERSION"
                if vf.exists():
                    txt = vf.read_text(encoding="utf-8").strip()
                    if txt:
                        v = txt
                else:
                    import datetime
                    mt = datetime.datetime.fromtimestamp(
                        Path(sys.executable).stat().st_mtime)
                    v = f"exe · {mt.strftime('%Y-%m-%d %H:%M')}"
            else:
                out = subprocess.check_output(
                    ["git", "-C", str(BASE_DIR), "log", "-1",
                     "--format=%h · %cd", "--date=short"],
                    stderr=subprocess.DEVNULL, timeout=3,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                ).decode("utf-8", errors="replace").strip()
                if out:
                    v = out
        except Exception as e:
            logger.warning(f"version lookup failed: {e}")
        self._version_cache = v
        return v

    def _maybe_send_welcome_back(self):
        """If the previous process flagged a self-restart, greet the user once.

        Skipped silently if the marker is missing, stale (>1h), or no chat is
        recorded. The marker is cleared on every attempt so we never replay it.
        """
        pwb = self._state.pop("pending_welcome_back", None)
        if not pwb:
            return
        try:
            self._save_state()
        except Exception:
            pass
        if time.time() - float(pwb.get("at", 0)) > 3600:
            logger.info("welcome-back marker is stale, skipping")
            return
        chat_id = pwb.get("chat_id") or self._state.get("owner_chat_id")
        if not chat_id:
            return
        ver = self._get_version()
        try:
            self._send_message(
                chat_id,
                f"✅ Я снова на связи\n🔖 Версия: <code>{html.escape(ver)}</code>",
                parse_mode="HTML")
        except Exception as e:
            logger.warning(f"welcome-back send failed: {e}")

    def _offer_agent_refresh(self, chat_id, idle_hours: float):
        """Ping the user after a long silence: source files on disk may have
        been edited while they were away, but THIS python process still runs
        the old in-memory copy. Offer a clean self-restart."""
        self._send_message(
            chat_id,
            f"⏰ Перерыв был ~{idle_hours:.1f} ч. Возможно, агент на диске обновился, "
            "пока меня не дёргали. Перезапустить, чтобы подхватить свежий код?",
            reply_markup={"inline_keyboard": [
                [{"text": "🔄 Обновить агента", "callback_data": "agent:update"}],
                [{"text": "⏭ Не сейчас", "callback_data": "agent:noupdate"}],
            ]})

    def _self_restart(self, chat_id):
        """Spawn a detached helper that waits ~3s and re-launches us, then
        os._exit() this process. Works for both `python main.py` and a frozen
        PyInstaller exe — we relaunch sys.executable + (main.py if not frozen)."""
        # Idempotency: a second click before os._exit fires would spawn a second
        # helper and the two new processes would fight over getUpdates (409 Conflict).
        if getattr(self, "_restart_in_progress", False):
            self._send_message(chat_id, "⏳ Перезапуск уже идёт — подожди ~5 секунд.")
            return
        self._restart_in_progress = True
        self._send_message(chat_id, "🔄 Обновляю агента — вернусь через ~5 секунд.")
        # Drop a marker the next process will read on startup and use to send a
        # welcome-back ping. Stamp 'at' so a crash-loop doesn't replay forever.
        self._state["pending_welcome_back"] = {"chat_id": chat_id, "at": time.time()}
        try:
            self._save_state()
        except Exception:
            pass

        if getattr(sys, "frozen", False):
            target = [sys.executable]
        else:
            # Prefer pythonw.exe so the relaunched tray doesn't flash a console.
            pyw = Path(sys.executable).with_name("pythonw.exe")
            py = str(pyw if pyw.exists() else sys.executable)
            target = [py, str(BASE_DIR / "main.py")]
        quoted = " ".join(f'"{x}"' for x in target)
        # `start ""` needs an empty title arg or it eats the first quoted token as title.
        # `&&` so the relaunch only fires if the timeout succeeded.
        helper = f'timeout /t 3 /nobreak >nul && start "" /B {quoted}'

        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        try:
            subprocess.Popen(
                ["cmd", "/c", helper],
                creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                close_fds=True, cwd=str(BASE_DIR),
            )
        except Exception as e:
            logger.error(f"self-restart spawn failed: {e}")
            self._send_message(
                chat_id,
                f"❌ Не смог запустить новый процесс: {e}\nПерезапусти вручную из трея.")
            return

        logger.info("self-restart: detached helper spawned, exiting now")
        time.sleep(0.5)  # give Popen a moment to fully detach before we die
        os._exit(0)      # hard exit: drop the long-poll connection instantly so the
                         # relaunched instance doesn't hit 409 Conflict on getUpdates

    def _trigger_rescan(self, chat_id):
        """Run a rescan in a worker thread so the polling loop isn't blocked."""
        self._send_message(chat_id, "🔄 Сканирую установленные приложения…")
        threading.Thread(target=self._run_rescan,
                         kwargs={"chat_id": chat_id, "manual": True},
                         daemon=True, name="AppRescan").start()

    def _run_rescan(self, chat_id=None, manual=False) -> dict:
        self._set_status("active")
        try:
            result = rescan_apps()
            self._cfg = load_config()  # pick up merged apps / discovered_apps
        except Exception as e:
            logger.error(f"rescan error: {e}")
            result = {"error": str(e)}
        self._set_status("idle")
        target = chat_id or self._state.get("owner_chat_id")
        msg = self._format_rescan(result, manual)
        if target and msg:
            self._send_message(target, msg)
        return result

    @staticmethod
    def _format_rescan(result: dict, manual: bool):
        if result.get("error"):
            return f"🔄 Парсинг приложений: ошибка — {result['error']}"
        added = result.get("added", {})
        updated = result.get("updated", {})
        if not added and not updated:
            return "🔄 Новых приложений не найдено." if manual else None
        lines = ["🔄 Обновление приложений:"]
        lines += [f"➕ {k}" for k in added]
        lines += [f"♻️ {k}" for k in updated]
        lines.append(f"\nВсего доступно: {result.get('total_apps', '?')}")
        return "\n".join(lines[:60])

    def _scheduler_run(self):
        """Fire rescan_apps once per day at 23:00 local time."""
        import datetime
        while not self._stop.is_set():
            now = datetime.datetime.now()
            target = now.replace(hour=23, minute=0, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            if self._stop.wait((target - now).total_seconds()):
                break
            try:
                self._run_rescan(manual=False)
            except Exception as e:
                logger.error(f"scheduled rescan error: {e}")

    # ── Helpers ──────────────────────────────────────────────────────────────────

    def _set_status(self, status: str):
        if self._status != status:
            self._status = status
            self._on_status_change(status)
