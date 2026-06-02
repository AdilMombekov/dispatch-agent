# Следующая сессия · Шаг 6 MVP — все хендлеры end-to-end

> **Полный кикофф-документ.** Цель — прогнать каждый навык агента из Telegram и
> убедиться, что результат реально приходит назад. Контракты payload'ов — точные,
> из [agent/handlers.py](agent/handlers.py).
>
> **Предусловие:** Шаги 1–2 закрыты (poll + result работают, screenshot уже проверен).

---

## 0. TL;DR (30 секунд)

`dispatch()` (`handlers.py:356`) роутит по `command["type"]`. В MVP_PLAN заявлено
7 навыков, **по факту в коде их 11**. Для каждого: отдать команду из Telegram →
проверить, что результат пришёл (фото/текст/файл) → глянуть событие в Control Center.
Скриншот уже проверен на Шаге 2 — начинать можно с него как с эталона.

**Главное, что многие пропускают:** у каждого типа **свой ключ в `payload`**
(см. §3). И у `terminal` при выводе >3000 символов результат уходит **файлом**
(`send_as_file:true`), а не текстом — n8n должен это уметь.

**Готово, когда:** все нужные навыки отрабатывают из Telegram без ошибок,
а в Control Center → «История событий» видны записи с правильной severity.

---

## 1. Контекст

### 1.1 Полный список типов команд (из `dispatch`, `handlers.py:356-384`)

| `type`                  | payload (ключи)                          | Что возвращает (`result.data`)                          |
|-------------------------|------------------------------------------|----------------------------------------------------------|
| `screenshot`            | — (игнор)                                | `{image: b64jpeg, width, height}`                        |
| `terminal`              | `cmd` (или legacy `command`)             | `{output, returncode, send_as_file}` ИЛИ файл-вариант    |
| `launch-app`            | `app` (ключ из `config.apps`)            | строка `"Launched <app>"`                                |
| `chrome-profile`        | `profile_directory`                      | строка `"Launched Chrome with profile: ..."`             |
| `chrome-profiles-list`  | — (игнор)                                | список `[{directory, display, email}]`                   |
| `install`               | `url` (.exe/.msi/.zip)                    | строка статуса установки                                 |
| `system-info`           | — (игнор)                                | `{cpu_percent, ram_*, disk_*}`                           |
| `obsidian-context`      | `note`/`notes`, `lines`, `query`         | `{notes: {name: tail}}`                                  |
| `obsidian-log`          | `text`, `note?`                          | строка подтверждения                                     |
| `obsidian-read`         | `note`, `lines`                          | `{note, content}`                                        |
| `send-file`             | `filepath`                               | `{filename, mime_type, file_base64}` (лимит 50 МБ)       |

> MVP_PLAN перечисляет 7 «основных»: screenshot, terminal, launch-app, chrome-profile,
> install, system-info, obsidian-context. Остальные 4 (chrome-profiles-list,
> obsidian-log, obsidian-read, send-file) — бонусные, проверить если есть кнопки/команды.

### 1.2 Общая форма результата

Любой хендлер возвращает `{"success": bool, "data": ..., "error": str|None}`
(`_ok`/`_err`, `handlers.py:16-21`). Поллер заворачивает это в payload result
(`poller.py:140`) и шлёт на `/webhook/agent-result`.

### 1.3 Где смотреть событие

Control Center → «История событий» / «Обзор». События парсятся из `agent.log`
регуляркой (`agent_os_app/main.js:187`) и классифицируются (`classifyEvent`,
`main.js:189`) с severity `info/warn/err`.

---

## 2. Промт для новой сессии (copy-paste)

```
Я работаю над проектом Dispatch (Windows-агент → Railway n8n → Telegram).
Корень — E:\dispatch.

Прочитай сперва:
1. E:\dispatch\MVP_PLAN.md
2. E:\dispatch\NEXT_SESSION_STEP6.md — ТЗ на сегодня (точные payload'ы навыков).

Цель — Шаг 6: проверить все навыки агента end-to-end из Telegram.

Список типов и их payload'ы — в §3 NEXT_SESSION_STEP6.md (источник — agent/handlers.py).
Для каждого навыка:
1. Отдать команду из Telegram (кнопка меню или /команда).
2. Проверить, что результат пришёл — фото/текст/файл, без ошибок.
3. Глянуть Control Center → «История событий»: запись с правильной severity.
4. Если падает — открыть agent/handlers.py, поправить хендлер.

Особые случаи:
- terminal с выводом >3000 символов → приходит .txt-вложением (send_as_file:true).
- chrome-profile с двумя разными профилями → два разных окна Chrome.
- install с .exe → тихая установка.
- send-file → файл до 50 МБ.

ОГРАНИЧЕНИЯ: фиксы — только в handlers.py / n8n. UI и поллер не трогать без нужды.

Закрой по §6. Запиши в MVP_PLAN.md, какие были фиксы (Шаг 6 ✅).
```

---

## 3. ТЗ — точные payload'ы по каждому навыку

> Команда в очереди имеет вид `{command_id, chat_id, type, payload}`.
> `dispatch` читает `command["payload"]` (`handlers.py:358`). Ниже — содержимое `payload`.

### 3.1 `screenshot` (эталон, проверен на Шаге 2)
- payload: `{}`
- result.data: `{image: "<b64 jpeg>", width, height}` → n8n шлёт `sendPhoto` из `result.data.image`.

### 3.2 `terminal`
- payload: `{"cmd": "echo hello"}` (или legacy `{"command": ...}`)
- Таймаут 300 с, `CREATE_NO_WINDOW`, encoding utf-8/replace.
- **Вывод ≤3000 символов:** `result.data = {output, returncode, send_as_file:false}` → n8n шлёт текст.
- **Вывод >3000 символов:** `result.data = {output:<preview>, output_file_b64:<b64>,
  filename:"terminal_output.txt", returncode, send_as_file:true}` → **n8n должен
  отправить файл** (`sendDocument`), не текст. Тест: `dir /s` или `pip list`.
- Таймаут → `_err("TIMEOUT: ...")`.

### 3.3 `launch-app`
- payload: `{"app": "cursor"}` — ключ должен существовать в `config.json:apps`.
- Если путь пустой/не настроен → `_err("app '<name>' not configured or not found")`.
- Проверить, что приложение реально открылось на ПК. Доступные ключи по умолчанию:
  `cursor, vscode, claude-code, antigravity, terminal, chrome` (`config.py:9`).

### 3.4 `chrome-profile`
- payload: `{"profile_directory": "Default"}` или `"Profile 1"` и т.д.
- Берёт `config.apps.chrome`. Если пусто → `_err("Chrome not found in config")`.
- **Тест на 2 профиля:** отдать дважды с разными `profile_directory` → два разных окна
  Chrome с разными аккаунтами. Список профилей — через `chrome-profiles-list`.

### 3.5 `chrome-profiles-list`
- payload: `{}`
- Возвращает `[{directory, display, email}]`, читая `Preferences` каждого профиля
  (`config.py:56`). Полезно перед `chrome-profile`, чтобы узнать имена.

### 3.6 `install`
- payload: `{"url": "https://.../app.exe"}` — поддержка `.exe/.msi/.zip`.
- `.exe` → silent (`/S /silent /quiet`); `.msi` → `msiexec /quiet /norestart`;
  `.zip` → распаковка. Прочее → `_err("unsupported installer type")`.
- Тест: маленький тихий установщик. Проверить, что установилось без UI.

### 3.7 `system-info`
- payload: `{}`
- result.data: `{cpu_percent, ram_used_gb, ram_total_gb, ram_percent,
  disk_used_gb, disk_total_gb, disk_percent}` → n8n шлёт текстом.

### 3.8 `obsidian-context` (прямое чтение vault с диска)
- payload: `{"note": "Daily"}` или `{"notes": ["A","B"], "lines": 50, "query": "todo"}`
- Путь vault — `config.obsidian.vault_path` (дефолт `E:/Obsidian`, `handlers.py:205`).
- `.md` дописывается автоматически. `query` — регистронезависимый grep, `lines` — хвост.
- Нет файла → `"Файл не найден."` в значении.

### 3.9 `obsidian-log` (запись через Local REST API)
- payload: `{"text": "сделал X", "note?": "claude97"}`
- PATCH (append к заголовку), при 404 — PUT (создать заметку). Нужен плагин
  Obsidian Local REST API: `config.obsidian.host/token/vault/note`.

### 3.10 `obsidian-read` (чтение через REST API)
- payload: `{"note": "claude97", "lines": 20}`
- Возвращает `{note, content}` (хвост N строк).

### 3.11 `send-file`
- payload: `{"filepath": "E:\\path\\file.pdf"}`
- result.data: `{filename, mime_type, file_base64}`. Лимит **50 МБ**
  (`handlers.py:337`). Нет файла → `_err`. → n8n `sendDocument`.

### 3.12 Что НЕ трогать
- ✋ UI Control Center.
- ✋ `poller.py` (кроме опц. логов из Шага 2).
- Фиксы навыков — в `handlers.py`; фиксы доставки — в n8n.

---

## 4. План работы

### Фаза A · Подготовка (10 мин)
1. Прочитать `handlers.py` и этот файл.
2. Запустить агента (`python main.py`), открыть хвост `agent.log`.
3. Открыть Control Center → «История событий».

### Фаза B · Прогон по навыкам (основное время)
Идти по таблице §1.1. Для каждого:
1. Отдать команду из Telegram.
2. Дождаться результата в чате (фото/текст/файл).
3. Проверить событие в Control Center.
4. Падает → читать `handlers.py`, чинить, повторить.

Порядок (от простого к сложному):
`system-info` → `screenshot` → `terminal` (малый вывод) → `terminal` (>3000 → файл)
→ `launch-app` → `chrome-profiles-list` → `chrome-profile` (×2 профиля)
→ `send-file` → `obsidian-*` → `install`.

### Фаза C · Особые случаи
- terminal >3000 символов → пришёл .txt, а не обрезанный текст.
- chrome-profile ×2 → два окна.
- install → тихо, без UI.

### Фаза D · Зачистка (5 мин)
- Записать в MVP_PLAN.md: что прошло, какие были фиксы (Шаг 6 ✅).

---

## 5. TODO (checklist по навыкам)

- [ ] `system-info` — текст с CPU/RAM/диском
- [ ] `screenshot` — фото
- [ ] `terminal` (малый вывод) — текст
- [ ] `terminal` (>3000 символов) — **.txt-вложение**
- [ ] `launch-app` — приложение открылось
- [ ] `chrome-profiles-list` — список профилей
- [ ] `chrome-profile` — **2 разных профиля → 2 окна**
- [ ] `send-file` — файл до 50 МБ пришёл
- [ ] `obsidian-context` — содержимое заметки
- [ ] `obsidian-log` — строка дописалась в заметку
- [ ] `obsidian-read` — хвост заметки пришёл
- [ ] `install` — тихая установка .exe/.msi
- [ ] Все события видны в Control Center с верной severity
- [ ] MVP_PLAN.md обновлён (Шаг 6 ✅, список фиксов)

---

## 6. Готовность (приёмка)

Закрыто, когда:
1. Каждый используемый навык отдан из Telegram → результат реально пришёл
   (фото/текст/файл), без ошибок.
2. terminal >3000 символов приходит **файлом**, не обрезанным текстом.
3. chrome-profile проверен минимум на **2 разных профилях** (два окна).
4. В Control Center → «История событий» видны записи `cmd.*` / события с правильной
   severity (обычное/внимание/ошибка).
5. В MVP_PLAN.md записано, какие навыки прошли и какие фиксы потребовались.

---

## 7. Файлы и команды

| Файл                          | Зачем                                 |
|-------------------------------|---------------------------------------|
| `E:\dispatch\agent\handlers.py` | все хендлеры (фиксы тут)             |
| `E:\dispatch\agent\config.py` | `apps`, chrome-профили                |
| `E:\dispatch\config.json`     | `apps`, `obsidian` настройки          |
| `E:\dispatch\agent.log`       | смотреть cmd.exec / ошибки            |
| `E:\dispatch\agent_os_app\`   | Control Center → История событий      |

```powershell
cd E:\dispatch; python main.py
Get-Content E:\dispatch\agent.log -Wait -Tail 30
# Список chrome-профилей вручную:
Get-ChildItem "$env:LOCALAPPDATA\Google\Chrome\User Data" -Directory | Select-Object Name
```

---

## 8. Типичные грабли

| Симптом                                       | Причина                                  | Что делать                                          |
|-----------------------------------------------|------------------------------------------|-----------------------------------------------------|
| `unknown command type`                         | `type` не из списка §1.1                 | проверить, что n8n шлёт правильный `type`            |
| `app '...' not configured`                     | пустой путь в `config.apps`              | заполнить путь в `config.json` или Control Center    |
| terminal обрезается, файл не приходит          | n8n игнорит `send_as_file`/`output_file_b64` | в n8n: если `send_as_file` → sendDocument из b64 |
| chrome-profile открывает один и тот же профиль | неверный `profile_directory`             | взять имена из `chrome-profiles-list`                |
| obsidian-* падает с ошибкой соединения         | плагин Local REST API выключен / порт    | проверить `config.obsidian.host/token`               |
| send-file: file too large                      | >50 МБ                                    | лимит в `handlers.py:337` (поднимать осторожно)      |
| install ничего не ставит                       | не silent-флаги у конкретного инсталлера  | у разных .exe свои флаги; проверить вручную          |
| Событие не появилось в Control Center           | строка лога не подошла под регулярку      | формат лога — `main.py:21`; классификация `main.js:189` |

---

## 9. После Шага 6 — что дальше

- **Шаг 7** — сборка дистрибутивов (PyInstaller + electron-builder), последний шаг
  до MVP. Кикофф: [NEXT_SESSION_STEP7.md](NEXT_SESSION_STEP7.md).

---

## 10. Глоссарий

- **Хендлер / навык** — функция в `handlers.py`, исполняющая один тип команды.
- **dispatch** — роутер: `command["type"]` → нужный хендлер.
- **payload** — параметры команды (`command["payload"]`), у каждого типа свои ключи.
- **send_as_file** — флаг: результат terminal слишком большой, слать файлом.
- **vault** — папка Obsidian с заметками (`.md`).
- **Local REST API** — плагин Obsidian, открывающий HTTP-доступ к заметкам.
- **severity** — уровень события в Control Center: обычное / внимание / ошибка.
