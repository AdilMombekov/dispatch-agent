# Agent-OS Dispatch — план до MVP

> Документ-памятка для следующих сессий. Каждый шаг — отдельная сессия.
> Перед началом работы скопируй блок **«Промт для новой сессии»** под нужным шагом
> и вставь как первое сообщение в свежий `claude`.

---

## Что уже сделано (на 2026-05-26)

- **Python-агент** (`agent/poller.py`, `agent/handlers.py`, `agent/config.py`, `main.py`) — есть, поллит каждые 2 с, пишет в `agent.log`.
- **n8n workflow** — `workflow_fixed.json` готов к импорту в Railway.
- **Electron Control Center** (`agent_os_app/`) — рабочий, нативный macOS-Settings вид, Liquid Glass.
  - `main.js` + `preload.js` — IPC: `config:read/write`, `log:tail`, `system:info`, `shell:openPath`, **`metrics:host`**, **`agent:status`**, **`events:recent`**, **`workflow:read`**.
  - `renderer/app.html` — 12 разделов, текст простым языком, **подключён к реальным данным**: CPU/RAM/Диск, статус агента по mtime лога, события из `agent.log` с русским текстом, маскированный токен, реальный URL/poll-interval, рабочий тоггл «Автозапуск» (пока только в `config.json`).
  - **Вкладка «Воркфлоу n8n»** — рисует реальный граф из `workflow_fixed.json` как n8n-канвас (узлы с цветными иконками по типу, bezier-связи, dot-grid, зум +/−/fit). Lazy-render при первом открытии.
  - **Узлы кликабельные** — клик по любому узлу открывает правую панель с цветной иконкой, плейн-описанием (34 курированных описания, плюс fallback по типу для будущих узлов) и параметрами/кодом (для code-узлов — JS-тело, для webhook — метод+путь+режим ответа, для telegram — кому+текст+клавиатура, для if/switch — условия). Закрытие × или Escape.
  - **Канвас в максимуме** — страница `#p-workflow` без cap'а ширины, viewport 74vh, **pan мышью** (зажать и тащить), **Ctrl + колесо** для зума (плюс кнопки + / − / fit), авто-fit при первом открытии.
- **Запуск UI**: `cd agent_os_app && npm start` → нативное окно Electron с живыми данными.
- **Запуск UI в браузере для проверки**: `cd agent_os_tabs && python -m http.server 8000` → http://127.0.0.1:8000/app.html (в браузере без Electron — графsly fallback, поля `—`).

## Что блокирует end-to-end прямо сейчас

`agent.log` каждые 2 с пишет `poll error: Expecting value: line 1 column 1 (char 0)` или `404`. Это значит — **n8n-вебхук `/webhook/agent-poll` не возвращает валидный JSON**. Пока это не починено, ни одна команда из Telegram до компьютера не доедет. Это **Шаг 1**.

## Карта файлов

```
E:\dispatch\
├─ main.py                    # точка входа Python-агента (трей)
├─ config.json                # реальный конфиг (railway_url, agent_token, polling, apps, autostart)
├─ agent.log                  # рабочий лог агента
├─ agent\
│  ├─ poller.py               # цикл poll → exec → result
│  ├─ handlers.py             # screenshot/terminal/launch-app/chrome-profile/install/system-info
│  └─ config.py               # чтение/запись config.json
├─ ui\settings_window.py      # окно настроек на customtkinter (старое)
├─ setup\auto_setup.py        # first-run мастер
├─ build\build.py             # PyInstaller сборка
├─ workflow_fixed.json        # workflow для импорта в n8n
├─ test_webhook.py            # тест вебхуков
├─ agent_os_app\              # Electron Control Center
│  ├─ main.js · preload.js
│  └─ renderer\app.html       # SPA с 11 разделами (текущий финальный вид)
├─ agent_os_tabs\app.html     # тот же файл, удобно править/смотреть в браузере
└─ MVP_PLAN.md                # этот документ
```

---

# Шаги до MVP

## Шаг 1 — починить n8n-вебхук `/webhook/agent-poll`

**Цель.** Эндпоинт всегда возвращает валидный JSON: пустой `{success:true,data:null,error:null}` если очередь пуста, либо `{success:true,data:{command_id,type,chat_id,...},error:null}` если есть команда.

**Готовность (acceptance):**
- `curl -H "X-Agent-Token: <agent_token>" https://n8n-production-419d8.up.railway.app/webhook/agent-poll` возвращает HTTP 200 + валидный JSON.
- В `agent.log` за 1 минуту больше нет строк `poll error: Expecting value` или `404`.
- Появляются `WARNING/INFO`-записи о нормальном опросе.

### Промт для новой сессии

```
Это проект Dispatch (E:\dispatch). Контекст лежит в E:\dispatch\MVP_PLAN.md — прочитай его целиком прежде чем что-то делать.

Сегодня цель — Шаг 1: починить n8n-вебхук /webhook/agent-poll, чтобы он отвечал валидным JSON.

Сейчас агент работает (E:\dispatch\agent.log пишет каждые 2 секунды), но эндпоинт на Railway отвечает либо 404, либо не-JSON, из-за чего poller.py падает в "Expecting value". Без этого шага вся система не работает end-to-end.

Что делать:
1. Прочитай E:\dispatch\workflow_fixed.json и E:\dispatch\agent\poller.py — пойми контракт ответа.
2. Открой n8n на Railway, импортируй (или обнови) workflow_fixed.json. Особое внимание узлам "Poll Webhook" (GET /webhook/agent-poll) и "Result Webhook" (POST /webhook/agent-result) — они должны ВСЕГДА отвечать JSON, даже когда очередь пуста.
3. Дёрни эндпоинт через curl или E:\dispatch\test_webhook.py, убедись что ответ — JSON.
4. Перезапусти агента (если нужно) и в течение минуты смотри agent.log — ошибок "Expecting value" быть не должно.

Готовность: curl возвращает JSON, в логе чисто. После этого обнови раздел "Что уже сделано" в MVP_PLAN.md.
```

---

## Шаг 2 — Сквозной тест: скриншот через Telegram

**Цель.** Юзер пишет в Telegram-бот → жмёт кнопку «📸 Снимок экрана» (или /screenshot) → через 2–3 секунды JPEG приходит обратно в чат.

**Готовность:**
- Реально приходит картинка в Telegram.
- В `agent.log` за один запрос видно последовательность: `poll.recv` → `cmd.exec` → `result.sent`.
- В UI Control Center на «Обзор → Что происходило недавно» появляется запись «Снимок экрана / обычное».

### Промт для новой сессии

```
Это проект Dispatch (E:\dispatch). Сначала прочитай E:\dispatch\MVP_PLAN.md — там вся карта и состояние.

Шаг 1 (n8n-вебхук) должен быть сделан. Если в agent.log всё ещё валятся ошибки poll — сначала вернись к Шагу 1.

Сегодня цель — Шаг 2: сквозной тест screenshot.

1. Убедись что python-агент запущен (или запусти: cd E:\dispatch && python main.py).
2. Открой Telegram-бот, отправь /start или нажми кнопку «Снимок экрана» из главного меню.
3. Ожидаемо: через ~3 секунды в чат прилетает JPEG.
4. Если не пришло — смотри E:\dispatch\agent.log и историю исполнений в n8n. Самые частые проблемы: пустая очередь, неправильный chat_id в Result Webhook, агент не подхватывает poll-ответ.
5. Проверь handler: E:\dispatch\agent\handlers.py функция screenshot — она должна делать mss capture, ресайз до 1280px, base64.

Готовность: реально пришёл скриншот + последовательность poll.recv → cmd.exec → result.sent в логе. Запиши в MVP_PLAN.md что Шаг 2 пройден.
```

---

## Шаг 3 — Трей-приложение (pystray)

**Цель.** `python main.py` запускает иконку в системном трее. Цвет — зелёный/жёлтый/красный по состоянию (свежесть poll). Меню: «Открыть Control Center», «Настройки», «Показать лог», «Выйти».

**Готовность:**
- После запуска `main.py` в трее иконка есть.
- Двойной клик / пункт «Открыть Control Center» поднимает Electron-окно (`agent_os_app`).
- Иконка цветом отражает реальный статус (как в Dashboard).
- При закрытии Control Center агент продолжает работать; «Выйти» из меню действительно убивает оба.

### Промт для новой сессии

```
Это проект Dispatch (E:\dispatch). Прочитай E:\dispatch\MVP_PLAN.md.

Сегодня цель — Шаг 3: трей-приложение.

Точка входа уже есть: E:\dispatch\main.py. Нужно проверить/доделать:
1. pystray иконка появляется в трее (Windows).
2. Цвет иконки — зелёный/жёлтый/красный — отражает реальное состояние. Логику бери ту же, что в agent_os_app/main.js → 'agent:status': свежо (<3×poll) → online, до 30× → stale, дальше → offline.
3. Меню: "Открыть Control Center" (запускает agent_os_app через subprocess: `npx electron .` или `electron.exe` из node_modules), "Настройки" (ui/settings_window.py — старое окно или редирект в Control Center), "Показать лог" (открыть agent.log в системном просмотрщике), "Выйти" (корректно остановить poller-поток).
4. При запуске НЕ должно быть консольного окна (CREATE_NO_WINDOW в Popen, pythonw.exe).

Файлы: E:\dispatch\main.py, E:\dispatch\agent\poller.py, E:\dispatch\ui\settings_window.py.

Готовность: иконка в трее, цвет меняется, меню работает, Control Center запускается из меню. Запиши в MVP_PLAN.md.
```

---

## Шаг 4 — Control Center управляет агентом (start / stop / isRunning)

**Цель.** В Control Center появляются кнопки «Запустить агент / Остановить агент». Статус «Компьютер: На связи» становится точным (не только по mtime лога, но и по реальному процессу).

**Готовность:**
- В `agent_os_app/main.js` добавлены IPC `agent:start`, `agent:stop`, `agent:isRunning`.
- Команда `agent:start` запускает `python main.py` (или собранный `.exe`) как дочерний процесс, без консольного окна.
- В Dashboard `app.html` появляется кнопка / тоггл «Агент: запущен/остановлен» с PID.
- `live`-loop тянет `agent:isRunning` и красит статус.

### Промт для новой сессии

```
Это проект Dispatch (E:\dispatch). Прочитай E:\dispatch\MVP_PLAN.md.

Сегодня цель — Шаг 4: Control Center управляет агентом.

Сейчас Control Center показывает реальные данные (CPU/RAM, события из agent.log, конфиг), но **не управляет** агентом — его надо запускать вручную через `python main.py`.

План:
1. В E:\dispatch\agent_os_app\main.js добавить 3 новых IPC:
   - `agent:start` — spawn `python` (или собранный .exe) с детачем; запомнить PID; обработать ошибки.
   - `agent:stop` — kill процесса по PID (graceful → SIGTERM аналог, потом kill).
   - `agent:isRunning` — true/false + PID + uptime.
   Использовать `child_process.spawn` с `detached:true, stdio:'ignore', windowsHide:true`.
2. Выставить методы в E:\dispatch\agent_os_app\preload.js: `startAgent()`, `stopAgent()`, `isAgentRunning()`.
3. В E:\dispatch\agent_os_tabs\app.html (и потом скопировать в renderer) добавить в Dashboard секцию управления: кнопка «Запустить / Остановить», статус с PID. В существующем `refreshAll()` дёргать `isAgentRunning()` каждые 2 с.
4. Скопировать app.html в agent_os_app/renderer/.

Готовность: из UI можно запустить и остановить агент; статус и PID видны; запустился — agent.log начинает писать.
```

---

## Шаг 5 — Автозапуск при включении ПК (реально работающий)

**Цель.** Тоггл «Запускать при включении ПК» в разделе «Пароли и ключи» не просто пишет в config.json, а реально регистрирует автостарт в Windows.

**Готовность:**
- Включён → в `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` появляется ключ `AgentOS` с путём к `AgentDispatch.exe` (или `pythonw.exe main.py`).
- Выключен → ключ удалён.
- После перезагрузки агент сам поднимается, иконка в трее.

### Промт для новой сессии

```
Это проект Dispatch (E:\dispatch). Прочитай E:\dispatch\MVP_PLAN.md.

Сегодня цель — Шаг 5: реальный автозапуск Windows.

Сейчас тоггл «Запускать при включении ПК» в Credentials tab меняет только config.json. Нужно — реально регистрировать автостарт.

План:
1. В E:\dispatch\agent_os_app\main.js добавить IPC `autostart:set` (bool, путь).
   На Windows: `child_process.execFile('reg', ['add', 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run', '/v', 'AgentOS', '/t', 'REG_SZ', '/d', `"${exePath}"`, '/f'])`
   На удаление: `reg delete ... /f`.
2. Путь брать так: сначала из config.json (поле `autostart_target`), иначе — путь к python.exe + main.py.
3. Также добавить IPC `autostart:get` — проверить наличие ключа.
4. В renderer/app.html в обработчике клика по `#liveAutostart` — после сохранения config вызывать `window.api.setAutostart(next)`, читать состояние из `getAutostart()` при refresh.

Готовность: тоггл реально включает/выключает автозапуск; проверяется reboot'ом.
```

---

## Шаг 6 — Все 7 хендлеров проверены end-to-end

**Цель.** Каждый из навыков агента отрабатывает из Telegram без ошибок: `screenshot`, `terminal`, `launch-app`, `chrome-profile`, `install`, `system-info`, `obsidian-context`.

**Готовность:**
- Для каждого: команда отдана → результат приходит в Telegram (фото/текст/файл).
- На вкладке «История событий» Control Center видны соответствующие `cmd.exec` / `result.sent`.
- Для крупных ответов (terminal >3000 символов) — приходит файл-вложение, не текст.
- Для chrome-profile проверены минимум 2 разных профиля.

### Промт для новой сессии

```
Это проект Dispatch (E:\dispatch). Прочитай E:\dispatch\MVP_PLAN.md.

Сегодня цель — Шаг 6: проверить все 7 хендлеров end-to-end из Telegram.

Список хендлеров — в E:\dispatch\agent\handlers.py: screenshot, terminal, launch-app, chrome-profile, install, system-info, obsidian-context.

Для каждого:
1. Из Telegram отдай команду (через кнопки главного меню или /команду).
2. Проверь что результат пришёл — фото/текст/файл, без ошибок.
3. Глянь в Control Center → «История событий» — должна появиться запись с правильным "обычное / внимание / ошибка".
4. Если что-то падает — открой E:\dispatch\agent\handlers.py и поправь.

Особые случаи проверить:
- terminal с ответом >3000 символов → должен прийти .txt-вложением.
- chrome-profile с двумя разными аккаунтами → разные окна Chrome.
- install с .exe — silent установка.
- screenshot — primary monitor.

Готовность: 7/7 хендлеров реально работают из Telegram. Записать в MVP_PLAN.md какие были фиксы.
```

---

## Шаг 7 — Сборка дистрибутивов

**Цель.** Один установщик для каждой части: `AgentDispatch-Setup.exe` (Python-агент, PyInstaller) и `Agent-OS-Control-Center-Setup.exe` (Electron-builder).

**Готовность:**
- `python E:\dispatch\build\build.py` собирает `dist\AgentDispatch.exe`.
- `cd E:\dispatch\agent_os_app && npm run build` собирает `dist\Agent-OS-Control-Center-Setup.exe`.
- На чистой Windows-машине после установки оба компонента стартуют, иконка в трее появляется, Control Center открывается, агент пишет в `%APPDATA%\AgentOS\agent.log`.

### Промт для новой сессии

```
Это проект Dispatch (E:\dispatch). Прочитай E:\dispatch\MVP_PLAN.md.

Сегодня цель — Шаг 7: сборка дистрибутивов (последний шаг до MVP).

Две сборки:
1. Python-агент → .exe через PyInstaller.
   Файл: E:\dispatch\build\build.py. Проверь и при необходимости обнови:
   - --noconsole (трей без консольного окна)
   - --add-data для иконки и шаблонов
   - --hidden-import для pystray, mss, customtkinter, requests, psutil, Pillow
   - --icon agent_os_app/renderer/icon.ico
   Результат: E:\dispatch\dist\AgentDispatch.exe (один файл).

2. Electron Control Center → installer через electron-builder.
   Файл: E:\dispatch\agent_os_app\package.json — уже есть блок "build" с target nsis.
   Команда: cd E:\dispatch\agent_os_app && npm run build.
   Результат: E:\dispatch\agent_os_app\dist\Agent-OS-Control-Center-Setup-0.1.0.exe.

3. ВАЖНО: исправь пути в Electron-приложении — сейчас main.js ищет config.json и agent.log в `path.resolve(__dirname, '..')`. В установленном приложении этого `..` нет. Нужно: если запущено в установленном виде (app.isPackaged), искать в `%APPDATA%\AgentOS\`. Создать папку при первом запуске.

4. Smoke test: установить оба .exe на чистый Windows VM (или хотя бы на другой профиль), проверить что:
   - Трей-иконка появилась.
   - Control Center открывается, в Dashboard живые данные.
   - Из Telegram отдаётся команда — результат приходит.

Готовность: оба .exe собраны, прошёл smoke test. После этого — MVP готов.
```

---

# После MVP (не для этого плана)

Когда MVP сдан, потенциальные направления:
- Auto-update (electron-updater + PyInstaller updater).
- Реальная очередь команд в Control Center (поход в n8n REST + отрисовка).
- Полная страница «Чаты» с реальными диалогами из n8n staticData.
- MCP-серверы — конфигурируемый список из UI.
- Подписанный сертификатом installer (code signing) — иначе SmartScreen.

---

# Глоссарий для будущих сессий

- **Агент** — Python-программа, которая крутится в трее и опрашивает n8n.
- **Сервер / n8n** — workflow на Railway, посредник между Telegram и агентом.
- **Control Center** — Electron-приложение для просмотра состояния и управления.
- **Хендлер / навык** — конкретное действие, которое умеет агент (screenshot и т.д.).
- **Poll-цикл** — `GET /webhook/agent-poll` каждые 2 с, агент сам идёт за заданиями.
- **AGENT_TOKEN** — общий секрет между агентом и n8n (заголовок `X-Agent-Token`).
- **chat_id** — Telegram-собеседник, кому отправить ответ.
