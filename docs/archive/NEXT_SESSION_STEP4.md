# Следующая сессия · Шаг 4 MVP — Control Center управляет агентом

> **Полный кикофф-документ.** Цель — добавить в Control Center кнопки
> «Запустить агент / Остановить агент» и точный статус с PID. Сейчас Control Center
> только **читает** состояние (CPU/RAM, лог, конфиг) — управлять агентом нельзя.
>
> **Предусловие:** Шаги 1–3 закрыты (агент работает, трей готов). Это первый шаг,
> где мы пишем новый код на стороне Electron.

---

## 0. TL;DR (30 секунд)

Добавить 3 новых IPC в `agent_os_app/main.js`: `agent:start`, `agent:stop`,
`agent:isRunning`. Пробросить их в `preload.js`. В renderer (`app.html`) добавить
в Dashboard кнопку-тоггл «Запустить/Остановить» + статус с PID, и дёргать
`isAgentRunning()` в уже существующем `refreshAll()`.

**Главная сложность:** дочерний процесс детачится, а Control Center может
перезапуститься — тогда in-memory PID теряется. Решение — **pid-файл** рядом с
`config.json`, и `isRunning` проверяет и его, и живость процесса.

**Готово, когда:** из UI можно запустить и остановить агента; виден статус и PID;
после запуска `agent.log` снова пишется, иконка в трее появляется.

---

## 1. Контекст

### 1.1 Что сейчас умеет Control Center (read-only)

`agent_os_app/main.js` регистрирует только **читающие** IPC:
`config:read/write`, `log:tail`, `system:info`, `shell:openPath`, `metrics:host`,
`agent:status` (по mtime лога), `events:recent`, `workflow:read`.
**Управления процессом нет.** `preload.js` (`api.*`) тоже без start/stop.

### 1.2 Как сейчас определяется статус

`agent:status` (`main.js:158`) — это **косвенный** сигнал: свежесть mtime
`agent.log`. `online` если лог свежее `poll_interval_active*3`, иначе `stale`/`offline`.
Это не «жив ли процесс», а «давно ли он писал». Шаг 4 добавляет **прямой** сигнал —
реально ли запущен процесс агента (по PID).

### 1.3 Запуск агента сегодня

Только вручную: `python main.py` (или `pythonw main.py`). Цель Шага 4 — кнопка в UI.

---

## 2. Промт для новой сессии (copy-paste)

```
Я работаю над проектом Dispatch (Windows-агент → Railway n8n → Telegram).
Корень — E:\dispatch.

Прочитай сперва:
1. E:\dispatch\MVP_PLAN.md
2. E:\dispatch\NEXT_SESSION_STEP4.md — ТЗ на сегодня.

Цель — Шаг 4: Control Center запускает/останавливает агента и показывает PID.

Сделать:
1. agent_os_app/main.js: добавить IPC agent:start, agent:stop, agent:isRunning.
   spawn python (или собранный .exe) detached, windowsHide, stdio ignore.
   PID писать в pid-файл рядом с config.json; isRunning проверять живость по PID.
2. agent_os_app/preload.js: пробросить startAgent(), stopAgent(), isAgentRunning().
3. agent_os_app/renderer/app.html: в Dashboard добавить кнопку «Запустить/Остановить»
   + статус с PID; в существующем refreshAll() дёргать isAgentRunning().

ОГРАНИЧЕНИЯ:
- НЕ ломать существующие IPC и дизайн.
- Следовать стилю кода main.js (ipcMain.handle, {ok, ...}) и паттерну привязки
  кликов в app.html (dataset.bound — см. liveAutostart на строке ~1206).
- Запуск процесса — windowsHide, без консоли.

Закрой по §6 «Готовность». Обнови MVP_PLAN.md (Шаг 4 ✅).
```

---

## 3. ТЗ (контракты — в стиле существующего main.js)

Все IPC возвращают объект `{ ok: bool, ... }` — как остальные хэндлеры в `main.js`.

### 3.1 `agent:start`

```js
ipcMain.handle('agent:start', async () => {
  // 1. если уже запущен (по isRunning) → вернуть { ok:true, already:true, pid }
  // 2. определить команду запуска:
  //    - dev: pythonw.exe + <root>/main.py   (windowsHide)
  //    - prod: собранный dist/DispatchAgent.exe, если есть (Шаг 7)
  // 3. spawn(cmd, args, { detached:true, stdio:'ignore', windowsHide:true });
  //    child.unref();
  // 4. записать child.pid в pid-файл AGENT_PID_PATH
  // 5. вернуть { ok:true, pid }
});
```

Запускающую команду строить так:
- Корень проекта = `path.resolve(__dirname, '..')` (там же `main.py`, как config/log).
- `pythonw` найти: `pythonw` из PATH, либо `python` (fallback). На Windows
  `pythonw.exe` не создаёт консоль — предпочесть его.
- В prod (Шаг 7) приоритет — `DispatchAgent.exe`, если найден рядом.

### 3.2 `agent:stop`

```js
ipcMain.handle('agent:stop', async () => {
  // 1. прочитать PID из pid-файла
  // 2. process.kill(pid)  (Windows: можно taskkill /PID <pid> /T /F для дерева)
  // 3. удалить pid-файл
  // 4. вернуть { ok:true, stopped:true }
});
```

> На Windows graceful-SIGTERM не работает как в POSIX. Допустимо `taskkill /PID <pid> /F`
> (через `execFile`). Поллер — daemon-поток внутри процесса, отдельной чистки не требует.

### 3.3 `agent:isRunning`

```js
ipcMain.handle('agent:isRunning', async () => {
  // 1. прочитать PID из pid-файла (если нет → { ok:true, running:false })
  // 2. проверить живость: process.kill(pid, 0) в try/catch
  //    (ESRCH → мёртв; EPERM → жив, но чужой). На Windows надёжнее
  //    tasklist /FI "PID eq <pid>" и проверить, что в выводе есть процесс.
  // 3. вернуть { ok:true, running:bool, pid, uptimeSec? }
});
```

> **Зачем pid-файл, а не переменная в памяти:** дочерний процесс детачится и
> переживает Control Center. Если CC перезапустить, in-memory ссылка пропадёт, и
> isRunning «забудет» про живого агента. Pid-файл (рядом с `config.json`,
> например `agent.pid`) переживает рестарт CC. Путь:
> `const AGENT_PID_PATH = path.resolve(__dirname, '..', 'agent.pid');`

### 3.4 `preload.js` — добавить в `api`

```js
startAgent:     ()  => ipcRenderer.invoke('agent:start'),
stopAgent:      ()  => ipcRenderer.invoke('agent:stop'),
isAgentRunning: ()  => ipcRenderer.invoke('agent:isRunning'),
```

### 3.5 Renderer (`app.html`) — Dashboard

1. Добавить в Dashboard кнопку/тоггл «Запустить агент / Остановить агент» и
   строку статуса с PID.
2. Привязать клик по паттерну `liveAutostart` (`app.html:1206-1218`):
   ```js
   const btn = $$('agentToggle');
   if (btn && !btn.dataset.bound) {
     btn.dataset.bound = '1';
     btn.addEventListener('click', async () => {
       const st = await window.api.isAgentRunning();
       const r = st.running ? await window.api.stopAgent() : await window.api.startAgent();
       showToast(r.ok ? (st.running ? 'Агент остановлен' : 'Агент запущен') : 'Ошибка');
     });
   }
   ```
3. В `refreshAll()` (`app.html:1154`) добавить вызов `isAgentRunning()` в
   `Promise.all` и отрисовать `running`/`pid`. Учесть, что `refreshAll`
   уже крутится по `setInterval(refreshAll, LIVE_INTERVAL_MS)` (`app.html:1634`).

### 3.6 Что НЕ трогать

- ✋ Существующие IPC (`config:*`, `metrics:host`, `agent:status` и т.д.).
- ✋ Дизайн/вёрстку — добавить элементы в стиле существующих карточек, не переделывать.
- ✋ `agent/poller.py`, `main.py` — не нужны изменения для этого шага.

---

## 4. План работы

### Фаза A · main.js (40 мин)
1. Прочитать `agent_os_app/main.js` (стиль IPC, пути CONFIG_PATH/LOG_PATH).
2. Добавить `AGENT_PID_PATH`.
3. Реализовать `agent:isRunning` (сначала — на нём строятся остальные).
4. Реализовать `agent:start` (spawn detached + запись pid-файла).
5. Реализовать `agent:stop` (taskkill + удаление pid-файла).

### Фаза B · preload.js (5 мин)
1. Добавить 3 метода в `api`.

### Фаза C · renderer (30 мин)
1. Добавить элементы в Dashboard (кнопка + статус + PID).
2. Привязать клик (паттерн `dataset.bound`).
3. Включить `isAgentRunning()` в `refreshAll()`.

### Фаза D · Проверка (20 мин)
1. `cd agent_os_app; npm start`.
2. Нажать «Запустить» → `agent.log` начинает обновляться, в трее иконка
   (если main.py тоже стартует трей — да), статус показывает `running` + PID.
3. Нажать «Остановить» → процесс убит, `isRunning=false`, PID пропал.
4. Перезапустить Control Center при живом агенте → статус по-прежнему `running`
   (pid-файл сработал).

### Фаза E · Зачистка (5 мин)
- Обновить MVP_PLAN.md (Шаг 4 ✅).

---

## 5. TODO (checklist)

### main.js
- [ ] `AGENT_PID_PATH` добавлен
- [ ] `agent:isRunning` (pid-файл + проверка живости через tasklist/kill 0)
- [ ] `agent:start` (spawn detached, windowsHide, unref, запись pid-файла)
- [ ] `agent:stop` (taskkill /F + удаление pid-файла)
- [ ] выбор команды: pythonw + main.py (dev) / DispatchAgent.exe (prod)

### preload.js
- [ ] `startAgent`, `stopAgent`, `isAgentRunning` в `api`

### renderer
- [ ] Кнопка «Запустить/Остановить» в Dashboard
- [ ] Строка статуса с PID
- [ ] Клик привязан (паттерн `dataset.bound`)
- [ ] `isAgentRunning()` включён в `refreshAll()`

### Проверка
- [ ] Старт из UI → `agent.log` обновляется
- [ ] Стоп из UI → процесс убит
- [ ] Рестарт CC при живом агенте → статус сохраняется (pid-файл)

### Зачистка
- [ ] MVP_PLAN.md обновлён (Шаг 4 ✅)

---

## 6. Готовность (приёмка)

Закрыто, когда **все** проходят:
1. В Dashboard есть кнопка; нажатие «Запустить» поднимает процесс агента
   (без консольного окна), `agent.log` начинает писаться.
2. Статус показывает `running=true` и реальный PID; `agent:status` (mtime) тоже
   переходит в `online`.
3. «Остановить» убивает процесс: `isRunning=false`, лог перестаёт обновляться.
4. Перезапуск Control Center при живом агенте → статус остаётся `running`
   (in-memory не теряется благодаря pid-файлу).

---

## 7. Файлы и команды

| Файл                                  | Зачем                               |
|---------------------------------------|-------------------------------------|
| `E:\dispatch\agent_os_app\main.js`    | добавить 3 IPC                      |
| `E:\dispatch\agent_os_app\preload.js` | пробросить методы                   |
| `E:\dispatch\agent_os_app\renderer\app.html` | кнопка + refreshAll (стр. 1154) |
| `E:\dispatch\main.py`                 | то, что запускаем (python entrypoint)|
| `E:\dispatch\agent.pid`               | новый файл — хранит PID агента      |

```powershell
# Запуск Control Center в dev-режиме
cd E:\dispatch\agent_os_app; npm start

# Проверить, жив ли процесс по PID (то, что делает isRunning)
tasklist /FI "PID eq <pid>"
```

---

## 8. Типичные грабли

| Симптом                                       | Причина                                  | Что делать                                              |
|-----------------------------------------------|------------------------------------------|---------------------------------------------------------|
| После рестарта CC статус «не запущен»          | PID хранится только в памяти             | использовать pid-файл (см. §3.3)                         |
| Старт открывает чёрное консольное окно         | `python.exe` вместо `pythonw.exe`        | spawn `pythonw`, `windowsHide:true`                      |
| `agent:stop` не убивает процесс                | детач + дерево процессов на Windows       | `taskkill /PID <pid> /T /F`                              |
| Запускается несколько копий агента             | start не проверяет isRunning перед spawn  | в начале start вернуть `already:true`, если жив          |
| `isRunning=true`, но агент мёртв (stale pid)    | процесс умер, pid-файл остался           | проверять живость, при мёртвом — чистить pid-файл        |
| Дочерний процесс умирает вместе с CC            | забыт `detached:true` + `child.unref()`  | добавить оба                                             |

---

## 9. После Шага 4 — что дальше

- **Шаг 5** — реальный автозапуск Windows (тоггл «Запускать при включении ПК»
  пишет в реестр). Кикофф: [NEXT_SESSION_STEP5.md](NEXT_SESSION_STEP5.md).
- Шаг 6 — все хендлеры end-to-end.
- Шаг 7 — сборка `.exe`.

---

## 10. Глоссарий

- **IPC** — обмен сообщениями renderer ⇄ main в Electron (`ipcMain.handle` / `ipcRenderer.invoke`).
- **preload** — мост, безопасно выставляющий `window.api.*` в renderer.
- **detached / unref** — процесс живёт независимо от родителя (Control Center).
- **pid-файл** — файл с PID запущенного агента; переживает перезапуск Control Center.
- **refreshAll** — функция в `app.html`, опрашивающая бэкенд раз в `LIVE_INTERVAL_MS`.
- **taskkill** — Windows-утилита завершения процесса по PID.
