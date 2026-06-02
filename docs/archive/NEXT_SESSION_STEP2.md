# Следующая сессия · Шаг 2 MVP — сквозной тест screenshot

> **Полный кикофф-документ** для свежей сессии. Цель — первый раз провести команду
> по всей цепочке: **Telegram → n8n → агент → скриншот → n8n → обратно в Telegram**.
> Это первый «живой» сценарий: после него видно, что вся архитектура реально работает.
>
> **Предусловие:** Шаг 1 закрыт (см. [NEXT_SESSION.md](NEXT_SESSION.md)) — `/webhook/agent-poll`
> отвечает валидным JSON, в `agent.log` нет `poll error`. Если ещё валятся ошибки poll —
> вернись к Шагу 1, без него Шаг 2 невозможен.

---

## 0. TL;DR (если у тебя 30 секунд)

Юзер жмёт в Telegram-боте «📸 Снимок экрана» (или шлёт `/screenshot`).
n8n кладёт в очередь команду `{type:"screenshot", chat_id, command_id}`.
Агент её забирает poll'ом, делает скриншот, шлёт base64 назад на `/webhook/agent-result`.
n8n достаёт `result.data.image`, декодирует и `sendPhoto` обратно в тот же `chat_id`.

**Готово, когда:** в Telegram реально пришёл JPEG, и в `agent.log` за этот запрос
виден переход `Status: idle → Status: active → Status: idle`.

**Два подводных камня, которые почти гарантированно всплывут (детали в §3):**
1. n8n должен брать картинку из `result.data.image`, **не** из `result.data`.
2. `chat_id` должен пройти сквозь всю цепочку (Telegram → очередь → result → sendPhoto).

---

## 1. Контекст

### 1.1 Архитектура (напоминание)

```
Telegram (телефон) ⇄ Railway n8n (сервер) ⇄ Desktop Agent (Python, polling)
```

Агент не слушает порты — он **сам** опрашивает n8n каждые 2 с. Поэтому «команда»
не доставляется агенту напрямую: она кладётся в очередь на стороне n8n
(в `staticData`, см. коммит `383e5b4`), а агент забирает её на следующем poll'е.

### 1.2 Полный путь одной команды screenshot

```
1. Юзер в Telegram жмёт кнопку «📸 Снимок экрана»  (или /screenshot)
2. Telegram Trigger в n8n получает update → узнаёт chat_id
3. n8n генерит command_id, кладёт в очередь: {command_id, chat_id, type:"screenshot", payload:{}}
4. Агент: GET /webhook/agent-poll → получает эту команду
5. poller._execute → dispatch → handle_screenshot → JPEG → base64
6. Агент: POST /webhook/agent-result с {command_id, chat_id, command_type, result}
7. n8n Result Webhook: достаёт result.data.image → base64-decode → бинарь
8. n8n: Telegram sendPhoto в chat_id из шага 2
9. JPEG приходит юзеру в чат
```

Любое звено может оборваться. §4 ведёт по диагностике звено за звеном.

### 1.3 Что уже точно работает (не трогать)

- `handle_screenshot` (`agent/handlers.py:26`) — делает `mss` capture с primary-монитора,
  ресайз до 1280px, JPEG quality 75, base64. **Возвращает `{"image": b64, "width", "height"}`.**
- `poller._execute` / `_post_result` (`agent/poller.py:120-163`) — корректно формирует payload
  результата и шлёт его на `/webhook/agent-result` (таймаут 30 с, для больших — 120 с).
- `dispatch` (`agent/handlers.py:356`) — берёт `command["type"]` и `command["payload"]`,
  роутит в нужный хендлер.

---

## 2. Промт для новой сессии (copy-paste в чистый `claude`)

```
Я работаю над проектом Dispatch (Windows-агент → Railway n8n → Telegram).
Корень — E:\dispatch.

Перед чем-либо ещё прочитай:
1. E:\dispatch\MVP_PLAN.md — общее состояние и шаги до MVP.
2. E:\dispatch\NEXT_SESSION_STEP2.md — детальное ТЗ на сегодня (этот шаг).

Цель сегодня — Шаг 2 MVP: первый сквозной тест screenshot.
Юзер жмёт «Снимок экрана» в Telegram-боте → через ~3 секунды в чат приходит JPEG.

ВАЖНЫЕ ОГРАНИЧЕНИЯ:
- НЕ трогать UI (agent_os_app/, agent_os_tabs/), CSS, Electron, дизайн.
- НЕ менять контракт result в poller.py / handlers.py — он уже правильный
  (скриншот лежит в result.data.image, см. NEXT_SESSION_STEP2 §3.2).
- Чинить нужно сторону n8n: чтобы Result Webhook достал result.data.image,
  декодировал и отправил sendPhoto в правильный chat_id.
- Изменения воркфлоу делать в workflow_fixed.json (источник истины), затем
  импортировать в Railway-n8n.

Стартуй с §4 «План работы». Закрой задачу по §6 «Готовность».
По завершении обнови раздел «Что уже сделано» в MVP_PLAN.md (Шаг 2 ✅).
```

---

## 3. ТЗ (точные контракты — из реального кода)

### 3.1 Команда, которую n8n кладёт в очередь (читает poll)

`GET /webhook/agent-poll` при наличии команды должен вернуть (форма §3.2 из
[NEXT_SESSION.md](NEXT_SESSION.md)):

```json
{
  "success": true,
  "data": {
    "command_id": "scr_8f2a",
    "chat_id":    100423001,
    "type":       "screenshot",
    "payload":    {}
  },
  "error": null
}
```

> ⚠️ **Ключ `payload`, не `params`.** `dispatch()` читает `command.get("payload", {})`
> (`agent/handlers.py:358`). Для screenshot payload игнорируется, но структуру
> держи правильной — это понадобится для terminal/launch-app на Шаге 6.
> `chat_id` обязателен и должен совпадать с тем, кто прислал команду в Telegram.

### 3.2 Что агент шлёт назад (`POST /webhook/agent-result`)

`poller._post_result` (`agent/poller.py:131`) формирует **ровно** это:

```json
{
  "command_id":   "scr_8f2a",
  "chat_id":      100423001,
  "command_type": "screenshot",
  "result": {
    "success": true,
    "data": {
      "image":  "<base64 JPEG без data:-префикса>",
      "width":  1280,
      "height": 720
    },
    "error": null
  }
}
```

> 🔑 **Самое важное для этого шага.** Картинка лежит в **`result.data.image`** —
> это base64-строка JPEG (чистый base64, без `data:image/jpeg;base64,`).
> `width`/`height` рядом — для информации. n8n **обязан** взять именно
> `$json.result.data.image`, а не `$json.result.data`.

Если хендлер упал, форма та же, но:
```json
{ "result": { "success": false, "data": null, "error": "screenshot failed: ..." } }
```
n8n в этом случае разумно отправить юзеру текст ошибки, а не пытаться слать фото.

### 3.3 Что n8n должен сделать в Result Webhook (узлы `Result *`)

Псевдо-логика (проверь соответствие в `workflow_fixed.json`):

```
1. Принять POST, проверить X-Agent-Token.
2. По command_id найти в очереди исходный chat_id (или взять chat_id прямо из тела —
   агент его дублирует в payload, можно использовать его напрямую).
3. Если result.success == false → Telegram sendMessage(chat_id, "Ошибка: " + result.error).
4. Если result.command_type == "screenshot":
     - bin = base64decode(result.data.image)
     - Telegram sendPhoto(chat_id, bin)   // как binary data, не как URL
5. Ответить агенту: 200 { "success": true }.
```

> В n8n для отправки бинарного фото обычно нужен узел «Convert to File / Move Binary Data»
> (base64 string → binary property), а затем Telegram-узел `sendPhoto` с
> `Binary Data = true` и указанием имени binary-свойства. Проверь, как это
> сделано в текущем воркфлоу, и поправь поле-источник на `result.data.image`.

### 3.4 Что реально пишется в `agent.log` (важно для приёмки!)

`poller.py` + `main.py` логируют **только**:

| Строка в логе                         | Когда                                  | Уровень |
|---------------------------------------|----------------------------------------|---------|
| `main: Dispatch Agent starting`       | старт                                  | INFO    |
| `main: Status: idle`                  | очередь пуста / нет команд             | INFO    |
| `main: Status: active`                | **команда взята и выполняется**        | INFO    |
| `main: Status: error`                 | сеть/таймаут/исключение poll           | INFO    |
| `poller: poll: timeout` / `connection error` | проблемы сети                   | WARNING |
| `poller: poll error: ...`             | необработанное исключение poll         | ERROR   |
| `poller: post result error: ...`      | не удалось отдать результат            | ERROR   |

> ⚠️ **Строк `poll.recv` / `cmd.exec` / `result.sent` в коде НЕТ.**
> Критерий из MVP_PLAN.md («видно последовательность poll.recv → cmd.exec → result.sent»)
> написан авансом и коду не соответствует. Реальный наблюдаемый признак успеха —
> переход **`Status: idle → Status: active → Status: idle`** вокруг исполнения команды.
>
> **Опционально (рекомендуется), небольшое изменение для наблюдаемости:**
> добавить 3 INFO-строки в `poller.py`, тогда лог станет читаемым end-to-end:
> - `agent/poller.py:58` после `command = self._poll(...)` →
>   `if command: logger.info(f"cmd.recv {command.get('type')} id={command.get('command_id')}")`
> - `agent/poller.py:129` после `dispatch(...)` →
>   `logger.info(f"cmd.done {command.get('type')} success={result.get('success')}")`
> - `agent/poller.py:161` после `resp.raise_for_status()` →
>   `logger.info(f"result.sent id={command_id} chat={chat_id}")`
>
> Это единственное оправданное касание `poller.py` на этом шаге. Если делаешь —
> отрази в §6 как факт, что критерий лога теперь буквальный.

---

## 4. План работы

### Фаза A · Подготовка (5 минут)

1. Прочитать `MVP_PLAN.md` и этот файл.
2. Убедиться, что Шаг 1 закрыт:
   ```powershell
   $cfg = Get-Content E:\dispatch\config.json -Raw | ConvertFrom-Json
   Invoke-WebRequest "$($cfg.railway_url)/webhook/agent-poll" `
     -Headers @{ 'X-Agent-Token' = $cfg.agent_token } -UseBasicParsing
   ```
   Должен быть 200 + валидный JSON. Если 404/HTML — стоп, идти в [NEXT_SESSION.md](NEXT_SESSION.md).
3. Найти в Telegram нужного бота (токен/имя — в воркфлоу узла Telegram Trigger
   и/или в credentials n8n). Убедиться, что у бота есть кнопка «📸 Снимок экрана»
   или работает команда `/screenshot`.

### Фаза B · Запустить агента и проследить poll (10 минут)

1. ```powershell
   cd E:\dispatch; python main.py
   ```
   (запустится трей-иконка; консоль оставить открытой — туда дублируется лог).
2. В отдельном окне — хвост лога:
   ```powershell
   Get-Content E:\dispatch\agent.log -Wait -Tail 30
   ```
   Должно быть тихо или `Status: idle`. Никаких `poll error`.

### Фаза C · Дёрнуть команду из Telegram (15 минут)

1. В боте нажать «📸 Снимок экрана» / отправить `/screenshot`.
2. Смотреть лог: в течение ~2–4 с должно появиться `Status: active`, затем снова `Status: idle`.
3. **Если фото пришло в чат — Шаг 2 пройден, переходи к Фазе E.**
4. Если `Status: active` появился, но фото не пришло → проблема на стороне n8n
   (Result Webhook не отдал фото). Идти в Фазу D, сценарий 2/3.
5. Если `Status: active` так и не появился → команда не попала в очередь /
   poll её не вернул. Идти в Фазу D, сценарий 1.

### Фаза D · Починка по симптому (по необходимости)

**Сценарий 1 — `Status: active` не появляется (агент не получает команду)**
- Открыть в n8n историю исполнений (Executions) — сработал ли Telegram Trigger?
- Проверить, что после триггера команда реально кладётся в очередь (`staticData`)
  с правильным `command_id`, `chat_id`, `type:"screenshot"`, `payload:{}`.
- Проверить `Poll Handler`: при наличии команды отдаёт её в `data` (форма §3.1),
  и **удаляет/помечает** её, чтобы не выдавать повторно.
- Re-test из Telegram.

**Сценарий 2 — фото не уходит, в логе `Status: active → idle` есть, агент отдал результат**
- В n8n Executions найти исполнение Result Webhook. Посмотреть входной JSON —
  там должен быть `result.data.image` (длинная base64).
- Проверить узел, который готовит фото: источник должен быть **`{{$json.result.data.image}}`**
  (частая ошибка — указывают `result.data`, и приходит `[object Object]` или пусто).
- Проверить, что base64 конвертится в **binary** перед Telegram `sendPhoto`
  (узел Convert to File / Move Binary Data → property, например `data`),
  и что Telegram-узел шлёт именно binary, а не текст/URL.
- Проверить `chat_id` в `sendPhoto` — берётся из тела результата или из очереди по `command_id`.
- Re-test.

**Сценарий 3 — Telegram отвечает ошибкой (фото битое / "wrong file identifier")**
- Значит base64 декодируется неверно или префикс `data:` попал в строку.
  Агент шлёт **чистый** base64 (без префикса) — убедиться, что n8n не дописывает префикс
  и декодирует именно из base64-строки в binary.

**Сценарий 4 — в логе `post result error`**
- Агент не смог отдать результат. Применить диагностику Шага 1, но к
  `POST /webhook/agent-result`: 404 (узел не активен/путь не тот) или таймаут
  (Railway долго отвечает на большой base64 — но 30 с обычно хватает на 1280px JPEG).

### Фаза E · Зачистка (5 минут)

1. Если правил воркфлоу в n8n — **экспортнуть** актуальную версию обратно в
   `workflow_fixed.json` (источник истины не должен отставать).
2. Обновить `MVP_PLAN.md`: в «Что уже сделано» отметить Шаг 2 ✅ + дату,
   коротко — какие были фиксы.
3. Если добавлял INFO-логи в `poller.py` — упомянуть это.

---

## 5. TODO (granular checklist)

### Подготовка
- [ ] Прочитан `MVP_PLAN.md` и `NEXT_SESSION_STEP2.md`
- [ ] Шаг 1 подтверждён: `/webhook/agent-poll` → 200 + JSON
- [ ] Найден Telegram-бот, есть кнопка/команда screenshot

### Запуск и poll
- [ ] `python main.py` запущен, трей-иконка появилась
- [ ] Хвост `agent.log` открыт, видно `Status: idle`, нет `poll error`

### Сквозной прогон
- [ ] Нажата кнопка «Снимок экрана» в Telegram
- [ ] В логе появился `Status: active`
- [ ] В логе вернулся `Status: idle` (команда отработала)
- [ ] **JPEG реально пришёл в Telegram-чат**

### Починка (если потребовалась)
- [ ] Telegram Trigger срабатывает (видно в n8n Executions)
- [ ] Команда кладётся в очередь с `chat_id` и `type:"screenshot"`
- [ ] Poll возвращает команду в форме §3.1 и не выдаёт её повторно
- [ ] Result Webhook берёт фото из `result.data.image` (не `result.data`)
- [ ] base64 → binary → `sendPhoto` с правильным `chat_id`

### Зачистка
- [ ] (если правил n8n) `workflow_fixed.json` экспортнут заново
- [ ] `MVP_PLAN.md` обновлён, Шаг 2 ✅
- [ ] (опц.) добавлены INFO-логи `cmd.recv / cmd.done / result.sent` в `poller.py`

---

## 6. Готовность (приёмка)

Шаг закрыт, когда **обе** проверки проходят:

1. **Главная:** из Telegram нажат «Снимок экрана» → через ~2–4 с в чат
   **пришёл реальный JPEG** текущего экрана ПК.
2. **Лог:** в `agent.log` вокруг этого запроса виден переход
   `Status: idle → Status: active → Status: idle`, и **нет** строк `ERROR`
   (`poll error` / `post result error`).

> Если добавлял INFO-логи — дополнительно убедись, что в логе появились
> `cmd.recv screenshot`, `cmd.done screenshot success=True`, `result.sent`.

Плюс желательная проверка регрессии Шага 1: за минуту простоя (без команд)
в логе по-прежнему нет `poll error`.

---

## 7. Файлы и команды

### Ключевые файлы

| Файл                              | Зачем смотреть                                    |
|-----------------------------------|---------------------------------------------------|
| `E:\dispatch\agent\handlers.py:26`| `handle_screenshot` — форма результата (`.image`) |
| `E:\dispatch\agent\poller.py:131` | `_post_result` — точная форма payload результата  |
| `E:\dispatch\workflow_fixed.json` | узлы Telegram Trigger / Poll / Result (правим тут)|
| `E:\dispatch\config.json`         | `railway_url`, `agent_token`                      |
| `E:\dispatch\agent.log`           | признак успеха (`Status: active`)                 |
| `E:\dispatch\MVP_PLAN.md`         | контекст и финальная отметка Шага 2               |

### Команды

```powershell
# Проверить, что Шаг 1 жив
$cfg = Get-Content E:\dispatch\config.json -Raw | ConvertFrom-Json
Invoke-WebRequest "$($cfg.railway_url)/webhook/agent-poll" `
  -Headers @{ 'X-Agent-Token' = $cfg.agent_token } -UseBasicParsing

# Запустить агента (трей + лог в консоль)
cd E:\dispatch; python main.py

# Хвост лога в реальном времени
Get-Content E:\dispatch\agent.log -Wait -Tail 30
```

---

## 8. Если что-то пойдёт не так — типичные грабли

| Симптом                                          | Скорее всего                                  | Что делать                                                        |
|--------------------------------------------------|-----------------------------------------------|-------------------------------------------------------------------|
| `Status: active` не появляется                   | команда не попала в очередь / poll не вернул  | n8n Executions: сработал ли Telegram Trigger; проверить enqueue   |
| Команда выдаётся агенту повторно (бесконечно)    | Poll Handler не вычищает очередь после выдачи | После выдачи помечать/удалять команду из `staticData`             |
| `Status: active → idle` есть, фото нет           | Result Webhook не шлёт фото                   | Источник `result.data.image`; base64→binary; sendPhoto binary    |
| Telegram: `wrong file identifier / bad request`  | base64 не декодирован в binary, или есть `data:`-префикс | декодировать чистый base64 в binary, без префикса        |
| Фото уходит не тому / никому                      | `chat_id` потерялся в цепочке                 | Протянуть `chat_id` Telegram→очередь→result→sendPhoto             |
| `post result error` в логе                        | `/webhook/agent-result` 404 или таймаут       | Та же диагностика, что Шаг 1, но для result-эндпоинта             |
| Скриншот чёрный / не тот монитор                  | `mss.monitors[1]` = primary                   | ОК для MVP; мультимонитор — позже                                 |
| Пришёл текст ошибки вместо фото                   | `result.success == false`                     | Прочитать `result.error`, чинить хендлер/окружение на ПК          |

---

## 9. После Шага 2 — что дальше

- **Шаг 3** — Tray-приложение (pystray, цвет иконки по статусу, меню). Частично
  уже есть в `main.py` — нужно проверить/доделать. Промт в [MVP_PLAN.md → Шаг 3](MVP_PLAN.md).
- Шаг 4 — Control Center запускает/останавливает агента.
- Шаг 5 — Реальный автозапуск Windows.
- Шаг 6 — Все 7 хендлеров end-to-end (screenshot уже будет проверен здесь).
- Шаг 7 — Сборка `.exe` (PyInstaller + electron-builder).

---

## 10. Глоссарий

- **Команда** — `{command_id, chat_id, type, payload}`, кладётся n8n в очередь.
- **Очередь** — `staticData` воркфлоу n8n: буфер между Telegram-триггером и poll-ом агента.
- **Хендлер** — `agent/handlers.py`, исполняет команду; screenshot → `handle_screenshot`.
- **result.data.image** — base64 JPEG, который n8n должен превратить в фото.
- **chat_id** — Telegram-собеседник; должен пройти всю цепочку, иначе фото некуда слать.
- **Status: active** — поллер взял команду и выполняет её; главный признак в логе.
- **Poll Webhook / Result Webhook** — два n8n-эндпоинта: забрать команду / принять результат.
