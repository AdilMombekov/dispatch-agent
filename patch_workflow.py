"""Patch n8n workflow: add Tool Calling to Anthropic Full + smart Process Result."""
import urllib.request, json, http.cookiejar, time

API_KEY_1 = 'ANTHROPIC_KEY_REDACTED'
API_KEY_2 = 'ANTHROPIC_KEY_REDACTED'
AGENT_TOKEN = '3bafc0aa-bb42-4e01-b918-8c5a8a252a43'

ANTHROPIC_FULL_CODE = """
const https = require('https');
const { chatId, messages, activeAccount } = $input.first().json;

const apiKey = [
  '%s',
  '%s',
  ''
][(activeAccount || 1) - 1] || '%s';

const TOOLS = [{
  name: 'queue_agent_command',
  description: 'Queue a command for the local Windows PC agent. Use this to take screenshots, run terminal commands, manage files, read/write Obsidian notes, or get system info.',
  input_schema: {
    type: 'object',
    properties: {
      type: {
        type: 'string',
        enum: ['screenshot','terminal','obsidian-log','obsidian-read','system-info','launch-app','install'],
        description: 'Command type'
      },
      payload: {
        type: 'object',
        description: 'Command parameters',
        properties: {
          command:  { type: 'string', description: 'terminal: shell command to execute' },
          text:     { type: 'string', description: 'obsidian-log: text to append' },
          lines:    { type: 'number', description: 'obsidian-read: number of lines (default 20)' },
          note:     { type: 'string', description: 'obsidian-log/read: note name override' },
          app:      { type: 'string', description: 'launch-app: app name from config' },
          url:      { type: 'string', description: 'install: download URL' }
        }
      }
    },
    required: ['type']
  }
}];

const reqBody = JSON.stringify({
  model: 'claude-haiku-4-5-20251001',
  max_tokens: 1024,
  system: 'You are a helpful assistant controlling a Windows PC remotely via Telegram. Be concise. Use the queue_agent_command tool to perform actions on the PC when the user asks.',
  messages,
  tools: TOOLS,
  tool_choice: { type: 'auto' }
});

const apiResp = await new Promise((resolve, reject) => {
  const req = https.request({
    hostname: 'api.anthropic.com', path: '/v1/messages', method: 'POST',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(reqBody)
    }
  }, res => {
    const chunks = [];
    res.on('data', c => chunks.push(c));
    res.on('end', () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString())); } catch(e) { reject(e); } });
    res.on('error', reject);
  });
  req.on('error', reject);
  req.write(reqBody); req.end();
});

const sd = $getWorkflowStaticData('global');
if (!sd.spend)      sd.spend      = { total_usd: 0, limit_usd: 5, anthropic_usd: 0, openai_usd: 0 };
if (!sd.history)    sd.history    = {};
if (!sd.tool_calls) sd.tool_calls = {};

// GC: clean stale tool_calls (> 5 min)
const now = Date.now();
const TIMEOUT_MS = 5 * 60 * 1000;
const timedOutChats = [];
for (const [cmdId, ctx] of Object.entries(sd.tool_calls)) {
  if (now - ctx.ts > TIMEOUT_MS) {
    timedOutChats.push(ctx.chat_id);
    delete sd.tool_calls[cmdId];
  }
}

// Track cost
const usage = apiResp.usage || { input_tokens: 0, output_tokens: 0 };
const cost = (usage.input_tokens / 1_000_000 * 0.80) + (usage.output_tokens / 1_000_000 * 4.00);
sd.spend.anthropic_usd = (sd.spend.anthropic_usd || 0) + cost;
sd.spend.total_usd     = (sd.spend.total_usd || 0) + cost;

// Parse content blocks
let displayText  = '';
let toolUseBlock = null;
for (const block of (apiResp.content || [])) {
  if (block.type === 'text')     displayText  += block.text;
  if (block.type === 'tool_use') toolUseBlock  = block;
}

let commandId = null;
if (toolUseBlock) {
  commandId = toolUseBlock.id;

  // Snapshot history before appending tool_use
  const historySnapshot = [...(sd.history[chatId] || [])];

  // Store context for execution #2
  sd.tool_calls[commandId] = {
    tool_use_id:       toolUseBlock.id,
    tool_name:         toolUseBlock.name,
    tool_input:        toolUseBlock.input,
    chat_id:           chatId,
    messages_snapshot: historySnapshot,
    ts:                now
  };

  // Queue command for local agent
  if (!sd.command_queue) sd.command_queue = [];
  if (sd.command_queue.length >= 50) sd.command_queue.shift();
  sd.command_queue.push({
    command_id: commandId,
    chat_id:    chatId,
    type:       toolUseBlock.input.type,
    payload:    toolUseBlock.input.payload || {}
  });

  // Save assistant message (full content array with tool_use) to history
  if (!sd.history[chatId]) sd.history[chatId] = [];
  sd.history[chatId].push({ role: 'assistant', content: apiResp.content });
  if (sd.history[chatId].length > 30) sd.history[chatId] = sd.history[chatId].slice(-30);

} else {
  // Plain text — save normally
  if (!sd.history[chatId]) sd.history[chatId] = [];
  sd.history[chatId].push({ role: 'assistant', content: displayText });
  if (sd.history[chatId].length > 30) sd.history[chatId] = sd.history[chatId].slice(-30);
}

const reply = displayText || (toolUseBlock ? ('Выполняю ' + toolUseBlock.input.type + '...') : '(no response)');

return [{ json: {
  chatId, reply,
  hasToolCall: !!toolUseBlock,
  commandId, timedOutChats,
  inputTokens: usage.input_tokens, outputTokens: usage.output_tokens, cost
} }];
""".strip() % (API_KEY_1, API_KEY_2, API_KEY_1)

PROCESS_RESULT_CODE = """
const incomingToken = $input.first().json.headers?.['x-agent-token'];
if (incomingToken !== '%s') return [{ json: { authorized: false } }];

const body        = $input.first().json.body;
const chatId      = String(body.chat_id);
const commandId   = body.command_id;
const commandType = body.command_type;
const result      = body.result;

const sd = $getWorkflowStaticData('global');
if (!sd.tool_calls) sd.tool_calls = {};
if (!sd.history)    sd.history    = {};
if (!sd.spend)      sd.spend      = { total_usd: 0, limit_usd: 5, anthropic_usd: 0, openai_usd: 0 };

// GC stale tool_calls
const now = Date.now();
const TIMEOUT_MS = 5 * 60 * 1000;
const timedOutChats = [];
for (const [cmdId, ctx] of Object.entries(sd.tool_calls)) {
  if (now - ctx.ts > TIMEOUT_MS) {
    timedOutChats.push(ctx.chat_id);
    delete sd.tool_calls[cmdId];
  }
}

const ctx = sd.tool_calls[commandId];

// Legacy path: not a tool_call result
if (!ctx) {
  return [{ json: { chatId, commandType, result, isToolCall: false, authorized: true, timedOutChats } }];
}

// Tool Call path: close the async loop
delete sd.tool_calls[commandId];

const toolResultContent = result.success
  ? (typeof result.data === 'string' ? result.data : JSON.stringify(result.data))
  : ('Error: ' + result.error);

// Reconstruct messages for second Anthropic call
const messagesForClaude = [
  ...ctx.messages_snapshot,
  {
    role: 'assistant',
    content: [{ type: 'tool_use', id: ctx.tool_use_id, name: ctx.tool_name, input: ctx.tool_input }]
  },
  {
    role: 'user',
    content: [{ type: 'tool_result', tool_use_id: ctx.tool_use_id, content: toolResultContent, is_error: !result.success }]
  }
];

// Second Anthropic call
const https = require('https');
const apiKey = '%s';

const TOOLS = [{
  name: 'queue_agent_command',
  description: 'Queue a command for the local Windows PC agent.',
  input_schema: {
    type: 'object',
    properties: {
      type: { type: 'string', enum: ['screenshot','terminal','obsidian-log','obsidian-read','system-info','launch-app','install'] },
      payload: { type: 'object' }
    },
    required: ['type']
  }
}];

const reqBody = JSON.stringify({
  model: 'claude-haiku-4-5-20251001',
  max_tokens: 1024,
  system: 'You are a helpful assistant controlling a Windows PC remotely via Telegram. Be concise. Use the queue_agent_command tool to perform actions on the PC.',
  messages: messagesForClaude,
  tools: TOOLS,
  tool_choice: { type: 'auto' }
});

const apiResp2 = await new Promise((resolve, reject) => {
  const req = https.request({
    hostname: 'api.anthropic.com', path: '/v1/messages', method: 'POST',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(reqBody)
    }
  }, res => {
    const chunks = [];
    res.on('data', c => chunks.push(c));
    res.on('end', () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString())); } catch(e) { reject(e); } });
    res.on('error', reject);
  });
  req.on('error', reject);
  req.write(reqBody); req.end();
});

// Track cost
const usage2 = apiResp2.usage || { input_tokens: 0, output_tokens: 0 };
const cost2 = (usage2.input_tokens / 1_000_000 * 0.80) + (usage2.output_tokens / 1_000_000 * 4.00);
sd.spend.anthropic_usd = (sd.spend.anthropic_usd || 0) + cost2;
sd.spend.total_usd     = (sd.spend.total_usd || 0) + cost2;

// Parse second response
let finalText    = '';
let nextToolBlock = null;
for (const block of (apiResp2.content || [])) {
  if (block.type === 'text')     finalText     += block.text;
  if (block.type === 'tool_use') nextToolBlock  = block;
}

// Update history: full tool_use -> tool_result -> final assistant reply
if (!sd.history[chatId]) sd.history[chatId] = [];
sd.history[chatId].push(
  { role: 'assistant', content: [{ type: 'tool_use', id: ctx.tool_use_id, name: ctx.tool_name, input: ctx.tool_input }] },
  { role: 'user',      content: [{ type: 'tool_result', tool_use_id: ctx.tool_use_id, content: toolResultContent }] }
);
if (finalText) {
  sd.history[chatId].push({ role: 'assistant', content: finalText });
}
if (sd.history[chatId].length > 40) sd.history[chatId] = sd.history[chatId].slice(-40);

// Chain: Claude wants another tool call
if (nextToolBlock) {
  const nextCmdId = nextToolBlock.id;
  sd.tool_calls[nextCmdId] = {
    tool_use_id:       nextToolBlock.id,
    tool_name:         nextToolBlock.name,
    tool_input:        nextToolBlock.input,
    chat_id:           chatId,
    messages_snapshot: [...sd.history[chatId]],
    ts:                now
  };
  if (!sd.command_queue) sd.command_queue = [];
  sd.command_queue.push({
    command_id: nextCmdId, chat_id: chatId,
    type: nextToolBlock.input.type, payload: nextToolBlock.input.payload || {}
  });
}

const reply = finalText || (nextToolBlock ? ('Продолжаю: ' + nextToolBlock.input.type + '...') : 'Done');

return [{ json: {
  chatId, reply, isToolCall: true, commandType, authorized: true,
  timedOutChats, hasChainedCommand: !!nextToolBlock
} }];
""".strip() % (AGENT_TOKEN, API_KEY_1)

# ── Connect to n8n ─────────────────────────────────────────────────────────────
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
login_data = json.dumps({'emailOrLdapLoginId':'adil.mombekov97@gmail.com','password':'Dispatch2025!'}).encode()
opener.open(urllib.request.Request('https://n8n-production-419d8.up.railway.app/rest/login',
    data=login_data, headers={'Content-Type':'application/json'}))

resp = opener.open('https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J')
wf = json.loads(resp.read()).get('data', {})

# ── Update code nodes ──────────────────────────────────────────────────────────
for node in wf.get('nodes', []):
    if node['name'] == 'Anthropic Full':
        node['parameters']['jsCode'] = ANTHROPIC_FULL_CODE
        print('Updated: Anthropic Full')
    elif node['name'] == 'Process Result':
        node['parameters']['jsCode'] = PROCESS_RESULT_CODE
        print('Updated: Process Result')

# ── Add new nodes if not present ──────────────────────────────────────────────
existing_names = {n['name'] for n in wf.get('nodes', [])}

NEW_NODES = [
    {
        "parameters": {
            "conditions": {
                "combinator": "and",
                "conditions": [{
                    "leftValue": "={{ $json.isToolCall }}",
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "true"}
                }]
            }
        },
        "id": "is-tool-call",
        "name": "Is Tool Call?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2,
        "position": [650, 1000]
    },
    {
        "parameters": {
            "chatId": "={{ $json.chatId }}",
            "text": "={{ $json.reply }}",
            "additionalFields": {}
        },
        "id": "send-tool-reply",
        "name": "Send Tool Reply",
        "type": "n8n-nodes-base.telegram",
        "typeVersion": 1,
        "position": [900, 860],
        "credentials": {"telegramApi": {"id": "Ie6tdHWYmxtqisex", "name": "TELEGRAM_BOT_TOKEN"}}
    },
    {
        "parameters": {
            "chatId": "={{ $json.timedOutChats[0] }}",
            "text": "=⏳ Команда не выполнена — агент не ответил 5 минут.\nПроверьте, запущен ли DispatchAgent.exe.",
            "additionalFields": {}
        },
        "id": "send-timeout-notify",
        "name": "Send Timeout Notify",
        "type": "n8n-nodes-base.telegram",
        "typeVersion": 1,
        "position": [900, 1200],
        "credentials": {"telegramApi": {"id": "Ie6tdHWYmxtqisex", "name": "TELEGRAM_BOT_TOKEN"}}
    }
]

for nn in NEW_NODES:
    if nn['name'] not in existing_names:
        wf['nodes'].append(nn)
        print(f'Added node: {nn["name"]}')

# ── Update connections ─────────────────────────────────────────────────────────
conns = wf.setdefault('connections', {})

# Process Result -> Is Tool Call? (was -> Result Router directly)
conns['Process Result'] = {
    "main": [[{"node": "Is Tool Call?", "type": "main", "index": 0}]]
}
# Is Tool Call? true -> Send Tool Reply, false -> Result Router
conns['Is Tool Call?'] = {
    "main": [
        [{"node": "Send Tool Reply", "type": "main", "index": 0}],
        [{"node": "Result Router",   "type": "main", "index": 0}]
    ]
}
print('Updated connections')

# ── Deactivate, PATCH, reactivate ─────────────────────────────────────────────
# Deactivate
req_d = urllib.request.Request(
    'https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J',
    data=json.dumps({'active': False}).encode(),
    headers={'Content-Type':'application/json'}, method='PATCH')
opener.open(req_d)
time.sleep(2)

# PATCH workflow
wf_data = json.dumps(wf).encode()
req_p = urllib.request.Request(
    'https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J',
    data=wf_data, headers={'Content-Type':'application/json'}, method='PATCH')
resp2 = opener.open(req_p)
result = json.loads(resp2.read())
print('PATCH:', resp2.status, '| updatedAt:', result.get('data',{}).get('updatedAt'))

# Get new versionId and activate
time.sleep(1)
wf2 = json.loads(opener.open('https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J').read()).get('data',{})
vid = wf2.get('versionId')
req_a = urllib.request.Request(
    'https://n8n-production-419d8.up.railway.app/rest/workflows/aKwY0wZ238JbzR8J/activate',
    data=json.dumps({'versionId': vid}).encode(),
    headers={'Content-Type':'application/json'}, method='POST')
r_a = json.loads(opener.open(req_a).read())
print('Active:', r_a.get('data',{}).get('active'))
