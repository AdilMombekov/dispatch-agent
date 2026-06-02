# TEST_PLAN — гипотезы, метрики, короткие промты

> Для каждой подсистемы: **Г** (гипотеза), **М** (метрика), **П** (промт — copy-paste).
> Если что-то падает — это баг. Если метрика не сошлась — это баг.
> Промты: PowerShell от `E:\dispatch\`, либо DevTools-консоль Electron (Ctrl+Shift+I), либо клик в UI.

---

## 60-секундный smoke

| # | Что | Как |
|---|---|---|
| 1 | Electron поднимается | `cd agent_os_app; npm start` → окно открылось без stack-trace |
| 2 | Все 12 вкладок есть | сайдбар: Обзор · Чаты · API · MCP · Креды · Каналы · Скиллы · **Воркфлоу** · События · Хранилище · Файлы · **Бэкап** |
| 3 | Реальные данные грузятся | Дашборд → CPU/RAM/Диск не «—», Каналы → 4 badge зелёные с ms |
| 4 | Воркфлоу рисует 34 узла | клик на «Воркфлоу n8n» → канвас с цветными карточками |
| 5 | Бэкап-вкладка живая | клик «Бэкап» → плитка «Простой» показывает секунды |

Всё мимо — стопаем, идём в детали ниже.

---

## A · Boot & IPC

### A1 · Electron стартует чисто
**Г**: `npm start` грузит main.js без exceptions.
**М**: в stdout нет `Error:` / `TypeError` / `Cannot find module`.
**П**:
```powershell
cd E:\dispatch\agent_os_app; npm start
```

### A2 · 35 IPC зарегистрированы
**Г**: все `ipcMain.handle` в `main.js` доступны через `window.api.*`.
**М**: в DevTools `Object.keys(window.api).length` ≥ 25 (методов в preload).
**П** (DevTools):
```js
Object.keys(window.api).length
```

### A3 · preload изолирован
**Г**: `window.require` недоступен (contextIsolation работает).
**М**: `typeof window.require === 'undefined'`
**П** (DevTools):
```js
typeof require + ' / ' + typeof window.require
```

### A4 · Single-instance lock
**Г**: запуск второго `npm start` пробуждает существующее окно, не плодит дубль.
**М**: процесс-список содержит **один** electron.exe (top-level).
**П**:
```powershell
(Get-Process electron -ErrorAction SilentlyContinue | Where-Object {$_.MainWindowTitle}).Count
```

---

## B · Реальные данные из файлов

### B1 · config.json читается
**Г**: `getConfig()` отдаёт `data.telegram_bot_token`, `agent_token`, `owner_chat_id`.
**М**: все три поля непустые.
**П** (DevTools):
```js
(await window.api.getConfig()).data && {tg:!!d.telegram_bot_token,ag:!!d.agent_token,own:d.owner_chat_id}
```
(где `d = (await window.api.getConfig()).data`)

### B2 · agent_state.json читается
**Г**: `readState()` отдаёт `spend`, `active_account`.
**М**: `spend.total_usd ≥ 0`, `active_account` — число.
**П** (DevTools):
```js
(await window.api.readState()).data
```

### B3 · chats.json показывается
**Г**: `readChats()` отдаёт ≥1 чат после первого диалога.
**М**: `data.chats.length ≥ 1`, у каждого есть `chat_id` и `messages`.
**П** (DevTools):
```js
(await window.api.readChats()).data.chats.map(c=>({id:c.chat_id,msgs:c.messages.length}))
```

### B4 · agent.log парсится в события
**Г**: `recentEvents(80)` отдаёт массив объектов `{time, level, message, plain, severity}`.
**М**: `events.length > 0`, в первом есть все 5 полей.
**П** (DevTools):
```js
(await window.api.recentEvents(80)).events[0]
```

### B5 · inbox/ показывает файлы
**Г**: `listInbox()` находит реальный photo_*.jpg.
**М**: `files.length ≥ 1`, у файла есть `size` > 0.
**П** (DevTools):
```js
(await window.api.listInbox()).files.slice(0,3)
```

---

## C · Метрики хоста

### C1 · CPU отдаёт реальный %
**Г**: `metrics()` возвращает `cpu.percent` в `[0, 100]`.
**М**: два вызова с разницей 2 с — значения **разные** (живые данные).
**П** (DevTools):
```js
const a = (await window.api.metrics()).cpu.percent;
await new Promise(r=>setTimeout(r,2000));
const b = (await window.api.metrics()).cpu.percent;
({a, b, alive: a !== b})
```

### C2 · RAM сошлась с psutil
**Г**: `metrics().ram.total` соответствует системной памяти.
**М**: total ≈ `Get-CimInstance Win32_ComputerSystem | % TotalPhysicalMemory`.
**П**:
```powershell
[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
# DevTools: ((await window.api.metrics()).ram.total / 1024**3).toFixed(1)
```

### C3 · Диск C: реален
**Г**: `metrics().disk.total > 0`, `free < total`.
**М**: free ≈ результат `dir C:\` PowerShell.
**П** (DevTools):
```js
const d = (await window.api.metrics()).disk;
({drive:d.drive, totalGB:Math.round(d.total/1024**3), freeGB:Math.round(d.free/1024**3)})
```

---

## D · Process control

### D1 · agent:process находит Python
**Г**: если запущен `python main.py`, `agentProcess()` возвращает `{running:true, pid, uptime_sec}`.
**М**: `pid` совпадает с `(Get-Process python).Id`.
**П**:
```powershell
Start-Process pythonw -ArgumentList 'E:\dispatch\main.py' -WindowStyle Hidden
# DevTools: (await window.api.agentProcess())
# Cleanup: Get-Process pythonw | Stop-Process -Force
```

### D2 · agent:restart перезапускает
**Г**: `restartAgent()` убивает старый PID и стартует новый.
**М**: новый `pid` ≠ старый, оба валидны.
**П** (DevTools):
```js
const a = await window.api.agentProcess();
await window.api.restartAgent();
await new Promise(r=>setTimeout(r,2000));
const b = await window.api.agentProcess();
({old:a.pid, new:b.pid, changed: a.pid !== b.pid})
```

---

## E · Воркфлоу

### E1 · workflow:read отдаёт 34 узла
**Г**: парсер workflow_fixed.json возвращает все узлы и связи.
**М**: `nodeCount = 34`, `edgeCount > 30`.
**П** (DevTools):
```js
const r = await window.api.getWorkflow();
({nodes:r.nodeCount, edges:r.edgeCount, ok:r.ok})
```

### E2 · Канвас рисует все узлы
**Г**: клик «Воркфлоу n8n» → 34 div'а с классом `.wf-node`.
**М**: `document.querySelectorAll('.wf-node').length === 34`.
**П** (DevTools после клика):
```js
document.querySelectorAll('.wf-node').length
```

### E3 · Клик по узлу открывает панель с кодом
**Г**: клик на узел типа `code` показывает `<pre class="code">` с JS-телом.
**М**: после клика на «Poll Handler» → `#wfSide.aria-hidden === 'false'`, в `#wfSideBody` есть `pre`.
**П** (DevTools):
```js
document.querySelector('.wf-node[data-name="Poll Handler"]').click();
({open: document.getElementById('wfSide').getAttribute('aria-hidden'),
  hasCode: !!document.querySelector('#wfSideBody pre')})
```

---

## F · Backup daemon (CLI)

### F1 · `status` запускается, idle живой
**Г**: `python -m agent.backup_daemon status` показывает idle, whitelist, blacklist, отсутствие токена.
**М**: вывод содержит `Idle:`, `Whitelist (5):`, `Drive token: ... (missing)`.
**П**:
```powershell
$env:PYTHONIOENCODING='utf-8'; python -m agent.backup_daemon status
```

### F2 · `scan` находит файлы Documents/Desktop
**Г**: сканер обходит whitelist, blacklist отрабатывает.
**М**: вывод вида `Scanned N files (X MB)`, N > 50, blacklist'нутых нет в первых 100.
**П**:
```powershell
python -m agent.backup_daemon scan
Get-Content E:\dispatch\backup_index.json -Raw | ConvertFrom-Json | % { $_.PSObject.Properties.Name.Count }
```

### F3 · Blacklist пресекает node_modules
**Г**: после `scan` в `backup_index.json` нет путей с `node_modules`.
**М**: `Select-String node_modules backup_index.json` → пусто.
**П**:
```powershell
(Get-Content E:\dispatch\backup_index.json -Raw) -match 'node_modules'  # должно быть False
```

### F4 · `dry-run` показывает diff
**Г**: после первого scan → второй `dry-run` показывает `new: 0`, `unchanged: N`.
**М**: вывод содержит `new: 0`.
**П**:
```powershell
python -m agent.backup_daemon dry-run | Select-String 'new:|changed:|unchanged:'
```

### F5 · Idle-детект > 0 когда не двигаешь мышью
**Г**: после 10 с без активности idle ≥ 10.
**М**: `idle_seconds()` растёт между вызовами.
**П**:
```powershell
python -c "from agent.backup_daemon import idle_seconds; import time; print(idle_seconds()); time.sleep(10); print(idle_seconds())"
```

---

## G · Backup UI (Electron)

### G1 · Tab «Бэкап» открывается, 3 плитки живые
**Г**: клик «Бэкап» → плитка «Простой» обновляется с реальным idle.
**М**: `bkIdle` текст содержит число + ' с'.
**П** (DevTools после клика):
```js
document.getElementById('bkIdle').textContent
```

### G2 · Кнопка «Сканировать» работает
**Г**: клик → toast «Индекс обновлён», `backup_index.json` mtime изменился.
**М**: до клика и после — разные mtime файла.
**П**:
```powershell
$a=(Get-Item E:\dispatch\backup_index.json -ErrorAction SilentlyContinue).LastWriteTime; "before: $a"
# клик «Сканировать» в UI
$b=(Get-Item E:\dispatch\backup_index.json).LastWriteTime; "after: $b · diff: $($b -ne $a)"
```

### G3 · «Сравнить» парсит summary
**Г**: клик → таблица с N новых файлов, тайтл `bkResultCap` обновляется.
**М**: `bkResultCap.textContent` содержит `новых`.
**П** (DevTools после клика):
```js
document.getElementById('bkResultCap').textContent
```

### G4 · Авто-режим переключается
**Г**: клик на switch → spawn python, `serveStatus.running === true`, pid реальный.
**М**: после клика DevTools→`(await window.api.backupServeStatus()).running === true`, `pid` валиден.
**П** (DevTools):
```js
// сначала клик на #bkServeSwitch
await new Promise(r=>setTimeout(r,2000));
await window.api.backupServeStatus()
// потом снова клик чтобы выключить
```

### G5 · Progress bar парсит [N/M]
**Г**: при upload’е каждая строка `[12/361] new: ...` двигает `#bkProgressFill`.
**М**: `progFill.style.width` растёт по мере загрузки.
**П**: запустить upload (нужен OAuth) и проверить width вручную.

---

## H · Каналы связи (live ping)

### H1 · 4 канала пингуются за 30 с
**Г**: клик «Каналы связи» → 4 badge становятся `b-ok` с числом ms.
**М**: `document.querySelectorAll('[data-channel-status]')` — все 4 с классом `b-ok` или `b-warn`.
**П** (DevTools после открытия вкладки + ожидание 2с):
```js
[...document.querySelectorAll('[data-channel-status]')].map(b=>b.className+' · '+b.textContent.trim())
```

### H2 · Тайл «Каналов в сети» считает правильно
**Г**: `liveChannelsOk = liveChannelsTotal` когда все онлайн.
**М**: `4 / 4`.
**П** (DevTools):
```js
({ok:document.getElementById('liveChannelsOk').textContent,
  total:document.getElementById('liveChannelsTotal').textContent})
```

### H3 · Без интернета — badges краснеют
**Г**: при `webhookHealth` с timeout/error — badge `b-err` или `b-warn`.
**М**: после `New-NetFirewallRule -Action Block` (не делать!) badges должны окраситься в красный.
**П**: просто отключить Wi-Fi и обновить вкладку. Не более 30 с — badges должны стать жёлто-красные.

---

## I · Storage tab

### I1 · 14+ файлов в 3 группах
**Г**: `storageList()` возвращает items с `group` ∈ {Агент, Бэкап, Пользователь}.
**М**: 9 в группе «Агент», 4 в «Бэкап», 0-1 в «Пользователь».
**П** (DevTools):
```js
const r = await window.api.storageList();
const g = {};
r.items.forEach(it => g[it.group] = (g[it.group]||0)+1);
g
```

### I2 · Клик по строке открывает файл
**Г**: клик на row с `data-storage-path` → `shell.openPath()` запускает системный просмотрщик.
**М**: notepad/проводник появляется.
**П**: клик на config.json в UI → должен открыться системный редактор.

### I3 · Несуществующие файлы помечены как «не создан»
**Г**: `drive_credentials.json` (если OAuth не настроен) показывается opacity 0.55.
**М**: в HTML строки с `data-exists="0"` имеют `opacity:.55`.
**П** (DevTools):
```js
[...document.querySelectorAll('tr[data-exists="0"]')].length
```

---

## J · Автозапуск Windows

### J1 · `autostartGet` отдаёт honest state
**Г**: возвращает `enabled` соответствующее реальному реестру.
**М**: согласовано с `reg query`.
**П**:
```powershell
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AgentOS 2>&1 | Select-String 'REG_SZ|find'
# DevTools: (await window.api.autostartGet()).enabled
```

### J2 · Toggle реально пишет в реестр
**Г**: клик на switch → новая строка в `HKCU\…\Run`.
**М**: после клика `reg query` находит `AgentOS`.
**П**:
```powershell
# До клика:
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AgentOS 2>&1
# Клик на toggle «Запускать при включении ПК» в Credentials tab
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AgentOS
```

### J3 · Второй клик удаляет
**Г**: повторный клик → `reg delete`.
**М**: `reg query` снова возвращает «не найдено».
**П**: повторить J2, второй клик → проверить query.

---

## K · UI behaviour

### K1 · Хэш-роутинг работает
**Г**: переход на `#backup` → активна `p-backup`.
**М**: `document.querySelector('.page.active').id === 'p-backup'`.
**П** (DevTools):
```js
location.hash = '#workflow';
await new Promise(r=>setTimeout(r,100));
document.querySelector('.page.active').id
```

### K2 · Стрелки клавиатуры переключают вкладки
**Г**: ArrowDown → следующая вкладка по `ORDER`.
**М**: после нажатия hash меняется.
**П** (DevTools):
```js
const before = location.hash;
window.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowDown'}));
({before, after: location.hash, changed: before !== location.hash})
```

### K3 · Theme popover работает
**Г**: клик на segmented popover → меню открывается, выбор меняет `data-theme`.
**М**: `document.documentElement.dataset.theme` меняется.
**П**: клик на сегментированный контрол в правом верхнем углу, выбрать Dark.

### K4 · Sidebar search фильтрует
**Г**: ввод в поиске → ненужные nav-items скрываются.
**М**: после ввода «бэк» — видна только вкладка «Бэкап».
**П** (DevTools):
```js
const inp = document.getElementById('search');
inp.value = 'бэк'; inp.dispatchEvent(new Event('input'));
[...document.querySelectorAll('.nav-item:not([style*="none"]) .lbl')].map(l=>l.textContent)
```

---

## L · Negative / graceful

### L1 · Без `window.api` (браузер) — не падает
**Г**: запуск `app.html` через `python -m http.server` → нет красных ошибок в консоли.
**М**: 0 ошибок consol.
**П**:
```powershell
# Открыть http://127.0.0.1:8001/app.html в Chrome, F12 → Console — должно быть пусто
```

### L2 · `config.json.bak` создаётся при saveConfig
**Г**: каждая запись делает копию.
**М**: mtime `config.json.bak` обновляется.
**П**:
```powershell
$a=(Get-Item E:\dispatch\config.json.bak -ErrorAction SilentlyContinue).LastWriteTime
# Изменить любое поле через UI (например клик на toggle autostart)
$b=(Get-Item E:\dispatch\config.json.bak).LastWriteTime
"$a → $b · diff: $($b -gt $a)"
```

### L3 · `backup:uploadStart` отказывает при повторе
**Г**: если upload уже идёт, второй вызов → `{ok:false, error:'upload already running'}`.
**М**: response.error === 'upload already running'.
**П** (DevTools, во время активного upload):
```js
await window.api.backupUpload()  // должен вернуть {ok:false, error:'upload already running'}
```

---

## M · Кросс-функциональные

### M1 · owner_chat_id заблокировал доступ
**Г**: в `config.json.owner_chat_id` — реальный chat_id из chats.json.
**М**: значение `8761312498`.
**П**:
```powershell
(Get-Content E:\dispatch\config.json -Raw | ConvertFrom-Json).owner_chat_id
```

### M2 · Skills toggle сохраняется
**Г**: снять галочку с «Снимок экрана» → `config.json.skill_disabled` содержит `screenshot`.
**М**: после клика `(await getConfig()).data.skill_disabled.includes('screenshot') === true`.
**П** (DevTools, после клика на checkbox):
```js
(await window.api.getConfig()).data.skill_disabled
```

### M3 · Live obsoleted после minute idle UI
**Г**: если UI закрыт > 30с — следующий refresh не падает, тянет свежие данные.
**М**: 0 errors после "wake-up".
**П**: минимизировать окно на 1 минуту, развернуть. Smoke logs: 0 ошибок.

---

## Чек-лист итога

После прогона: каждый тест либо ✓ либо ✗. Все ✓ = система здорова.

```
A1 ☐  A2 ☐  A3 ☐  A4 ☐
B1 ☐  B2 ☐  B3 ☐  B4 ☐  B5 ☐
C1 ☐  C2 ☐  C3 ☐
D1 ☐  D2 ☐
E1 ☐  E2 ☐  E3 ☐
F1 ☐  F2 ☐  F3 ☐  F4 ☐  F5 ☐
G1 ☐  G2 ☐  G3 ☐  G4 ☐  G5 ☐(требует OAuth)
H1 ☐  H2 ☐  H3 ☐(требует выкл. сети)
I1 ☐  I2 ☐  I3 ☐
J1 ☐  J2 ☐  J3 ☐
K1 ☐  K2 ☐  K3 ☐  K4 ☐
L1 ☐  L2 ☐  L3 ☐
M1 ☐  M2 ☐  M3 ☐
```

**Всего**: 39 тестов · ~30 минут прогона если ничего не падает.

Главные блокеры (если ✗): A1, A2, B1, J2.
Косметика (если ✗ — не страшно): K2, K3, M3.
