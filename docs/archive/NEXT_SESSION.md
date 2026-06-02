# Следующая сессия Claude Pro · Шаг 1 MVP

> Это **полный кикофф-документ** для свежей сессии. Цель — починить n8n-вебхук
> `/webhook/agent-poll`, чтобы агент перестал падать и сквозной poll → exec → result
> впервые заработал. Это блокирующий шаг для всего остального — без него Telegram-
> команды не доходят до компьютера.

---

## 0. TL;DR (если у тебя 30 секунд)

`poller.py` каждые 2 с шлёт `GET https://n8n-production-419d8.up.railway.app/webhook/agent-poll`
с заголовком `X-Agent-Token`. Сервер либо отвечает 404, либо возвращает не-JSON.
Нужно: импортировать `workflow_fixed.json` в n8n на Railway, активировать его,
убедиться что эндпоинт возвращает **валидный JSON в любом случае** (даже когда очередь пуста).

**Готово, когда:** `agent.log` за минуту работы агента **не содержит ни одной строки `poll error`**.

---

## 1. Контекст (что было)

### 1.1 Что за проект

Dispatch — это «пульт управления Windows-компьютером с телефона». Архитектура:

```
Telegram (телефон) ⇄ Railway n8n (сервер) ⇄ Desktop Agent (Python, polling)
```

Агент сидит на ПК без открытых портов, **сам** опрашивает n8n каждые 2 с
(«есть для меня задание?»), выполняет команду локально (скриншот, терминал,
запуск программы), и шлёт результат обратно — n8n отправляет его в Telegram.

### 1.2 Что уже сделано в прошлых сессиях

См. [MVP_PLAN.md → «Что уже сделано»](MVP_PLAN.md) — там полный снимок состояния.
Коротко:

- **Python-агент** (`agent/poller.py`, `handlers.py`, `config.py`, `main.py`) —
  есть и крутится. Поллер корректно глотает пустые/не-JSON тела (фикс был отдельно).
- **n8n workflow** — лежит как файл `workflow_fixed.json` (34 узла), но
  **на Railway не активирован** или не той версии.
- **Electron Control Center** (`agent_os_app/`) — рабочий, 12 вкладок, нативный
  macOS-вид (Liquid Glass), реальные данные через IPC, **отдельная вкладка
  «Воркфлоу n8n»** с кликабельными узлами и кодом.
- **MVP_PLAN.md** — пошаговый план до MVP (Шаг 1–7) с промтами под каждый шаг.

### 1.3 Что сломано прямо сейчас

Лог `E:\dispatch\agent.log` за последний месяц забит одной из двух ошибок:

```
2026-04-26 19:42:33,116 [ERROR] poller: poll error: Expecting value: line 1 column 1 (char 0)
2026-04-26 19:50:41,812 [ERROR] poller: poll error: 404 Client Error: ...
```

Это значит:
- **404** — эндпоинт `/webhook/agent-poll` на Railway вообще не существует
  (workflow не активирован, или активирован не тот, или путь не совпадает).
- **Expecting value** — эндпоинт отвечает 200, но тело пустое или это HTML/текст,
  а не JSON.

**Без починки этого — ничего не работает end-to-end.**

---

## 2. Промт для новой сессии (copy-paste в чистый `claude`)

```
Я работаю над проектом Dispatch (Windows-агент → Railway n8n → Telegram).
Корень — E:\dispatch.

Перед чем-либо ещё прочитай два документа:
1. E:\dispatch\MVP_PLAN.md — общее состояние и шаги до MVP.
2. E:\dispatch\NEXT_SESSION.md — детальное ТЗ на сегодняшнюю сессию (этот шаг).

Цель сегодня — Шаг 1 MVP: починить n8n-вебхук /webhook/agent-poll, чтобы агент
перестал валить poll error и впервые заработал сквозной цикл poll → exec → result.

ВАЖНЫЕ ОГРАНИЧЕНИЯ:
- НЕ трогать UI (agent_os_app/, agent_os_tabs/), CSS, Electron-обвязку, design.
- НЕ менять контракт ответа в poller.py — он уже знает оба формата (см. NEXT_SESSION §3.3).
- НЕ менять AGENT_TOKEN в config.json — он используется в воркфлоу.
- Все изменения в воркфлоу делать в workflow_fixed.json (источник истины) и
  затем импортировать в Railway-n8n, а не наоборот.

Стартуй с §4 «План работы» и §5 «TODO» из NEXT_SESSION.md.
Закрой задачу когда выполнены все критерии из §7 «Готовность».
По завершении — обнови раздел «Что уже сделано» в MVP_PLAN.md.
```

---

## 3. ТЗ (требования)

### 3.1 Эндпоинты, которые должны работать

| Метод | Путь                     | Назначение                                | Заголовок auth     |
|-------|--------------------------|-------------------------------------------|--------------------|
| GET   | `/webhook/agent-poll`    | агент забирает следующую команду          | `X-Agent-Token`    |
| POST  | `/webhook/agent-result`  | агент шлёт результат назад                | `X-Agent-Token`    |
| POST  | `/webhook/telegram`      | Telegram Trigger получает обновления      | (n8n сам валидирует) |

Базовый URL: `https://n8n-production-419d8.up.railway.app`
(берётся из `config.json` → `railway_url`).

### 3.2 Контракт `GET /webhook/agent-poll`

**Запрос:**

```
GET /webhook/agent-poll HTTP/1.1
Host: n8n-production-419d8.up.railway.app
X-Agent-Token: 3bafc0aa-bb42-4e01-b918-8c5a8a252a43
```

**Ответ — очередь пуста (норма):**

```
HTTP/1.1 200 OK
Content-Type: application/json
```
```json
{ "success": true, "data": null, "error": null }
```

Альтернативная форма (тоже принимается поллером):

```json
{ "success": true, "has_command": false }
```

**Ответ — есть команда:**

```json
{
  "success": true,
  "data": {
    "command_id": "c_8f2a",
    "chat_id":   100423001,
    "type":      "screenshot",
    "params":    {}
  },
  "error": null
}
```

Альтернативная форма:

```json
{
  "success": true,
  "has_command": true,
  "command": {
    "command_id": "c_8f2a",
    "chat_id":   100423001,
    "type":      "screenshot"
  }
}
```

**Ответ — неверный токен:**

```
HTTP/1.1 401 Unauthorized
{ "success": false, "data": null, "error": "invalid token" }
```

**Чего НЕ должно быть НИКОГДА:**
- ❌ HTTP 404 (значит вебхук не активен)
- ❌ Пустое тело при HTTP 200 (poller это переварит, но n8n не должен так отвечать)
- ❌ HTML вместо JSON (значит дошли до n8n welcome-страницы, а не до webhook'a)
- ❌ `{"success": true}` без `data`/`has_command` — двусмысленно

### 3.3 Контракт `POST /webhook/agent-result`

**Запрос (как шлёт `poller.py._post_result`):**

```
POST /webhook/agent-result HTTP/1.1
Content-Type: application/json
X-Agent-Token: 3bafc0aa-bb42-4e01-b918-8c5a8a252a43
```
```json
{
  "command_id":   "c_8f2a",
  "chat_id":      100423001,
  "command_type": "screenshot",
  "result": {
    "success": true,
    "data":    "<base64 jpeg>",
    "error":   null
  }
}
```

Для типа `send-file` (см. `test_webhook.py`) — `result.data` это объект:
```json
{
  "filename":    "git_status.txt",
  "mime_type":   "text/plain",
  "file_base64": "<...>"
}
```

**Ожидаемый ответ:**

```
HTTP/1.1 200 OK
{ "success": true }
```

n8n должен после этого **доставить результат в Telegram в нужный `chat_id`**.

### 3.4 Аутентификация

Один общий секрет, в двух местах:
- В `config.json` (поле `agent_token`) — у агента.
- В n8n — в условиях узлов `Poll Webhook` и `Result Webhook`
  (сейчас в воркфлоу токен **захардкожен** в JS-коде узлов, см. известный
  компромисс в [MVP_PLAN.md → Credentials](MVP_PLAN.md)).

Текущий токен (на момент сессии):
```
3bafc0aa-bb42-4e01-b918-8c5a8a252a43
```

Если решишь ротировать — синхронизируй в **обоих** местах.

### 3.5 Поведение `poller.py` (уже работает корректно — не трогать)

```python
# agent/poller.py:81-116
def _poll(self, url, token):
    resp = requests.get(f"{url}/webhook/agent-poll", headers={"X-Agent-Token": token}, timeout=10)
    resp.raise_for_status()             # 4xx/5xx → except → ConnectionError/Exception
    if not resp.text.strip(): return {} # пустое тело → нет команды (не ошибка)
    data = resp.json()                  # не-JSON → ValueError → return {} (не ошибка)
    if not data.get("success"): return {}
    if "has_command" in data:
        return data.get("command") or {} if data.get("has_command") else {}
    return data.get("data") or {}       # current n8n format
```

Поллер уже толерантен. Источник ошибок в логе — **сам n8n** возвращает 404 или
не-JSON.

### 3.6 Что НЕ трогать

- ✋ `agent_os_app/`, `agent_os_tabs/` — Electron Control Center и его рендерер. Дизайн закончен.
- ✋ `agent/poller.py`, `agent/handlers.py`, `agent/config.py` — Python-агент. Работает.
- ✋ `config.json` поля `apps`, `obsidian`, `poll_interval_*`, `qdrant_*` — настройки пользователя.
- ✋ `AGENT_TOKEN` — менять только если **точно** синхронизируешь оба места.

Менять можно:
- ✅ `workflow_fixed.json` (источник истины для воркфлоу).
- ✅ n8n-инстанс на Railway (импортировать `workflow_fixed.json`, активировать).
- ✅ Создавать новые тестовые файлы (`test_*.py`) в корне.

---

## 4. План работы

### Фаза A · Диагностика (15 минут)

1. Прочитать `MVP_PLAN.md` целиком (статус системы).
2. Прочитать `agent/poller.py` (раздел `_poll`) — увидеть точный контракт.
3. Прочитать `workflow_fixed.json` (узлы `Poll Webhook`, `Poll Handler`, `Poll Respond`):
   - какой `path` стоит у `Poll Webhook`?
   - какой `httpMethod`?
   - что отвечает `Poll Respond` (mode, body)?
   - есть ли проверка X-Agent-Token в `Poll Handler`?
4. **Прямо потыкать прод**:
   ```bash
   curl -i -H "X-Agent-Token: 3bafc0aa-bb42-4e01-b918-8c5a8a252a43" \
        https://n8n-production-419d8.up.railway.app/webhook/agent-poll
   ```
   Зафиксировать: статус, заголовки, тело. По нему понятно — 404 (workflow не активен)
   или 200 + плохое тело (workflow есть, но broken).
5. Аналогично для `/webhook/agent-result` (через `python test_webhook.py`).

### Фаза B · Починка (30–60 минут)

В зависимости от диагноза:

**Сценарий 1: 404 → воркфлоу вообще не активен**
- Зайти в n8n на Railway (URL = `railway_url` без `/webhook/...` + `/`).
- Импортировать `workflow_fixed.json` (если не импортирован).
- Активировать воркфлоу (toggle вверху редактора).
- Re-test curl → ожидаем 200 + JSON.

**Сценарий 2: 200 + не-JSON → workflow есть, но `Poll Respond` отдаёт мусор**
- Открыть узел `Poll Respond` в n8n.
- Убедиться `responseMode: responseNode` (если он не respondToWebhook,
  n8n отдаст default-ответ webhook-узла, который часто HTML).
- В `Poll Handler` (узел `n8n-nodes-base.code`) убедиться что возвращает
  объект формата §3.2 (либо `{success, data: null, error: null}`,
  либо `{success, data: {...}, error: null}`).
- Re-test.

**Сценарий 3: 200 + JSON, но всё ещё `Expecting value` в логе**
- Проверить, что в начале тела нет BOM, пробелов или невалидных символов.
- Проверить `Content-Type: application/json` в ответе.
- Re-test.

**Сценарий 4: 401 / неправильная авторизация**
- В `Poll Handler` найти проверку `X-Agent-Token`. Сравнить с `config.json:agent_token`.
- Привести к равенству.

### Фаза C · Сквозной тест (15 минут)

1. Запустить агента:
   ```powershell
   cd E:\dispatch
   python main.py
   ```
2. Открыть `agent.log` в режиме tail (или просто Read через инструмент каждые 10 с).
3. В течение **минуты** в логе **не должно появиться ни одной строки `poll error`**.
   Должно быть тихо (агент успешно поллит) или строки уровня INFO.
4. Положить тестовую команду в очередь n8n (через UI воркфлоу или
   отправить `/screenshot` в Telegram-бот) → агент должен подхватить, выполнить,
   отдать результат назад → результат прийти в Telegram.

### Фаза D · Зачистка (5 минут)

1. Обновить `MVP_PLAN.md`:
   - В разделе «Что уже сделано» убрать упоминание блокера.
   - В разделе «Что блокирует прямо сейчас» написать что починено + дату.
2. (Опционально) написать одну строку про сделанное в `agent.log` или комментом.

---

## 5. TODO (granular checklist)

### Диагностика
- [ ] Прочитан `MVP_PLAN.md`
- [ ] Прочитан `NEXT_SESSION.md` (этот файл)
- [ ] Прочитан `agent/poller.py` (раздел `_poll`)
- [ ] В `workflow_fixed.json` найдены узлы `Poll Webhook`, `Poll Handler`, `Poll Respond`
- [ ] `curl` к `/webhook/agent-poll` сделан, ответ зафиксирован
- [ ] `python test_webhook.py` запущен, ответ зафиксирован
- [ ] Определён сценарий (1/2/3/4 из §4 Фаза B)

### Починка
- [ ] Если 404 → workflow импортирован в n8n и активирован
- [ ] `Poll Webhook` имеет `httpMethod: GET`, `path: agent-poll`
- [ ] `Poll Webhook.responseMode = responseNode`
- [ ] `Poll Handler` возвращает структуру §3.2 (валидный JSON, оба case'а)
- [ ] `Poll Respond` имеет правильный body (либо из item, либо явный JSON)
- [ ] Token-check в `Poll Handler` равен `config.json:agent_token`
- [ ] `curl` к `/webhook/agent-poll` теперь возвращает 200 + валидный JSON
- [ ] Аналогично для `/webhook/agent-result`

### Сквозной тест
- [ ] Агент запущен (`python main.py`)
- [ ] В `agent.log` за минуту нет ни одной строки `poll error`
- [ ] Тестовая команда положена в очередь
- [ ] Агент подхватил команду (в логе видно `cmd.exec` или аналог)
- [ ] Результат пришёл в Telegram

### Зачистка
- [ ] `MVP_PLAN.md` обновлён (блокер снят, Шаг 1 ✅)
- [ ] Если меняли `workflow_fixed.json` — экспортнут актуальный из n8n назад в файл
- [ ] Если изменения значимые — короткий commit / запись в `agent.log` для истории

---

## 6. Файлы и команды для быстрого старта

### Ключевые файлы

| Файл                             | Зачем смотреть                                     |
|----------------------------------|----------------------------------------------------|
| `E:\dispatch\MVP_PLAN.md`        | контекст и снимок состояния                        |
| `E:\dispatch\config.json`        | `railway_url`, `agent_token`, `poll_interval_*`    |
| `E:\dispatch\agent.log`          | смотреть результат починки                         |
| `E:\dispatch\agent\poller.py`    | контракт ответа на стороне клиента                 |
| `E:\dispatch\workflow_fixed.json`| источник правды воркфлоу (импортируется в n8n)     |
| `E:\dispatch\test_webhook.py`    | готовый скрипт-тест для `/webhook/agent-result`    |
| `E:\dispatch\main.py`            | точка входа Python-агента                          |

### Команды

```powershell
# Прочитать конфиг (URL и токен)
Get-Content E:\dispatch\config.json -Raw

# Дёрнуть poll-эндпоинт
$cfg = Get-Content E:\dispatch\config.json -Raw | ConvertFrom-Json
$h = @{ 'X-Agent-Token' = $cfg.agent_token }
Invoke-WebRequest "$($cfg.railway_url)/webhook/agent-poll" -Headers $h -UseBasicParsing

# Тест result-эндпоинта (готовый скрипт)
cd E:\dispatch; python test_webhook.py

# Запустить агента в обычном режиме (с консолью)
cd E:\dispatch; python main.py

# Хвост лога (PowerShell-аналог tail -f)
Get-Content E:\dispatch\agent.log -Wait -Tail 30
```

### Как открыть n8n на Railway

URL из `config.json:railway_url` без `/webhook/...` — там же и UI редактора n8n.
Может потребоваться логин (если не настроен SSO — basic-auth).

---

## 7. Готовность (приёмка)

Шаг считается закрытым, когда **все три** проверки проходят:

1. **curl-тест:**
   ```bash
   curl -s -H "X-Agent-Token: <token>" https://.../webhook/agent-poll | head -c 200
   ```
   Возвращает валидный JSON, парсится `python -c "import json,sys; json.load(sys.stdin)"`.

2. **Лог-тест:**
   После минуты работы `python main.py` в `agent.log` **нет строк уровня ERROR**
   с подстрокой `poll error`. Допустимы WARNING `poll: timeout` (редко) и INFO о смене статуса.

3. **Сквозной тест:**
   Через UI n8n (или Telegram-бот, если он подключён) положить команду
   `{type: "screenshot"}` в очередь → агент её подхватывает → JPEG приходит
   обратно (в Telegram-бот, в n8n execution log, или хотя бы в `agent.log`
   виден `result.sent`).

---

## 8. Если что-то пойдёт не так — типичные грабли

| Симптом                                                    | Скорее всего                                     | Что делать                                                                 |
|------------------------------------------------------------|--------------------------------------------------|---------------------------------------------------------------------------|
| `curl` отдаёт HTML с «Welcome to n8n»                      | Воркфлоу не активирован                          | Активировать toggle вверху редактора                                       |
| `curl` отдаёт 404                                          | Путь webhook'а не совпадает с `/webhook/agent-poll` | Проверить поле `path` в узле `Poll Webhook`                              |
| `curl` отдаёт 200 + пусто                                  | `Poll Respond` отдаёт пустое тело                | Указать явный JSON в body, или `responseMode: responseNode` + код handler |
| `curl` отдаёт 401                                          | Токен в JS-коде Poll Handler ≠ `config.json`     | Синхронизировать                                                           |
| В логе всё чисто, но команды не выполняются                | Очередь пустая                                   | Положить команду через UI n8n или из Telegram                              |
| Команда выполнилась, но результат не дошёл в Telegram      | `/webhook/agent-result` сломан (та же проблема)  | Применить ту же диагностику к `Result Webhook` / `Result Respond`         |
| Агент молчит, в логе ничего                                | Агент не запущен                                 | `python main.py` в новом терминале                                         |
| Лог переполнен старыми записями                            | Месячный мусор                                   | Допустимо переименовать `agent.log` → `agent.log.bak` и начать с чистого  |

---

## 9. После Шага 1 — что дальше

Следующий шаг — **Шаг 2 MVP**: сквозной тест screenshot через Telegram-бот.
Кикофф-промт лежит в [MVP_PLAN.md → Шаг 2](MVP_PLAN.md).

Полный список оставшихся шагов:
- Шаг 2 — Сквозной тест screenshot
- Шаг 3 — Tray-приложение (pystray, иконка, меню)
- Шаг 4 — Control Center запускает/останавливает агента
- Шаг 5 — Реальный автозапуск Windows
- Шаг 6 — Все 7 хендлеров проверены end-to-end
- Шаг 7 — Сборка `.exe` (PyInstaller + electron-builder)

---

## 10. Глоссарий (для скорости вхождения)

- **Агент** — Python-программа `main.py`, крутится в трее, опрашивает n8n.
- **Поллер** — `agent/poller.py`, фоновый поток внутри агента, который делает GET.
- **Хендлер** — `agent/handlers.py`, исполняет конкретную команду (screenshot, terminal, …).
- **n8n** — low-code платформа автоматизации, лежит на Railway.
- **Воркфлоу** — конкретный сценарий в n8n (наш — «Dispatch Agent v2», 34 узла).
- **AGENT_TOKEN** — UUID, общий секрет агент ⇄ n8n. Заголовок `X-Agent-Token`.
- **chat_id** — Telegram-собеседник, куда n8n должна отправить результат.
- **Control Center** — Electron-приложение в `agent_os_app/`, GUI для просмотра состояния.
