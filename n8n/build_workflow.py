"""Sinh file n8n/workflows/tiktok-agent.json (chạy: python3 n8n/build_workflow.py).

Bạn KHÔNG cần chạy file này — file JSON đã có sẵn. Chỉ dùng khi muốn sửa workflow bằng code.
"""
import json
import uuid
from pathlib import Path

NS = uuid.UUID("6f1c2a52-8c55-4e1a-9a55-6b0f2f3c9d11")
nodes, conns = [], {}


def node(name, type_, version, params, pos, **extra):
    nodes.append({"id": str(uuid.uuid5(NS, name)), "name": name, "type": type_, "typeVersion": version,
                  "position": pos, "parameters": params, **extra})
    return name


def link(a, b, out=0):
    conns.setdefault(a, {"main": []})
    while len(conns[a]["main"]) <= out:
        conns[a]["main"].append([])
    conns[a]["main"][out].append({"node": b, "type": "main", "index": 0})


def code(name, js, pos):
    return node(name, "n8n-nodes-base.code", 2, {"jsCode": js.strip()}, pos)


WORKER_HEADERS = {"parameters": [{"name": "X-Worker-Token", "value": "={{ $env.WORKER_TOKEN }}"}]}
TG = "={{ $env.TELEGRAM_API }}/bot{{ $env.TELEGRAM_BOT_TOKEN }}/"

# ---------------------------------------------------------------- triggers
wh = node("Telegram gửi tới", "n8n-nodes-base.webhook", 2,
          {"httpMethod": "POST", "path": "telegram", "responseMode": "onReceived", "options": {}},
          [0, 0], webhookId=str(uuid.uuid5(NS, "telegram-webhook")))

classify = code("Phân loại tin nhắn", r"""
// Đổi tin nhắn Telegram thành 1 yêu cầu cho worker: lệnh (/...), bấm nút, góp ý (chữ), clip video, nhạc nền, ảnh.
const u = $json.body || {};
const secret = ($json.headers || {})['x-telegram-bot-api-secret-token'] || '';
let route = null;
const body = { secret };
if (u.callback_query) {
  const c = u.callback_query;
  route = 'tg/button';
  Object.assign(body, { chat_id: c.message?.chat?.id, data: c.data, message_id: c.message?.message_id, callback_id: c.id });
} else if (u.message) {
  const m = u.message;
  Object.assign(body, {
    chat_id: m.chat.id, message_id: m.message_id,
    reply_to_text: m.reply_to_message?.text || m.reply_to_message?.caption || '',
  });
  const doc = m.document && (m.document.mime_type || '').startsWith('video/') ? m.document : null;
  const vid = m.video || m.animation || doc;
  if (vid) {
    route = 'tg/clip';
    Object.assign(body, { file_id: vid.file_id, file_size: vid.file_size || 0, caption: m.caption || '', media_group_id: m.media_group_id || '' });
  } else if (m.audio || (m.document && (m.document.mime_type || '').startsWith('audio/'))) {
    const a = m.audio || m.document;
    route = 'tg/audio';
    Object.assign(body, { file_id: a.file_id, file_size: a.file_size || 0, file_name: a.file_name || '',
                          mime_type: a.mime_type || '', title: a.title || '' });
  } else if (m.photo) {
    route = 'tg/photo';
    Object.assign(body, { file_id: m.photo[m.photo.length - 1].file_id, caption: m.caption || '' });
  } else if ((m.text || '').startsWith('/')) {
    route = 'tg/command'; body.text = m.text;
  } else if (m.text) {
    route = 'tg/text'; body.text = m.text;
  }
}
if (!route) return [];
return [{ json: { route, body } }];
""", [220, 0])

ack = code("Báo đã nhận nút bấm", r"""
// Tắt vòng xoay trên nút vừa bấm (Telegram yêu cầu trả lời trong vài giây).
const id = $json.body.callback_id;
if (!id) return [];
return [{ json: { method: 'answerCallbackQuery', payload: { callback_query_id: id, text: '⏳ Đã nhận' } } }];
""", [440, -200])

s_plan = node("06:30 – kiểm tra kế hoạch 30 ngày", "n8n-nodes-base.scheduleTrigger", 1.2,
              {"rule": {"interval": [{"field": "cronExpression", "expression": "30 6 * * *"}]}}, [0, 220])
s_daily = node("07:00 – gửi kịch bản hằng ngày", "n8n-nodes-base.scheduleTrigger", 1.2,
               {"rule": {"interval": [{"field": "cronExpression", "expression": "0 7 * * *"}]}}, [0, 420])
r_plan = code("Yêu cầu: kế hoạch", "return [{ json: { route: 'scheduled/plan', body: {} } }];", [220, 220])
r_daily = code("Yêu cầu: kịch bản", "return [{ json: { route: 'scheduled/daily', body: {} } }];", [220, 420])

# ---------------------------------------------------------------- worker
call = node("Gọi worker", "n8n-nodes-base.httpRequest", 4.2, {
    "method": "POST",
    "url": "={{ $env.WORKER_URL }}/{{ $json.route }}",
    "sendHeaders": True, "headerParameters": WORKER_HEADERS,
    "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify($json.body || {}) }}",
    "options": {"timeout": 1800000, "response": {"response": {"neverError": True}}},
}, [660, 220], onError="continueRegularOutput")

result = code("Đọc kết quả", r"""
// Worker luôn trả {messages: [...], next: {...}}. Nếu worker lỗi/không chạy → báo lỗi lên Telegram.
const out = [];
for (const it of $input.all()) {
  const j = it.json || {};
  if (Array.isArray(j.messages)) { out.push({ json: { messages: j.messages, next: j.next || null } }); continue; }
  const err = j.error?.message || j.detail || JSON.stringify(j).slice(0, 300);
  out.push({ json: { next: null, messages: [{ method: 'sendMessage', payload: {
    chat_id: $env.TELEGRAM_CHAT_ID, text: '⚠️ n8n không gọi được worker: ' + err } }] } });
}
return out;
""", [880, 220])

split = code("Tách tin nhắn", r"""
const out = [];
for (const it of $input.all()) for (const m of it.json.messages || []) out.push({ json: m });
return out;
""", [1100, 100])

has_file = node("Có file đính kèm?", "n8n-nodes-base.if", 2.2, {
    "conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
        "conditions": [{"id": str(uuid.uuid5(NS, "c-file")), "leftValue": "={{ $json.file || '' }}", "rightValue": "",
                        "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}],
        "combinator": "and"},
    "options": {}}, [1320, 100])

fetch = node("Tải file từ worker", "n8n-nodes-base.httpRequest", 4.2, {
    "url": "={{ $env.WORKER_URL }}/files?path={{ encodeURIComponent($json.file) }}",
    "sendHeaders": True, "headerParameters": WORKER_HEADERS,
    "options": {"timeout": 120000, "response": {"response": {"responseFormat": "file", "outputPropertyName": "data"}}},
}, [1540, 0])

S = "$('Tách tin nhắn').item.json"
send_media = node("Telegram gửi ảnh/video", "n8n-nodes-base.httpRequest", 4.2, {
    "method": "POST",
    "url": TG + "{{ " + S + ".method }}",
    "sendBody": True, "contentType": "multipart-form-data",
    "bodyParameters": {"parameters": [
        {"name": "chat_id", "value": "={{ " + S + ".payload.chat_id }}"},
        {"name": "caption", "value": "={{ " + S + ".payload.caption }}"},
        {"name": "parse_mode", "value": "HTML"},
        {"name": "reply_markup", "value": "={{ " + S + ".payload.reply_markup }}"},
        {"name": "supports_streaming", "value": "={{ " + S + ".payload.supports_streaming || 'false' }}"},
        {"parameterType": "formBinaryData", "name": "={{ " + S + ".field }}", "inputDataFieldName": "data"},
    ]},
    "options": {"timeout": 600000, "response": {"response": {"neverError": True}},
                "batching": {"batch": {"batchSize": 1, "batchInterval": 500}}},
}, [1760, 0])

send = node("Telegram API", "n8n-nodes-base.httpRequest", 4.2, {
    "method": "POST",
    "url": TG + "{{ $json.method }}",
    "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify($json.payload) }}",
    "options": {"timeout": 60000, "response": {"response": {"neverError": True}},
                "batching": {"batch": {"batchSize": 1, "batchInterval": 300}}},
}, [1540, 200])

has_next = node("Còn bước tiếp?", "n8n-nodes-base.if", 2.2, {
    "conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
        "conditions": [{"id": str(uuid.uuid5(NS, "c-next")), "leftValue": "={{ !!($json.next && $json.next.action) }}",
                        "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
        "combinator": "and"},
    "options": {}}, [1100, 420])

nxt = code("Chuẩn bị bước tiếp", r"""
// Bước dài (lập kế hoạch, viết kịch bản, ghép video…) chạy sau khi tin "⏳ Đang xử lý" đã được gửi.
return $input.all().map(it => ({ json: { route: 'continue', body: it.json.next } }));
""", [1320, 420])

for a, b in [(wh, classify), (classify, ack), (classify, call), (ack, send), (s_plan, r_plan), (s_daily, r_daily),
             (r_plan, call), (r_daily, call), (call, result), (result, split), (result, has_next),
             (split, has_file), (fetch, send_media), (nxt, call)]:
    link(a, b)
link(has_file, fetch, 0)
link(has_file, send, 1)
link(has_next, nxt, 0)

wf = {
    "id": "TikTokAgent0001",
    "name": "TikTok Agent – Telegram duyệt bài",
    "active": False,
    "nodes": nodes,
    "connections": conns,
    "settings": {"executionOrder": "v1", "timezone": "Asia/Ho_Chi_Minh", "saveManualExecutions": True},
    "pinData": {},
    "meta": {"templateCredsSetupCompleted": True},
    "tags": [],
}
out = Path(__file__).parent / "workflows" / "tiktok-agent.json"
out.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("Đã ghi", out)
