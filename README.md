# Claude--code-lab
Train skills AI
curl -fsSL https://claude.ai/install.sh | bash

---

# TikTok Content Agent — Tuổi thơ 8x 9x, nề nếp gia đình, chân – thiện – mỹ

> 👉 **Người mới / làm trên iPad: đọc [HUONG_DAN.md](HUONG_DAN.md)** — hướng dẫn từng bước, cài bằng 1 lệnh.

Hệ thống tự động: **quét kênh đối thủ → Claude lập kế hoạch 30 ngày với một gia đình nhân vật cố định → mỗi sáng gửi
kịch bản qua Telegram để duyệt → bạn tạo 3–4 clip AI 8 giây miễn phí (Google Flow) → tự ghép video 24–32 giây có giọng
đọc + phụ đề → gửi Telegram duyệt → đăng lên các kênh TikTok.** Mọi khoản trả phí đều phải được duyệt ngân sách trên Telegram trước.

## Kiến trúc (Docker trên 1 VPS)

```
Telegram ⇄ Caddy (HTTPS) ⇄ n8n  ── lịch 06:30/07:00, nhận/gửi tin Telegram, điều phối các bước
                              │
                              └─► worker (FastAPI, tiktok_agent/worker.py)
                                    Claude Opus 5.5 (kế hoạch, kịch bản) · ngân sách · clip · FFmpeg · edge-tts · TikTok API
```

| File | Vai trò |
|---|---|
| `deploy/install.sh` | Cài/cập nhật toàn bộ trên Ubuntu bằng 1 lệnh (Docker, HTTPS sslip.io, nạp workflow, webhook Telegram) |
| `docker-compose.yml`, `Dockerfile`, `deploy/Caddyfile` | Caddy + n8n 2.44.6 + worker |
| `n8n/workflows/tiktok-agent.json` | Workflow n8n (sinh bởi `n8n/build_workflow.py`) |
| `tiktok_agent/worker.py` | API cho n8n: lệnh Telegram, nút bấm, nhận clip, ghép video, đăng TikTok |
| `tiktok_agent/series.py` | Kế hoạch 30 ngày + series bible (nhân vật/bối cảnh cố định) + kịch bản + prompt clip |
| `tiktok_agent/budget.py` | Sổ chi phí và cổng duyệt ngân sách |
| `tiktok_agent/video.py`, `manual_clips.py`, `ai_video.py`, `publisher.py`, `scraper.py` | Dựng video, clip Flow, clip Gemini (trả phí), TikTok, Apify |

Mỗi endpoint của worker trả `{"messages": [...lệnh Telegram...], "next": {...}}`; n8n gửi các tin nhắn rồi gọi bước
tiếp theo (`/continue`) — nhờ vậy bạn luôn nhận "⏳ Đang xử lý…" trước khi các bước dài (Claude, ghép video) chạy.

## Chạy thử trên máy có Docker

```bash
cp .env.example .env   # điền TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, DOMAIN, WORKER_TOKEN, TELEGRAM_WEBHOOK_SECRET, N8N_ENCRYPTION_KEY
docker compose up -d --build
docker compose exec n8n n8n import:workflow --input=/workflows/tiktok-agent.json
docker compose exec n8n n8n publish:workflow --id=TikTokAgent0001 && docker compose restart n8n
```

## Chế độ cũ (CLI Python, không dùng n8n)

`python -m tiktok_agent.main bot | run-once | research | tiktok-auth` vẫn chạy được (bot Telegram dạng polling).
**Không chạy song song với bản n8n** — Telegram chỉ cho một nơi nhận tin của cùng một bot.

## Lưu ý

- Agent **học pattern** từ đối thủ và viết nội dung **mới**, không tải lại video của họ.
- Scrape TikTok qua bên thứ ba đi ngược điều khoản của TikTok — giữ ở mức nhẹ (vài kênh, 1 lần/tháng khi lập kế hoạch).
- Google Flow không có API; hệ thống **không** tự động hoá web Flow (vi phạm điều khoản, dễ bị khoá tài khoản).
- Nên bật nhãn "AI-generated content" khi đăng video dựng bằng AI.
- `inbox` = đẩy vào hộp nháp TikTok (dùng ngay); `direct` = đăng thẳng, chỉ công khai sau khi app qua audit của TikTok.
