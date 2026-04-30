"""Update system prompt in Anthropic Full and Process Result nodes."""
import urllib.request, json, http.cookiejar, time

NEW_SYSTEM = (
    "Ты — Dispatch Agent, облачный ИИ-оркестратор, управляющий локальным Windows ПК пользователя "
    "через асинхронный шлюз. Твоя задача — выполнять поручения пользователя, автоматизировать рутину, "
    "писать код и управлять файловой системой, используя предоставленные инструменты (Tools).\n\n"
    "ТВОИ ИНСТРУМЕНТЫ (TOOLS)\n\n"
    "У тебя есть доступ к локальному ПК через специальные функции. Всегда используй их вместо того, "
    "чтобы просто отвечать текстом, если задача требует действий на ПК:\n\n"
    "terminal: Выполнение команд PowerShell/CMD.\n"
    "obsidian-read / obsidian-context: Чтение базы знаний и логов.\n"
    "obsidian-log: Запись отчетов и логов.\n"
    "screenshot: Получение снимка экрана для анализа текущего состояния.\n\n"
    "КРИТИЧЕСКИЕ ПРАВИЛА БЕЗОПАСНОСТИ И СТАБИЛЬНОСТИ\n\n"
    "ЗАПРЕТ НА ИНТЕРАКТИВНОСТЬ: Твой терминал работает в слепом (headless) режиме. "
    "Ты НЕ МОЖЕШЬ нажимать Y или Enter в ответ на запросы консоли. "
    "Всегда используй флаги авто-подтверждения: -y, --yes, --force, Force-Item и т.д. "
    "Избегай команд вроде pause или скриптов, ожидающих input().\n\n"
    "ЛИМИТ ВЫВОДА: Если ты читаешь большой файл через терминал (например, логи), всегда используй "
    "tail, head или Select-String (grep), чтобы не перегрузить контекстное окно.\n\n"
    "КОДИРОВКИ: Для Windows терминала предполагай кодировку UTF-8 "
    "(при необходимости используй chcp 65001).\n\n"
    "ПАТТЕРНЫ РАБОТЫ И ДЕЛЕГИРОВАНИЕ\n\n"
    "Ты — Оркестратор. Если задача слишком объемная, делегируй её локальному ИИ. "
    "У пользователя установлен claude-code (консольная утилита Anthropic).\n\n"
    "Чтобы запустить локальный ИИ, используй инструмент terminal с командой:\n"
    "cd путь\\к\\проекту && claude -p \"Твой подробный промпт для локального агента\"\n\n"
    "(Флаг -p означает print/non-interactive mode, что защищает от зависаний).\n\n"
    "АЛГОРИТМ ДЕЙСТВИЙ\n\n"
    "1. Пойми задачу: Что нужно пользователю? Нужен ли доступ к ПК?\n"
    "2. Спланируй: Вызови нужные инструменты последовательно.\n"
    "3. Проверь результат: Убедись что команда вернула код 0. Если ошибка — исправь и повтори.\n"
    "4. Задокументируй: Важные действия сохрани через obsidian-log.\n"
    "5. Отчитайся: Кратко сообщи пользователю что сделано. Не дублируй большие логи в чат.\n\n"
    "СТИЛЬ ОБЩЕНИЯ\n\n"
    "Общайся на русском.\n"
    "Будь краток, профессионален и проактивен. Не спрашивай разрешения на безопасные действия.\n"
    "Если действие опасно (удаление файлов, остановка служб) — сначала запроси подтверждение."
)

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
login_data = json.dumps({'emailOrLdapLoginId': 'adil.mombekov97@gmail.com', 'password': 'Dispatch2025!'}).encode()
opener.open(urllib.request.Request(
    'https://n8n-production-419d8.up.railway.app/rest/login',
    data=login_data, headers={'Content-Type': 'application/json'}
))

resp = opener.open('https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J')
wf = json.loads(resp.read()).get('data', {})

TARGET_NODES = ('Anthropic Full', 'Process Result')
updated = []

for node in wf.get('nodes', []):
    if node['name'] not in TARGET_NODES:
        continue
    code = node['parameters'].get('jsCode', '')

    # Find:  system: 'old text',
    # The system value is a JS single-quoted string on one logical line
    # Strategy: find "  system: " then extract to next unescaped quote + comma
    marker = "  system: '"
    start = code.find(marker)
    if start == -1:
        # Try double-quote variant
        marker = '  system: "'
        start = code.find(marker)
    if start == -1:
        print(f"  {node['name']}: marker not found")
        idx = code.find('system')
        print("  context:", repr(code[max(0, idx-5):idx+60]))
        continue

    quote_char = marker[-1]  # ' or "
    search_from = start + len(marker)
    end = -1
    i = search_from
    while i < len(code):
        if code[i] == quote_char and (i == 0 or code[i-1] != chr(92)):  # chr(92) = backslash
            end = i + 1  # include the closing quote
            break
        i += 1

    if end == -1:
        print(f"  {node['name']}: could not find closing quote")
        continue

    old_snippet = code[start:end]
    new_snippet = "  system: " + json.dumps(NEW_SYSTEM, ensure_ascii=False)
    node['parameters']['jsCode'] = code[:start] + new_snippet + code[end:]
    updated.append(node['name'])
    print(f"  {node['name']}: updated ({len(old_snippet)} -> {len(new_snippet)} chars)")

print(f"\nUpdated nodes: {updated}")

# Deactivate
opener.open(urllib.request.Request(
    'https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J',
    data=json.dumps({'active': False}).encode(),
    headers={'Content-Type': 'application/json'}, method='PATCH'
))
time.sleep(2)

# PATCH
wf_data = json.dumps(wf).encode()
resp2 = opener.open(urllib.request.Request(
    'https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J',
    data=wf_data, headers={'Content-Type': 'application/json'}, method='PATCH'
))
result = json.loads(resp2.read())
print('PATCH:', resp2.status, '|', result.get('data', {}).get('updatedAt'))

# Activate
time.sleep(1)
wf2 = json.loads(
    opener.open('https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J').read()
).get('data', {})
vid = wf2.get('versionId')
r_a = json.loads(opener.open(urllib.request.Request(
    'https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J/activate',
    data=json.dumps({'versionId': vid}).encode(),
    headers={'Content-Type': 'application/json'}, method='POST'
)).read())
print('Active:', r_a.get('data', {}).get('active'))
