# Следующая сессия · Шаг 5 MVP — реальный автозапуск Windows

> **Полный кикофф-документ.** Цель — чтобы тоггл «Запускать при включении ПК»
> не просто писал в `config.json`, а реально регистрировал автостарт в реестре
> Windows (ключ `HKCU\...\Run`). После перезагрузки агент должен подниматься сам.
>
> **Предусловие:** Шаги 1–4 закрыты. Желательно — Шаг 4 (агент запускается из CC),
> т.к. путь к запускаемому объекту тот же.

---

## 0. TL;DR (30 секунд)

Сейчас тоггл `#liveAutostart` в Control Center (`app.html:1208`) меняет **только**
`config.autostart` — на реальный автозапуск это не влияет. Нужно: добавить IPC
`autostart:set` / `autostart:get` в `main.js`, которые через `reg add` / `reg delete`
пишут/читают ключ `AgentOS` в `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`,
и привязать тоггл к ним.

**Главный нюанс:** автостартовать должен **агент** (`pythonw main.py` или
`DispatchAgent.exe`), а **не** сам Control Center. Не путать с
`app.setLoginItemSettings` (он автостартит Electron-приложение).

**Готово, когда:** тоггл ON → ключ в реестре появился; OFF → исчез; после reboot
агент сам поднялся (иконка в трее).

---

## 1. Контекст

### 1.1 Что сейчас делает тоггл

`app.html:1203-1218` — клик по `#liveAutostart`:
```js
r.data.autostart = next;
await window.api.saveConfig(r.data);   // пишет только config.json
```
Никакой записи в реестр. `config.autostart` — это просто флаг в JSON, который никто
не исполняет при загрузке системы.

### 1.2 Что должно происходить

Тоггл ON → в `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` появляется
строковый параметр `AgentOS` со значением — командой запуска агента.
Windows при входе пользователя выполнит её → агент стартует → иконка в трее.

### 1.3 Что автостартовать (важно!)

Цель — **Python-агент**, не Control Center:
- **dev:** `pythonw.exe "E:\dispatch\main.py"` (без консоли).
- **prod (Шаг 7):** `"...\DispatchAgent.exe"`.

> Не используй `app.setLoginItemSettings({openAtLogin:true})` — он зарегистрирует
> автозапуск **Electron Control Center**, а нам нужен агент. Поэтому — явный `reg`.

---

## 2. Промт для новой сессии (copy-paste)

```
Я работаю над проектом Dispatch (Windows-агент → Railway n8n → Telegram).
Корень — E:\dispatch.

Прочитай сперва:
1. E:\dispatch\MVP_PLAN.md
2. E:\dispatch\NEXT_SESSION_STEP5.md — ТЗ на сегодня.

Цель — Шаг 5: реальный автозапуск Windows.
Тоггл «Запускать при включении ПК» должен писать/удалять ключ AgentOS в
HKCU\Software\Microsoft\Windows\CurrentVersion\Run, а не только config.json.

Сделать:
1. agent_os_app/main.js: IPC autostart:set(bool) и autostart:get() через
   reg add / reg delete / reg query. Значение ключа — команда запуска АГЕНТА
   (pythonw + main.py в dev, DispatchAgent.exe в prod), НЕ Control Center.
2. agent_os_app/preload.js: setAutostart(bool), getAutostart().
3. app.html: в обработчике клика #liveAutostart (стр. ~1208) после saveConfig
   вызывать setAutostart(next); в refreshAll() читать реальное состояние через
   getAutostart() и синхронизировать вид тоггла.

ОГРАНИЧЕНИЯ:
- НЕ использовать app.setLoginItemSettings (он автостартит Electron, а нужен агент).
- Команды reg запускать через execFile с массивом аргументов (без shell-строки).
- config.autostart оставить как зеркало состояния, но источник правды — реестр.

Закрой по §6. Обнови MVP_PLAN.md (Шаг 5 ✅).
```

---

## 3. ТЗ (контракты)

### 3.1 Реестр — целевой ключ

```
Куст:   HKCU\Software\Microsoft\Windows\CurrentVersion\Run
Имя:    AgentOS
Тип:    REG_SZ
Данные: "C:\...\pythonw.exe" "E:\dispatch\main.py"      (dev)
        "E:\dispatch\dist\DispatchAgent.exe"            (prod)
```

`HKCU` (не `HKLM`) — не требует прав администратора.

### 3.2 `autostart:set`

```js
ipcMain.handle('autostart:set', async (_evt, enabled) => {
  const RUN_KEY = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';
  const NAME = 'AgentOS';
  if (enabled) {
    const cmd = await buildAgentLaunchCommand(); // см. §3.5
    // reg add <RUN_KEY> /v AgentOS /t REG_SZ /d "<cmd>" /f
    return execReg(['add', RUN_KEY, '/v', NAME, '/t', 'REG_SZ', '/d', cmd, '/f']);
  } else {
    // reg delete <RUN_KEY> /v AgentOS /f
    return execReg(['delete', RUN_KEY, '/v', NAME, '/f']);
  }
});
```

Возврат: `{ ok: bool, error? }`. Запускать через `execFile('reg', args, {windowsHide:true})`.

### 3.3 `autostart:get`

```js
ipcMain.handle('autostart:get', async () => {
  // reg query <RUN_KEY> /v AgentOS
  // exit code 0 + строка с AgentOS → enabled:true; иначе false
  return { ok: true, enabled: bool, value };
});
```

### 3.4 `preload.js`

```js
setAutostart: (b) => ipcRenderer.invoke('autostart:set', b),
getAutostart: ()  => ipcRenderer.invoke('autostart:get'),
```

### 3.5 Построение команды запуска (`buildAgentLaunchCommand`)

Порядок:
1. Если в `config.json` есть поле `autostart_target` — взять его (явный путь к exe).
2. Иначе prod: если рядом есть `dist/DispatchAgent.exe` — вернуть путь к нему в кавычках.
3. Иначе dev: найти `pythonw.exe` (рядом с `python` из PATH) и вернуть
   `"<pythonw>" "<root>/main.py"`.

> Корень = `path.resolve(__dirname, '..')`. Кавычки вокруг каждого пути обязательны —
> в путях бывают пробелы. `reg add /d` принимает значение одной строкой.

### 3.6 Renderer — привязка к тогглу

В обработчике клика `#liveAutostart` (`app.html:1208`) **после** `saveConfig`:
```js
const a = await window.api.setAutostart(next);
showToast(a.ok ? (next ? 'Автозапуск включён' : 'Автозапуск выключен')
                : 'Не удалось изменить автозапуск');
```
В `refreshAll()` (`app.html:1203`) вместо доверия `config.autostart` —
прочитать реальное состояние:
```js
const real = await window.api.getAutostart();
as.classList.toggle('on', !!(real.ok && real.enabled));
```
> Источник правды — реестр, `config.autostart` — лишь зеркало (можно синхронизировать).

### 3.7 Что НЕ трогать

- ✋ Существующие IPC и дизайн.
- ✋ Не использовать `app.setLoginItemSettings` (автостартит Electron, не агента).
- ✋ Не писать в `HKLM` (требует админ-прав).

---

## 4. План работы

### Фаза A · main.js (30 мин)
1. Прочитать `agent_os_app/main.js`.
2. Написать helper `execReg(args)` (обёртка `execFile('reg', ...)` → `{ok,error}`).
3. Написать `buildAgentLaunchCommand()` (§3.5).
4. Реализовать `autostart:set` и `autostart:get`.

### Фаза B · preload.js (5 мин)
1. Добавить `setAutostart`, `getAutostart`.

### Фаза C · renderer (20 мин)
1. В клике `#liveAutostart` вызвать `setAutostart(next)`.
2. В `refreshAll()` синхронизировать вид тоггла через `getAutostart()`.

### Фаза D · Проверка (20 мин)
1. `cd agent_os_app; npm start`.
2. Включить тоггл → проверить реестр:
   ```powershell
   reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AgentOS
   ```
   Должна быть строка с командой запуска агента.
3. Выключить тоггл → `reg query` отдаёт «не найдено».
4. **Reboot-тест:** включить тоггл, перезагрузить ПК, войти → агент сам поднялся
   (иконка в трее, `agent.log` обновляется).

### Фаза E · Зачистка (5 мин)
- Обновить MVP_PLAN.md (Шаг 5 ✅).

---

## 5. TODO (checklist)

### main.js
- [ ] `execReg(args)` helper
- [ ] `buildAgentLaunchCommand()` (autostart_target → exe → pythonw+main.py)
- [ ] `autostart:set` (reg add / reg delete)
- [ ] `autostart:get` (reg query → enabled)

### preload.js
- [ ] `setAutostart`, `getAutostart`

### renderer
- [ ] Клик `#liveAutostart` вызывает `setAutostart(next)`
- [ ] `refreshAll()` читает реальное состояние через `getAutostart()`

### Проверка
- [ ] ON → ключ `AgentOS` в реестре есть
- [ ] OFF → ключ удалён
- [ ] Reboot → агент сам стартует

### Зачистка
- [ ] MVP_PLAN.md обновлён (Шаг 5 ✅)

---

## 6. Готовность (приёмка)

Закрыто, когда **все** проходят:
1. Тоггл ON → `reg query "HKCU\...\Run" /v AgentOS` показывает команду запуска агента.
2. Тоггл OFF → тот же `reg query` отдаёт «The system was unable to find...» (ключа нет).
3. Вид тоггла в UI соответствует реальному состоянию реестра (после рестарта CC тоже).
4. **Reboot-тест:** при включённом автозапуске после перезагрузки агент поднимается
   сам — иконка в трее, `agent.log` пишется без ручного запуска.

---

## 7. Файлы и команды

| Файл                                  | Зачем                          |
|---------------------------------------|--------------------------------|
| `E:\dispatch\agent_os_app\main.js`    | IPC autostart:set/get          |
| `E:\dispatch\agent_os_app\preload.js` | пробросить методы              |
| `E:\dispatch\agent_os_app\renderer\app.html` | тоггл #liveAutostart (1208) |
| `E:\dispatch\config.json`             | поле `autostart` (зеркало), опц. `autostart_target` |

```powershell
# Посмотреть ключ автозапуска
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AgentOS

# Удалить вручную (если надо сбросить)
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AgentOS /f

# Найти pythonw
(Get-Command pythonw).Source
```

---

## 8. Типичные грабли

| Симптом                                        | Причина                                  | Что делать                                          |
|------------------------------------------------|------------------------------------------|-----------------------------------------------------|
| После reboot стартует Control Center, не агент | использован `app.setLoginItemSettings`   | вернуть `reg` с командой запуска агента              |
| После reboot ничего не стартует                | в значении нет кавычек / путь с пробелами | обернуть каждый путь в `"..."`                       |
| Стартует, но с чёрной консолью                 | `python.exe` вместо `pythonw.exe`        | в команде использовать `pythonw.exe`                |
| `reg add` падает с отказом доступа             | писали в `HKLM`                          | использовать `HKCU` (без админ-прав)                |
| Тоггл «прыгает» обратно после клика            | refreshAll перетирает из `config`, не реестра | читать `getAutostart()` в refreshAll            |
| `reg` не найден                                | необычный PATH                           | вызвать полный путь `C:\Windows\System32\reg.exe`   |

---

## 9. После Шага 5 — что дальше

- **Шаг 6** — все хендлеры end-to-end из Telegram. Кикофф: [NEXT_SESSION_STEP6.md](NEXT_SESSION_STEP6.md).
- Шаг 7 — сборка `.exe`. На Шаге 7 `autostart_target` стоит выставить на собранный
  `DispatchAgent.exe`, чтобы автозапуск указывал на exe, а не на dev-питон.

---

## 10. Глоссарий

- **HKCU** — `HKEY_CURRENT_USER`, ветка реестра текущего пользователя (без админ-прав).
- **Run-ключ** — `...\CurrentVersion\Run`: что Windows запускает при входе пользователя.
- **REG_SZ** — строковый тип значения реестра.
- **reg add/delete/query** — встроенная утилита Windows для работы с реестром.
- **autostart_target** — опциональное поле в `config.json` с явным путём для автозапуска.
- **pythonw.exe** — Python без консольного окна.
