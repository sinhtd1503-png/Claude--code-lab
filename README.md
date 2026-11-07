# Claude--code-lab
Train skills AI
curl -fsSL https://claude.ai/install.sh | bash

---

# TikTok Content Agent — Tuổi thơ & kỷ niệm gia đình

Agent tự động mỗi ngày: **quét kênh đối thủ → Claude phân tích xu hướng → viết kịch bản → tạo video faceless (giọng AI + video stock + phụ đề) → gửi Telegram để bạn duyệt → đăng TikTok.**

```
[07:00 mỗi ngày]  Apify scrape  →  SQLite  →  Claude phân tích (pattern, hook, hashtag, 3 ý tưởng)
                                          →  Claude viết kịch bản (cảnh + lời đọc + từ khoá hình ảnh)
                                          →  edge-tts (giọng Việt) + Pixabay clips + FFmpeg → MP4 1080x1920
                                          →  Telegram: [✅ Duyệt & đăng] [✏️ Sửa] [🔄 Làm lại] [❌ Bỏ]
                                          →  (chỉ khi duyệt) TikTok Content Posting API
```

## Cài đặt

```bash
sudo apt-get install -y ffmpeg fonts-dejavu-core
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # điền các key
```

| Biến | Lấy ở đâu |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `APIFY_TOKEN` | console.apify.com → Integrations (dùng actor `clockworks/tiktok-scraper`) |
| `GEMINI_API_KEY` | aistudio.google.com/apikey (video AI Gemini Omni Flash, cần bật thanh toán) |
| `PIXABAY_API_KEY` | pixabay.com/api/docs (đăng nhập, key hiện trong mục *Parameters*, miễn phí) |
| `TELEGRAM_BOT_TOKEN` | Tạo bot với @BotFather |
| `TELEGRAM_CHAT_ID` | Chạy bot rồi gửi `/chatid` |
| `TIKTOK_CLIENT_KEY/SECRET/REDIRECT_URI` | developers.tiktok.com → tạo app, bật Login Kit + Content Posting API |

Sửa `config.yaml`: danh sách `competitors` (username không có @), giọng đọc, giờ chạy, chế độ đăng.

## Sử dụng

```bash
.venv/bin/python -m tiktok_agent.main tiktok-auth   # kết nối tài khoản TikTok (1 lần)
.venv/bin/python -m tiktok_agent.main research      # thử: chỉ quét + phân tích
.venv/bin/python -m tiktok_agent.main run-once      # thử: tạo video, không qua Telegram
.venv/bin/python -m tiktok_agent.main bot           # chạy chính: bot + lịch hằng ngày
```

Trong Telegram: `/run` tạo ngay, `/status` xem trạng thái. Nút **✏️ Sửa** → gõ góp ý → agent viết lại và render lại.

## Video AI (Gemini Omni Flash)

Cấu hình trong mục `ai_video` của `config.yaml`:
- `scenes`: cảnh nào dùng AI (`"first"`, `"all"`, `"none"` hoặc danh sách như `[1, 3]`); các cảnh còn lại dùng Pixabay.
- Mỗi cảnh AI được tách thành các shot tối đa `max_shot_sec` giây. Mọi shot dùng chung mô tả nhân vật/bối cảnh/phong cách (`visual_bible` do Claude viết) để giữ nhất quán.
- `daily_limit_sec`: giới hạn giây video AI mỗi ngày. Hết hạn mức, bị chặn hoặc lỗi thì cảnh đó tự chuyển sang Pixabay.
- Chi phí ước tính mỗi lần chạy được ghi vào `data/logs/ai_cost.jsonl` (mỗi dòng một lần chạy, có chi tiết từng shot).

> Veo trên Gemini API đã bị Google tắt từ 22/10/2026, nên agent dùng Gemini Omni Flash (model thay thế, cùng API key).

## Đăng TikTok: 2 chế độ (`tiktok.post_mode`)

- **`inbox`** (mặc định): video được đẩy vào hộp thư/nháp của app TikTok; bạn mở app, dán caption (bot gửi sẵn) và bấm đăng. Chỉ cần scope `video.upload`, dùng được ngay.
- **`direct`**: đăng thẳng. Cần scope `video.publish`. **App chưa qua audit của TikTok thì chỉ đăng được ở chế độ riêng tư (SELF_ONLY).** Gửi app đi audit, khi được duyệt thì đổi `post_mode: direct`.

## Lưu ý

- Agent **học pattern** từ đối thủ (chủ đề, hook, độ dài, hashtag) và viết nội dung **mới**, không tải lại video của họ (tránh bản quyền và bị TikTok giảm phân phối vì nội dung không nguyên bản).
- Scrape TikTok qua bên thứ ba đi ngược điều khoản của TikTok. Nên giữ ở mức nhẹ (vài kênh, 1 lần/ngày).
- Nội dung có giọng AI: nên bật nhãn "AI-generated content" khi đăng.
- `edge-tts` miễn phí nhưng không chính thức (code đã có retry). Muốn ổn định hơn có thể thay bằng ElevenLabs, Google TTS hoặc FPT.AI trong `tiktok_agent/video.py`.
- Nhạc nền: đặt file mp3 không bản quyền vào `video.background_music`.
- Chạy 24/7 trên VPS: `nohup .venv/bin/python -m tiktok_agent.main bot &` hoặc tạo service systemd.
