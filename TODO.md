# Dispatch — TODO

> Единый список задач проекта. Под каждой задачей — **промпт**: что конкретно делать, какие файлы трогать, как проверить.
>
> **Правила:**
> 1. Когда задача выполнена — поменять `[ ]` на `[x]`, добавить строку `> ✅ Сделано <дата>: <короткое описание что именно сделано и куда>`.
> 2. Если задача разбита на под-шаги — отметить каждый под-шаг.
> 3. Новые задачи добавлять в конец соответствующего приоритета.
> 4. Промпт можно править если стало понятнее, как делать — но не удалять историю того что сделано.
>
> Контекст проекта: Windows tray-агент, управляет ПК через Telegram. n8n/Railway удалены 26.05.2026, Telegram-бот теперь внутри `agent/telegram_bot.py`. Архитектура: `main.py` → `agent/telegram_bot.py` (long-poll) → `agent/handlers.py` (выполнение команд).

---

## ✅ Сделано в этой сессии (2026-06-02)

- [x] **Аудит логов агента** — проанализированы `agent.log`, `watchdog.log`, `backup.log`. Найдена живая петля рестартов.
  > ✅ 2026-06-02: причина — устаревший callback из getUpdates → `_self_restart` → потеря offset → тот же callback → петля.

- [x] **Account switcher для Claude Code** — переключение между OAuth-аккаунтами одной командой.
  > ✅ 2026-06-02: создан `C:\Users\User\.claude\claude-switch.ps1` + функция-обёртка в `$PROFILE`. Команды: `list / current / save <name> / use <name> / delete <name>`. Профиль = `.credentials.json` + `settings.json` + `settings.local.json`. Бэкапы при `use` идут в `~/.claude/accounts/.backup-<дата>/`.

- [x] **Фикс петли рестартов `telegram_bot.py`** — два бага в одном.
  > ✅ 2026-06-02: 
  > - `_answer_callback` теперь возвращает `(ok, expired)`. См. [agent/telegram_bot.py:441](agent/telegram_bot.py:441).
  > - `_handle_callback` дропает action при `expired=True`. См. [agent/telegram_bot.py:718](agent/telegram_bot.py:718).
  > - `offset` из getUpdates персистится в `_state["tg_offset"]` и сохраняется до обработки update. См. [agent/telegram_bot.py:374](agent/telegram_bot.py:374).
  > - Синтаксис проверен (`python -m py_compile`).
  > - ⚠️ Фикс в коде, **но процесс агента надо перезапустить вручную**, иначе в памяти старая версия (см. P0 #1).

- [x] **Схема оркестратора Haiku+CLI** — design approved.
  > ✅ 2026-06-02: схема согласована (см. чат). Стек: SQLite-очередь → Dispatcher → router → HaikuClient / ClaudeCliClient / computer-use → SonnetReviewer (на destructive шагах) → Reporter в Telegram. БД таблицы: `tasks` (id, kind, prompt, status, …), `runs` (id, task_id, model, cost, …). Дневной cost-cap. Кнопка Dispatch ON/OFF.

- [x] **Аудит продакшна** — собран список реальных проблем (вне облачного аудита Railway).
  > ✅ 2026-06-02: см. чат + этот TODO.md.

---

## 🔴 P0 — критично, делать прямо сейчас

### [ ] 1. Перезапустить агент И watchdog чтобы подхватить новый код
**Промпт:** Это делает пользователь руками (я не могу убить чужие процессы). На 2026-06-02 живы: агент PID 47536 (`pythonw main.py`) и watchdog PID 47468 (зависший). Оба со старым кодом.
1. Убить оба: `taskkill /F /PID 47536` и `taskkill /F /PID 47468` (PID-ы свежие — перепроверь актуальные: `Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | ? { $_.CommandLine -like '*dispatch*' } | select ProcessId,CommandLine`).
2. Запустить агент заново: `pythonw E:\dispatch\main.py` (или через иконку в трее).
3. Запустить watchdog заново: `pythonw E:\dispatch\watchdog.py`.
4. Проверить `agent.log`: `Dispatch Agent starting` → `Telegram bot polling started`, и **НЕТ** `self-restart: detached helper spawned` каждые 15 сек.
5. Проверить `watchdog.log`: новая строка `watchdog starting · … · debounce=2 · cooldown=300s` (значит подхватился мой код P0.2).
6. Тест: в Telegram нажать любую кнопку — срабатывает без рестарта.
7. Тест single-instance: попробовать запустить `pythonw E:\dispatch\main.py` второй раз — в `agent.log` должно быть `another agent instance is already running — exiting`, второй процесс сразу умирает.

---

### [x] 2. Watchdog auto-restart
**Промпт:** Сейчас `watchdog.py` только логирует `ok · agent alive · pids=[…]` каждые 10 минут, но если pids пустой — он молча ничего не делает. Сделать чтобы:
1. Прочитать текущий [watchdog.py](watchdog.py), понять как он находит pid.
2. Если `pids == []` подряд **2 проверки** — запускать новый процесс агента.
3. Cooldown между попытками.
4. Логировать действие.
5. Smoke-тест.
> ✅ Сделано 2026-06-02:
> - **Поправка посылки:** watchdog УЖЕ перезапускал агент (`spawn_agent()` на бывшей строке 127). Реальная проблема была не в отсутствии heal, а в **отсутствии debounce**: watchdog спавнил после первого же пустого опроса и попадал в окно self-restart агента (~3-5 сек без процесса) → запускал ВТОРОЙ агент → `Conflict: terminated by other getUpdates` (видно в логах 27.05, 01.06).
> - Добавлены константы `FAILS_BEFORE_SPAWN=2` (debounce) и `MIN_RESTART_GAP=300` (cooldown). См. [watchdog.py:27](watchdog.py:27).
> - Переписан главный цикл: спавн только после 2 опросов подряд с пустым pids И если с прошлого спавна прошло ≥5 мин. См. [watchdog.py:113](watchdog.py:113). Логирование «agent back», «holding off», «spawning fresh process».
> - Синтаксис проверен (`py_compile`).
> - Финальную защиту от дублей закрывает P1.4 (lock в main.py): даже если watchdog переспавнит — второй агент умрёт на lock.
> - ⚠️ Отдельная проблема: watchdog.log замолчал после 18:15 02.06 — значит сам watchdog-процесс не работал. Он в HKCU Run, стартует при логине. Проверить что он реально запущен: см. новую задачу P1.5b ниже.

---

## 🟠 P1 — серьёзно, в течение недели-двух

### [x] 5b. Проверить что watchdog реально в автозагрузке и запущен
**Промпт:** watchdog.log замолчал после 18:15 02.06 — проверить HKCU Run + живой ли процесс.
> ✅ Сделано 2026-06-02 (диагностика):
> - ✅ `HKCU\...\Run\AgentWatchdog = pythonw.exe E:\dispatch\watchdog.py` — присутствует.
> - ⚠️ Запущенный watchdog PID 47468 **завис**: последняя запись 18:15 с `pids=[12684]`, но текущий агент PID 47536 — смену не заметил, цикл не крутится (вероятно застрял в subprocess `wmic`/powershell). Нужен рестарт (войдёт в P0.1).
> - ⚠️ **Split-brain frozen vs dev** (новая задача P1.5c): HKCU Run содержит И `DispatchAgent = dist\DispatchAgent.exe` (frozen, данные в %APPDATA%\AgentOS), И `AgentWatchdog`→`pythonw main.py` (dev, данные в E:\dispatch). `%APPDATA%\AgentOS` не существует → frozen exe ни разу не отработал. Реальный конфиг (telegram token SET, owner 8761312498) только в `E:\dispatch\config.json`. Де-факто работает dev.

### [ ] 5c. Консолидировать deployment: frozen exe ИЛИ dev main.py (выбрать одно)
**Промпт:** Сейчас раздвоение (см. 5b). Два пути запуска агента с разными data-dir — источник путаницы и потенциальных двойных конфигов.
Решение за пользователем — выбрать режим:
- **Вариант DEV (рекомендую пока идёт разработка):** убрать из HKCU Run ключ `DispatchAgent` (frozen). Оставить только `AgentWatchdog`, который поднимает `pythonw main.py`. Данные — `E:\dispatch`. Минус: при логине агент стартует не мгновенно, а после первого опроса watchdog (до 60 сек).
  - Команда: `Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name DispatchAgent`
  - Дополнительно можно добавить прямой автозапуск dev-агента: `Set-ItemProperty ... -Name DispatchAgentDev -Value 'pythonw.exe E:\dispatch\main.py'`
- **Вариант FROZEN (для «прода»):** пересобрать exe (`pyinstaller DispatchAgent.spec`), убедиться что `%APPDATA%\AgentOS\config.json` заполнен (скопировать из `E:\dispatch\config.json`), и переключить watchdog чтобы он спавнил exe, а не main.py (правка `spawn_agent()` в watchdog.py).
- ⚠️ Какой бы вариант ни выбрал — единый mutex (P1.4) гарантирует что одновременно живёт ОДИН агент, так что катастрофы «два бота» уже не будет. Но data-dir надо свести к одному.

### [ ] 3. Удалить 2 неиспользуемых Railway Postgres
**Промпт:** Это делает пользователь (доступа к Railway у меня нет). По данным аудита Railway:
- Postgres ID `3637aa89-c512-44b7-8ad5-832dc21fcfcb` (1.1 GB)
- Postgres-xxzk ID `0d0e7f4e-4842-4fa0-80fc-ede2ab8fd93b` (2.2 GB)

Оба не используются (CPU ≈ 0). Это остатки от n8n которая удалена 26.05.
**Действие:** перед удалением — Railway → проект → каждый Postgres → Settings → `Backup` (на всякий случай экспорт дампа в локальный файл, ~3 GB суммарно), потом `Delete service`.

Если впоследствии понадобится PG — поднять локально в Docker или взять отдельный (так как новая task DB всё равно будет SQLite, см. P3).

---

### [x] 4. Single-instance guard для агента
**Промпт:** В `agent.log` за 27.05 видны серии `Conflict: terminated by other getUpdates request` — два экземпляра агента одновременно. Нужно гарантировать один процесс.
> ✅ Сделано 2026-06-02:
> - Создан [agent/singleton.py](agent/singleton.py) — guard через **Windows named mutex** (`Global\DispatchAgentSingleton`). Выбран mutex вместо file lock: ОС сама освобождает его при смерти процесса (даже kill -9), не остаётся устаревших lock-файлов. Fallback на POSIX `flock` для не-Windows. Fail-open при ошибке примитива (лучше запуститься, чем заклинить).
> - Подключён в [main.py](main.py): в начале `main()` до `ensure_config()` — если `acquire()` вернул False, лог `another agent instance is already running — exiting` + `sys.exit(1)`.
> - Освобождение не нужно явно — ОС закрывает handle при выходе процесса.
> - **Тест пройден:** процесс A acquire → True, процесс B (отдельный subprocess) → False. Синтаксис обоих файлов проверен.
> - Синергия с P0.2: даже если watchdog переспавнит во время self-restart, второй агент умрёт на mutex.

---

### [x] 5. Починить backup — добавить Google API клиент в deps
**Промпт:** В `backup.log` 01.06 18:37: `[ERROR] missing deps: pip install google-auth-oauthlib google-api-python-client`.
> ✅ Сделано 2026-06-02:
> - **Поправка посылки:** в `requirements.txt` пакеты УЖЕ были (строки 18-20). Корень — они просто не были `pip install`'нуты в рантайме (`ModuleNotFoundError: No module named 'googleapiclient'`). Источник ошибки — [agent/backup_daemon.py:326](agent/backup_daemon.py:326) (ImportError → "missing deps").
> - Установлены пакеты: `google-api-python-client 2.197.0`, `google-auth-oauthlib 1.4.0`, `google-auth-httplib2 0.4.0` (+ транзитивные google-auth, googleapis-common-protos и т.д.).
> - Проверено: все 5 импортов из backup_daemon/backup (`google.oauth2.credentials`, `google_auth_oauthlib.flow`, `google.auth.transport.requests`, `googleapiclient.discovery`, `googleapiclient.http`) резолвятся.
> - Добавлены эти модули в `hiddenimports` в [DispatchAgent.spec](DispatchAgent.spec) — иначе frozen .exe их не подхватит (lazy import не виден статанализу PyInstaller). Spec проверен на валидность.
> - ⚠️ Полный прогон backup не тестировал — нужен OAuth config (`backup.google_oauth.client_id`/`client_secret` + токен). Но ошибка `missing deps` устранена точно. После настройки OAuth проверить в `backup.log` успешную загрузку.

---

## 🟡 P2 — техдолг и уборка

### [x] 6. Коммит и пуш текущих изменений
> ✅ Сделано 2026-06-02:
> - Создана ветка `chore/stabilize-and-cleanup` (от main, т.к. нельзя коммитить напрямую в default).
> - 5 логических коммитов: (A) rm n8n leftovers, (B) telegram bot + paths, (C) backup daemon + deps + spec, (D) stability: singleton/watchdog/log-rotation, (E) gitignore + archive + TODO.md.
> - Запушено в `origin`.
> - ⏳ **PR создать вручную** (`gh` CLI не установлен): открыть https://github.com/AdilMombekov/dispatch-agent/pull/new/chore/stabilize-and-cleanup — или смержить ветку локально (`git checkout main; git merge chore/stabilize-and-cleanup`).
> - **НЕ закоммичено намеренно:** `agent_os_app/`, `agent_os_tabs/`, `agent_os_control_center.html`, `demo/` — отдельные решения.
> - ⚠️ **Безопасность:** в `.git/config` remote URL содержит GitHub PAT (`gho_…`) в открытом виде. Только локально (не в репо), но стоит ротировать токен.

<details><summary>Исходный промпт</summary>

**Промпт:** В `git status` болтаются крупные незакоммиченные правки (см. начало сессии):
- `M agent/config.py`, `M agent/handlers.py`, `M main.py`, `M requirements.txt`
- `D agent/poller.py` (удалён вместе с n8n)
- `?? agent/backup.py`, `?? agent/backup_daemon.py`, `?? agent/paths.py`, `?? agent/telegram_bot.py`

Шаги:
1. `git diff` — посмотреть весь объём, разбить на разумные коммиты:
   - **Коммит A:** "rm: n8n leftovers (poller, workflow.json, patch_workflow.py, update_system_prompt.py)"
   - **Коммит B:** "feat(telegram): direct Telegram bot inside agent (telegram_bot.py + handlers wire-up + state)"
   - **Коммит C:** "feat(backup): Google Drive backup daemon + paths helper"
   - **Коммит D:** "fix(telegram_bot): callback expiry + offset persistence (anti restart-loop)" — это уже **в этой сессии** сделанные правки.
2. Сделать PR в main (или закоммитить напрямую в main — на усмотрение).
3. **Не коммитить:** `config.json.bak`, `config.json.broken-27.05`, `agent_state.json`, `chats.json`, `_npm_start.*`, `build_log.txt`, `agent-os-*.zip`, `agent_os_app/`, `demo/`, `inbox/`. Это либо локальный state, либо мусор. См. P2 #7.

</details>

---

### [x] 7. Удалить мусор из репо
**Промпт:** В корне свалка от прошлых сессий. Удалить мусор, обновить .gitignore, спросить про спорное.
> ✅ Сделано 2026-06-02:
> - **Удалено безвозвратно (untracked мусор):** `_npm_start.err`, `_npm_start.log`, `_live_agent.log`, `agent-os-main.zip` (17.7 МБ), `agent-os-master.zip` (17.7 МБ), `agent-os-main/`, `build_log.txt`. Освобождено ~35 МБ.
> - **Архивировано в `docs/archive/`** (по решению юзера, не удалено): 12 планнинг-файлов — `NEXT_SESSION.md`+`STEP2..7`, `SESSION_HANDOFF.md`, `AUDIT_AND_PLAN.md`, `MVP_PLAN.md`, `TEST_PLAN.md`, `TEST_RESULTS.md`.
> - **`.gitignore` переписан:** добавлены runtime state (`agent_state.json`, `chats.json`, `backup_index.json`, `claude_task.json`, `claude_runs.jsonl`, `inbox/`), `config.json.*`, `node_modules/`, `_npm_start.*`, `*.zip`. Разигнорен `DispatchAgent.spec` (`!DispatchAgent.spec`) — нужен в гите для воспроизводимой сборки (там hiddenimports из P1.5).
> - **n8n-файлы** (`n8n_workflow.json`, `patch_workflow.py`, `update_system_prompt.py`) — уже помечены `D` в git, закоммитятся в P2.6.
> - ⏳ **`demo/primes.py`** — тривиальный demo (sieve первых 10 простых, артефакт теста). Показан юзеру, ждёт его решения удалять/нет. `agent_os_app/`, `agent_os_tabs/`, `agent_os_control_center.html` — оставлены (Electron Control Center, реальный код). `inbox/` — оставлена (это INBOX_DIR из paths.py, runtime-данные).

---

### [x] 8. Log rotation для agent.log
**Промпт:** `agent.log` уже 37 KB и растёт. Нет ротации — когда-то распухнет.
> ✅ Сделано 2026-06-02:
> - `agent.log` → `RotatingFileHandler(maxBytes=10MB, backupCount=5)` в [main.py](main.py) (logging.basicConfig handlers).
> - `watchdog.log` → `RotatingFileHandler(maxBytes=5MB, backupCount=3)` в [watchdog.py](watchdog.py) (basicConfig переведён с `filename=` на `handlers=[]`).
> - `backup.log` → `RotatingFileHandler(maxBytes=5MB, backupCount=3)` в [agent/backup_daemon.py](agent/backup_daemon.py).
> - Пути к логам не трогал — они уже идут через `paths.py` (frozen→%APPDATA%, dev→root). Синтаксис всех трёх проверен.

---

## 🟢 P3 — оркестратор Haiku+CLI (по шагам)

> Архитектура согласована. Реализация в папке `agent/orchestrator/`. Каждый шаг — отдельный коммит и тестируемый чекпойнт.

### [x] 9. Шаг 1 — шасси: SQLite-очередь + Dispatcher-worker + Telegram-команды
> ✅ Сделано 2026-06-02:
> - `agent/orchestrator/queue.py` — `TaskQueue` (SQLite, WAL, потокобезопасно). Методы: `enqueue/claim_next/mark_done/mark_failed/cancel/record_run/get/list/cost_since`. Таблицы `tasks`+`runs`. Recovery орфанов (running→queued при рестарте). **Юнит-тест зелёный** (FIFO, cancel-guard, cost-sum, recovery).
> - `agent/orchestrator/dispatcher.py` — worker-тред с инъекцией `is_enabled`/`report`. Роутинг по `kind` через `register()`; стаб-фоллбэк для неподключённых исполнителей. **Интеграционный тест зелёный** (стаб, кастомный executor, тоггл ON/OFF).
> - `agent/orchestrator/router.py` — `classify()` (эвристика qa/code/click_gui). **Создан раньше плана** (был нужен для `/q`); Haiku-классификатор остаётся в P3.10. **Тест 7/7**.
> - Интеграция в `telegram_bot.py`: команды `/q <prompt>`, `/tasks`, `/cancel <id>`, `/dispatch [on|off]`. Очередь+dispatcher создаются в `__init__`, стартуют/стопаются в `start()/stop()`. `dispatch_enabled` в state (default **OFF** — юзер опт-ин). report = `_send_message`.
> - **Отклонение от плана:** dispatcher стартует внутри `bot.start()`, а не в `main.py` (чище инкапсуляция) — main.py не трогал.
> - `tasks.db*` добавлены в .gitignore. Все файлы компилируются, модуль импортируется чисто.
> - ⚠️ Заработает только после рестарта агента (P0.1).

<details><summary>Исходный промпт</summary>
**Промпт:** Базовый каркас без AI. Цель — чтобы можно было ставить задачи в очередь, видеть их статус, переключать Dispatch ON/OFF.

Файлы:
- `agent/orchestrator/__init__.py` — пустой.
- `agent/orchestrator/queue.py`:
  - Класс `TaskQueue(db_path)` с методами `enqueue(chat_id, kind, prompt, cron=None) -> task_id`, `next() -> Task | None` (берёт queued, ставит running), `mark_done(task_id, result, runs)`, `mark_failed(task_id, error)`, `cancel(task_id)`, `list(status=None, limit=20)`, `get(task_id)`.
  - Схема таблиц `tasks` и `runs` (см. чат). Создавать через `CREATE TABLE IF NOT EXISTS`.
  - Локация БД: `paths.appdata() / "tasks.db"` (использовать существующий `agent/paths.py`).
- `agent/orchestrator/dispatcher.py`:
  - Класс `Dispatcher(queue, state)` — фоновый поток (через `threading.Thread(daemon=True)`).
  - Цикл: проверяет `state["dispatch_enabled"]` → если ON, берёт `queue.next()`, помечает started, **на этом шаге выполняет фейково**: ждёт 2 сек, `mark_done(task_id, result="(orchestrator not wired yet)", runs=[])`. На следующих шагах подключим Haiku/CLI.
  - Stop: через `threading.Event`.

Интеграция с Telegram-ботом (`agent/telegram_bot.py`):
- Команда `/q <prompt>` → `queue.enqueue(chat_id, kind="qa", prompt=…)` → ответ `🟢 Задача #<id> поставлена в очередь.`
- Команда `/tasks` (или callback на кнопку «Задачи») → список последних 10 задач со статусами.
- Команда `/cancel <id>` → `queue.cancel(id)`.
- В меню добавить кнопку с переключателем: текст `🤖 Dispatch: ON` / `🤖 Dispatch: OFF` → callback `dispatch:toggle` → `state["dispatch_enabled"] = not …`.

Запуск Dispatcher: в `main.py` после старта Telegram-бота — `dispatcher.start()`.

Проверка:
- `/q hello` → ответ с id, через ~2 сек статус становится `done`.
- Если `Dispatch: OFF` — задачи копятся в `queued`, не выполняются.
- Перезапуск агента — задачи в БД сохраняются.

</details>

---

### [x] 10. Шаг 2 — HaikuClient для kind=qa
> ✅ Сделано 2026-06-02:
> - **Отклонение от плана (обосновано):** не плодил `clients.py:HaikuClient` на SDK. Вместо этого переиспользовал существующий `_anthropic()` (HTTP) + `_add_cost()` бота — один счётчик трат, а не два. qa-executor = метод бота `_qa_executor`, зарегистрирован в dispatcher через `register("qa", …)`.
> - `_active_anthropic_key()` — берёт ключ из `anthropic_api_keys[active_account-1]` (с фоллбэком на свежий `load_config()`).
> - Модель `ROUTER_MODEL` (claude-haiku-4-5), `QA_SYSTEM_PROMPT` (кратко, по-русски), `max_tokens=1024`, без tools.
> - Soft-лимит: если `spend.total >= spend.limit` — задача падает с понятной ошибкой (полноценный budget в P3.13).
> - Стоимость пишется и в meter (`_add_cost`), и в `runs` (`record_run`) для /tasks.
> - **Реальный e2e тест пройден:** `/q «сколько 2+2»` → ответ «4», cost $0.000086, run-row (82 in/5 out, ok=1). Изолированно (temp db + no-op save_state, чтобы не трогать живой state).

<details><summary>Исходный промпт</summary>

**Промпт:** Простые вопросы через Anthropic API (модель Haiku) без tools.

Файлы:
- `agent/orchestrator/clients.py`:
  - Класс `HaikuClient(api_key, model="claude-haiku-4-5-20251001")` с методом `qa(prompt: str) -> tuple[str, dict]` где dict содержит `tokens_in/out`, `cost_usd`. Использовать `anthropic` SDK (добавить в `requirements.txt`).
  - Prompt каркас: system = "Ты ассистент Dispatch. Отвечай коротко и по делу, на русском, без лишних оговорок.", user = prompt задачи.
- `agent/orchestrator/router.py`:
  - Функция `classify(prompt: str) -> kind` — пока эвристика: если в prompt есть глаголы «открой/нажми/кликни/закрой/запусти приложение» → `click_gui`; если «закоммить/git/файл/код/функция/baghidi/refactor» → `code`; иначе → `qa`. (Haiku-классификатор подключим позже.)

Интеграция:
- В `dispatcher.py` заменить фейк на `if task.kind == "qa": result, run = self.haiku.qa(task.prompt)`. Записать `run` в таблицу `runs`. Остальные `kind` пока возвращают «не реализовано».

Конфиг:
- ⚠️ **Поправка (проверено 2026-06-02):** ключ лежит в `config.json` → `anthropic_api_keys` (МАССИВ, не строка; сейчас 1 элемент). Брать `cfg["anthropic_api_keys"][active_account-1]` или `[0]`. Не выдумывать новый `anthropic_api_key`. Если массив пуст — `[WARN]` + qa-задачи failed.
- Лимит трат уже есть: `config.json → api_limit_usd` + `state["spend"]`. Переиспользовать для budget (P3.13), не плодить второй счётчик.

Проверка:
- `/q сколько будет 2+2` → через несколько секунд приходит ответ от Haiku, в БД есть запись в `runs` с tokens/cost.

</details>

---

### [x] 11. Шаг 3 — ClaudeCliClient для kind=code
> ✅ Сделано 2026-06-02:
> - **Отклонение от плана (обосновано):** не делал `clients.py:ClaudeCliClient`. Переиспользовал существующий `run_claude_code_stream()` из handlers.py (синхронно, on_event=None) + готовые хелперы бота `_active_config_dir()` и `_claude_dev_root()`. Executor = `_code_executor`, зарегистрирован через `register("code", …)`.
> - payload: model=sonnet, cwd=dev root, config_dir=активный аккаунт, trusted=True, timeout=1800. Результат тримится до 3500 символов (лимит Telegram). Стоимость (subscription) пишется в `runs`.
> - Команда `/c <prompt>` — явный запуск code-задачи минуя классификатор (`force_kind="code"`). Также router сам отправляет «закоммить/git/код…» в code.
> - `claude` CLI подтверждён на PATH.
> - **Проверено:** wiring (executors qa+code зарегистрированы), синтаксис. **Реальную code-задачу НЕ запускал автоматически** — claude -p trusted может изменить файлы. Тестировать юзеру через Telegram на безопасном промпте.
> - ⚠️ **Без guard'а:** code-задачи выполняются trusted без ревью до P3.13 (SonnetReviewer). dispatch OFF по умолчанию — опт-ин.

<details><summary>Исходный промпт</summary>

**Промпт:** Тяжёлые задачи (правка кода, git) — через subprocess `claude -p <prompt>` (это claude CLI, headless mode). Использует активный профиль (см. `claude-switch.ps1`).

Файлы:
- В `agent/orchestrator/clients.py` добавить класс `ClaudeCliClient(workdir, model=None)`:
  - Метод `code(prompt: str) -> tuple[str, dict]`.
  - Под капотом: `subprocess.run(["claude", "-p", prompt, "--add-dir", workdir, "--output-format", "text"], capture_output=True, text=True, timeout=600)`.
  - Возвращает stdout как result, в dict кладёт `duration_s`, `model="claude-cli"`. Cost через CLI вытащить сложно — оставить `None` пока.

Интеграция:
- В `dispatcher.py`: `elif task.kind == "code": result, run = self.cli.code(task.prompt)`.
- В Telegram — команда `/c <prompt>` ставит задачу с `kind="code"`.

Проверка:
- `/c покажи структуру agent/` → CLI запускается, ответ возвращается в Telegram.

</details>

---

### [ ] 12. Шаг 4 — computer-use для kind=click_gui
**Промпт:** Самый сложный шаг. Haiku с computer-use tool для GUI-задач. Юзер сидит у ПК и видит как курсор бегает (это норма — это его машина).

Файлы:
- В `clients.py` добавить `ComputerUseClient(api_key, model="claude-haiku-4-5-20251001")`:
  - Метод `click_gui(prompt: str, screenshot_fn, action_fn) -> tuple[str, dict]`.
  - Цикл: API call с tools=[`computer_20250124`] → если tool_use → выполнить через `action_fn` (mouse_move/left_click/type/key/screenshot) → screenshot → следующий шаг → пока модель не вернёт `end_turn`.
  - Бюджет шагов: max_steps=20, max_tokens_per_step=2000.
- `agent/orchestrator/desktop.py`:
  - Класс `Desktop` оборачивающий PyAutoGUI или mss/pynput. Методы: `screenshot() -> bytes`, `mouse_move(x,y)`, `left_click(x,y)`, `type(text)`, `key(name)`, `scroll(direction)`.
  - Безопасность: failsafe (если курсор в углу экрана → abort) + emergency stop (запись в `state["dispatch_emergency_stop"]` → Dispatcher прерывает текущий run).

Интеграция:
- В `dispatcher.py`: `elif task.kind == "click_gui": result, run = self.cu.click_gui(task.prompt, desktop.screenshot, desktop.act)`.
- В Telegram: кнопка «🖥️ Кликнуть» в меню, или команда `/click <prompt>`.

Проверка:
- `/click открой Chrome и зайди на example.com` → Haiku открывает, в Telegram отчёт «✅ Открыл, на нужной странице».

---

### [ ] 13. Шаг 5 — SonnetReviewer + budget.py
**Промпт:** Защитная сетка: на destructive шагах перед действием Haiku Sonnet делает быстрое ревью.

Файлы:
- `agent/orchestrator/reviewer.py`:
  - Класс `SonnetReviewer(api_key, model="claude-sonnet-4-6")` с методом `review(action: dict, context: str) -> dict` где результат `{"verdict": "allow|deny|fix", "reason": str, "fix": Optional[dict]}`.
  - Дёргается только когда action попадает в чёрный список: `git push`, `git commit -a`, `rm`, `Remove-Item`, отправка сообщения третьей стороне, закрытие приложений со значимым состоянием.
- `agent/orchestrator/budget.py`:
  - Считает суммарную стоимость за сегодня по таблице `runs`.
  - При 80% от лимита (`config["daily_budget_usd"]`, default 7.0) — Telegram-предупреждение пользователю.
  - При 100% — Dispatcher не берёт новые задачи (статус остаётся `queued`), пишет в state причину `budget_exhausted`.

Интеграция:
- В клиентах (`HaikuClient`, `ComputerUseClient`): перед destructive action — вызвать `reviewer.review(...)`. Если deny — отменить шаг.

Проверка:
- Запустить click-задачу которая хочет `Remove-Item` — Sonnet должен заблокировать.
- Запустить N qa-задач подряд, превысить дневной лимит — Dispatcher останавливается.

---

### [ ] 14. Шаг 6 — APScheduler для kind=scheduled
**Промпт:** Cron-задачи: «каждое утро в 8:00 проверь почту и пришли сводку».

Файлы:
- `agent/orchestrator/scheduler.py`:
  - `Scheduler(queue)` обёртка над `APScheduler` (BackgroundScheduler).
  - На старте читает из `tasks` все строки с `cron IS NOT NULL` и `status = "scheduled"`, регистрирует cron-job.
  - Каждый job — `enqueue(chat_id, kind=<derived>, prompt=…)` (новая копия задачи, исходный шаблон остаётся со статусом `scheduled`).

Интеграция:
- В Telegram: команда `/cron "<cron>" <prompt>` — пример `/cron "0 8 * * *" проверь почту и пришли сводку`.
- Команда `/crons` — список scheduled, `/uncron <id>` — снять.

Добавить в `requirements.txt`: `apscheduler`.

Проверка:
- `/cron "*/2 * * * *" пришли время` — каждые 2 минуты прилетает время от Haiku.

---

## История изменений TODO.md

- 2026-06-02: создан, перенесены задачи из чата с разбиением на P0/P1/P2/P3.
