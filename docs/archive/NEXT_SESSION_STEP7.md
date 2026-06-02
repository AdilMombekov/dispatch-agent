# Следующая сессия · Шаг 7 MVP — сборка дистрибутивов (финал)

> **Полный кикофф-документ.** Последний шаг до MVP: собрать два установщика —
> Python-агент (`DispatchAgent.exe`, PyInstaller) и Electron Control Center
> (`Agent-OS Control Center Setup.exe`, electron-builder), и убедиться, что они
> работают на чистой машине.
>
> **Предусловие:** Шаги 1–6 закрыты (всё работает в dev-режиме).

---

## 0. TL;DR (30 секунд)

Две сборки + один критичный фикс путей. Конкретно:
1. **Агент:** `python build/build.py` → `dist/DispatchAgent.exe`. Скрипт почти готов,
   добавить недостающий hidden-import `requests`.
2. **Control Center:** `cd agent_os_app; npm run build` → NSIS-установщик.
   **Сломается из-за иконки:** `package.json` указывает `renderer/icon.ico`, а такого
   файла нет (есть только `assets/icon.ico`). Создать `renderer/icon.ico` или поправить путь.
3. **Критичный фикс:** `agent_os_app/main.js` ищет `config.json`/`agent.log`/
   `workflow_fixed.json` в `path.resolve(__dirname, '..')`. В установленном приложении
   этого `..` нет. Нужно: если `app.isPackaged` → читать из `%APPDATA%\AgentOS\`,
   создавать папку при первом запуске.

**Готово, когда:** оба `.exe` собраны и на (условно) чистой машине агент стартует,
Control Center открывается с живыми данными, команда из Telegram доходит.

---

## 1. Контекст — текущее состояние сборки (прочитано из кода)

### 1.1 `build/build.py` (агент, PyInstaller)

Уже есть (`build/build.py`):
- `--onefile --windowed` (один файл, без консоли) ✅
- `--name DispatchAgent` ✅
- `--icon assets/icon.ico` — **файл существует** ✅
- `--add-data agent;agent`, `--add-data ui;ui` ✅
- hidden-imports: `pystray._win32`, `PIL._tkinter_finder`, `customtkinter`, `mss`, `psutil` ✅
- **Не хватает:** `--hidden-import requests` (поллер его использует), при желании `mss.windows`.

### 1.2 `agent_os_app/package.json` (Control Center, electron-builder)

```json
"build": {
  "appId": "com.dispatch.agentos",
  "productName": "Agent-OS Control Center",
  "directories": { "output": "dist" },
  "files": ["main.js", "preload.js", "renderer/**"],
  "win": { "target": "nsis", "icon": "renderer/icon.ico" }
}
```
- **Проблема 1 — иконка:** `renderer/icon.ico` **отсутствует** (есть `assets/icon.ico`).
  electron-builder упадёт. Фикс: скопировать `assets/icon.ico` → `agent_os_app/renderer/icon.ico`
  (тогда и `files: renderer/**` его захватит), либо сменить путь на существующий.
- **Проблема 2 — данные не бандлятся:** `config.json`, `agent.log`, `workflow_fixed.json`
  лежат на уровень выше `agent_os_app/` и в `files` не входят. В установленном приложении
  их не будет рядом → см. фикс §1.3.

### 1.3 Критичный баг путей в `main.js` (подтверждён чтением кода)

`agent_os_app/main.js:10-12`:
```js
const CONFIG_PATH   = path.resolve(__dirname, '..', 'config.json');
const LOG_PATH      = path.resolve(__dirname, '..', 'agent.log');
const WORKFLOW_PATH = path.resolve(__dirname, '..', 'workflow_fixed.json');
```
В dev `__dirname/..` = `E:\dispatch` — всё на месте. В **установленном** приложении
`__dirname` указывает внутрь `resources/app.asar`, и `..` туда не ведёт к данным агента.
Все IPC (`config:read`, `log:tail`, `agent:status`, `events:recent`, `workflow:read`)
перестанут находить файлы.

**Фикс:** ввести базовую папку данных:
```js
const DATA_DIR = app.isPackaged
  ? path.join(process.env.APPDATA, 'AgentOS')   // %APPDATA%\AgentOS
  : path.resolve(__dirname, '..');              // dev — как сейчас
// создать DATA_DIR при старте, если нет
const CONFIG_PATH   = path.join(DATA_DIR, 'config.json');
const LOG_PATH      = path.join(DATA_DIR, 'agent.log');
const WORKFLOW_PATH = path.join(DATA_DIR, 'workflow_fixed.json');
```
И **агент** (`main.py`) в frozen-режиме уже пишет лог рядом с exe
(`main.py:17-18`). Чтобы агент и CC смотрели в одно место, договориться о
`%APPDATA%\AgentOS\` как едином каталоге данных в проде (см. §3.4).

---

## 2. Промт для новой сессии (copy-paste)

```
Я работаю над проектом Dispatch (Windows-агент → Railway n8n → Telegram).
Корень — E:\dispatch.

Прочитай сперва:
1. E:\dispatch\MVP_PLAN.md
2. E:\dispatch\NEXT_SESSION_STEP7.md — ТЗ на сегодня (финальная сборка).

Цель — Шаг 7: собрать оба дистрибутива и проверить smoke-тестом.

Сделать:
1. build/build.py: добавить --hidden-import requests. Собрать: python build/build.py
   → E:\dispatch\dist\DispatchAgent.exe.
2. agent_os_app: починить иконку (создать renderer/icon.ico из assets/icon.ico
   или поправить путь в package.json). Собрать: npm run build.
3. КРИТИЧНО: в agent_os_app/main.js завести DATA_DIR — если app.isPackaged, брать
   %APPDATA%\AgentOS\ для config.json/agent.log/workflow_fixed.json; создавать папку
   при первом запуске. В dev оставить path.resolve(__dirname,'..').
4. Договориться, что в проде агент и CC используют %APPDATA%\AgentOS\ как общий
   каталог данных (агент пишет лог туда же).
5. Smoke-тест: установить оба exe, проверить трей + Control Center + команда из Telegram.

ОГРАНИЧЕНИЯ: не ломать dev-режим (path.resolve(__dirname,'..') должен работать,
когда не isPackaged). Логику поллера/хендлеров не трогать.

Закрой по §6. Обнови MVP_PLAN.md (Шаг 7 ✅ — MVP готов).
```

---

## 3. ТЗ (требования)

### 3.1 Сборка агента (PyInstaller)
- В `build/build.py` добавить `--hidden-import requests`.
- Результат: `E:\dispatch\dist\DispatchAgent.exe` (один файл, без консоли).
- Запуск exe → иконка в трее, `agent.log` пишется (в проде — рядом с exe или в `%APPDATA%\AgentOS\`, см. §3.4).

### 3.2 Сборка Control Center (electron-builder)
- Починить иконку: создать `agent_os_app/renderer/icon.ico` (копия `assets/icon.ico`),
  чтобы `files: renderer/**` её включил, **или** изменить `win.icon` на доступный путь.
- `cd agent_os_app; npm install` (если ещё не) → `npm run build`.
- Результат: `agent_os_app/dist/Agent-OS Control Center Setup 0.1.0.exe`.

### 3.3 Фикс путей в main.js (см. §1.3)
- Ввести `DATA_DIR`, ветвление по `app.isPackaged`.
- Создавать `DATA_DIR` при старте, если нет (`fssync.mkdirSync(DATA_DIR, {recursive:true})`).
- Все три пути (`CONFIG_PATH`, `LOG_PATH`, `WORKFLOW_PATH`) — через `DATA_DIR`.
- **Dev не сломать:** при `!app.isPackaged` поведение прежнее (`__dirname/..`).

### 3.4 Единый каталог данных в проде
- Целевой каталог: `%APPDATA%\AgentOS\` (`C:\Users\<user>\AppData\Roaming\AgentOS`).
- Туда кладутся: `config.json`, `agent.log`, `workflow_fixed.json`.
- При первом запуске агента/CC — создать каталог и при отсутствии `config.json`
  засеять дефолт (`ensure_config` уже умеет дефолты, `config.py:112`; но `CONFIG_PATH`
  в `config.py:7` тоже завязан на расположение пакета — проверить, что в frozen-режиме
  он указывает в `%APPDATA%\AgentOS\`, иначе агент и CC разойдутся по разным config).

> ⚠️ Это место — главный источник рассинхрона: у агента свой `CONFIG_PATH`
> (`agent/config.py:7` = `__file__/../../config.json`), у CC свой (`main.js`).
> В проде оба должны указывать на `%APPDATA%\AgentOS\config.json`. Свести их.

### 3.5 Связка автозапуска (из Шага 5)
- Если Шаг 5 сделан, `autostart_target` в `config.json` должен указывать на
  установленный `DispatchAgent.exe`, а не на dev-pythonw.

### 3.6 Что НЕ трогать
- ✋ Логику поллера/хендлеров.
- ✋ Dev-режим запуска (`npm start`, `python main.py`) должен продолжать работать.

---

## 4. План работы

### Фаза A · Агент (20 мин)
1. Добавить `--hidden-import requests` в `build/build.py`.
2. `python build/build.py`.
3. Запустить `dist/DispatchAgent.exe` → иконка в трее, лог пишется.

### Фаза B · Фикс путей main.js (30 мин)
1. Ввести `DATA_DIR` + ветвление `app.isPackaged` + `mkdir`.
2. Свести `CONFIG_PATH` агента (`config.py`) и CC к `%APPDATA%\AgentOS\` в проде.
3. Проверить, что dev-режим (`npm start`) всё ещё видит файлы в `E:\dispatch`.

### Фаза C · Control Center (20 мин)
1. Создать `renderer/icon.ico` (копия `assets/icon.ico`).
2. `cd agent_os_app; npm install` (при необходимости) → `npm run build`.
3. Получить NSIS-установщик в `agent_os_app/dist/`.

### Фаза D · Smoke-тест (30 мин)
1. Установить оба `.exe` (на другой профиль/чистую VM, если есть).
2. Проверить:
   - Трей-иконка появилась.
   - Control Center открывается, в Dashboard живые данные (CPU/RAM, статус).
   - Из Telegram отдана команда → результат пришёл.
   - Логи и конфиг — в `%APPDATA%\AgentOS\`.

### Фаза E · Зачистка (10 мин)
- Обновить MVP_PLAN.md: Шаг 7 ✅, **MVP готов**. Отметить известные ограничения
  (нет code-signing → SmartScreen предупредит).

---

## 5. TODO (checklist)

### Агент
- [ ] `--hidden-import requests` добавлен в build.py
- [ ] `python build/build.py` → `dist/DispatchAgent.exe`
- [ ] exe запускается: трей + лог

### main.js пути
- [ ] `DATA_DIR` с ветвлением `app.isPackaged`
- [ ] `DATA_DIR` создаётся при старте
- [ ] `CONFIG_PATH/LOG_PATH/WORKFLOW_PATH` через `DATA_DIR`
- [ ] dev-режим не сломан
- [ ] агент и CC в проде смотрят в один `%APPDATA%\AgentOS\config.json`

### Control Center
- [ ] `renderer/icon.ico` создан (или путь исправлен)
- [ ] `npm run build` → NSIS-установщик в `dist/`

### Smoke-тест
- [ ] Установлены оба exe
- [ ] Трей-иконка есть
- [ ] Control Center: живые данные
- [ ] Команда из Telegram доходит
- [ ] config/log в `%APPDATA%\AgentOS\`

### Зачистка
- [ ] MVP_PLAN.md: Шаг 7 ✅, MVP готов, ограничения отмечены

---

## 6. Готовность (приёмка)

Закрыто (= MVP готов), когда **все** проходят:
1. `python build/build.py` собирает `dist/DispatchAgent.exe`, exe стартует (трей + лог).
2. `npm run build` собирает `Agent-OS Control Center Setup ...exe` без ошибки иконки.
3. После установки на (условно) чистой машине: трей-иконка есть, Control Center
   открывается с живыми данными, агент пишет в `%APPDATA%\AgentOS\agent.log`.
4. Из Telegram отдаётся команда → результат приходит (сквозной цикл жив в проде).

---

## 7. Файлы и команды

| Файл                                  | Зачем                                  |
|---------------------------------------|----------------------------------------|
| `E:\dispatch\build\build.py`          | сборка агента (добавить requests)      |
| `E:\dispatch\assets\icon.ico`         | исходная иконка (есть)                 |
| `E:\dispatch\agent_os_app\renderer\icon.ico` | **создать** (нет)               |
| `E:\dispatch\agent_os_app\package.json` | блок `build` (target nsis)           |
| `E:\dispatch\agent_os_app\main.js`    | фикс путей `DATA_DIR` (стр. 10-12)     |
| `E:\dispatch\agent\config.py`         | `CONFIG_PATH` агента (стр. 7) — свести с CC |
| `E:\dispatch\main.py`                 | frozen-логика лога (стр. 15-18)        |

```powershell
# Сборка агента
cd E:\dispatch; python build/build.py

# Создать иконку CC из существующей
Copy-Item E:\dispatch\assets\icon.ico E:\dispatch\agent_os_app\renderer\icon.ico

# Сборка Control Center
cd E:\dispatch\agent_os_app; npm install; npm run build

# Куда смотреть данные в проде
explorer "$env:APPDATA\AgentOS"
```

---

## 8. Типичные грабли

| Симптом                                          | Причина                                      | Что делать                                              |
|--------------------------------------------------|----------------------------------------------|---------------------------------------------------------|
| electron-builder: `cannot find icon`             | `renderer/icon.ico` отсутствует              | создать его или поправить путь в package.json           |
| Установленный CC: всё «—», нет данных            | пути `__dirname/..` не ведут к данным         | фикс `DATA_DIR` (§1.3), `%APPDATA%\AgentOS\`            |
| Агент и CC показывают разный конфиг              | разные `CONFIG_PATH`                          | свести оба на `%APPDATA%\AgentOS\config.json`           |
| exe падает: `ModuleNotFoundError: requests`      | нет hidden-import                             | `--hidden-import requests`                              |
| exe падает на pystray/mss                        | нет hidden-import                             | уже есть `pystray._win32`, `mss`; добавить при нужде     |
| SmartScreen блокирует установку                  | exe не подписан                               | известное ограничение MVP; code-signing — после MVP     |
| `npm run build` не находит electron-builder      | `npm install` не сделан                       | `npm install` в `agent_os_app/`                          |
| Лог не пишется в проде                           | frozen-путь лога ≠ DATA_DIR                   | согласовать `main.py:17-18` с `%APPDATA%\AgentOS\`      |

---

## 9. После Шага 7 — MVP готов

Дальнейшие направления (вне плана MVP, см. [MVP_PLAN.md → «После MVP»](MVP_PLAN.md)):
- Auto-update (electron-updater + PyInstaller updater).
- Реальная очередь команд в Control Center (n8n REST).
- Полная страница «Чаты» из n8n staticData.
- MCP-серверы — конфигурируемый список из UI.
- Code-signing установщиков (убрать предупреждение SmartScreen).

---

## 10. Глоссарий

- **PyInstaller** — упаковщик Python-кода в `.exe`.
- **--onefile / --windowed** — один файл / без консольного окна.
- **hidden-import** — модуль, который PyInstaller не находит сам, надо указать явно.
- **electron-builder** — сборщик установщиков Electron-приложений.
- **NSIS** — формат Windows-инсталлятора (Nullsoft).
- **app.isPackaged** — флаг Electron: запущено из установленного приложения или из dev.
- **%APPDATA%\AgentOS\** — целевой каталог данных в проде (config, log, workflow).
- **code-signing** — подпись exe сертификатом; без неё SmartScreen предупреждает.
