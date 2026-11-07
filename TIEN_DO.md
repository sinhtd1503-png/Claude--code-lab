# Tiến độ dự án — TikTok Content Agent

_Cập nhật: 07/11/2026_

## 🎯 Mục tiêu dự án

Xây agent tự động cho kênh TikTok ngách **tuổi thơ, kỷ niệm gia đình, hồi ức và năng lượng tích cực** (giúp con cái, vợ chồng nhớ lại những điều quý giá thay vì bị cuốn theo cuộc sống hằng ngày). Mỗi ngày agent:

1. Quét các kênh đối thủ có lượng like, theo dõi lớn
2. Phân tích vì sao nội dung của họ hiệu quả
3. Tự viết kịch bản mới và tạo video
4. Gửi video qua Telegram để chủ kênh duyệt
5. Chỉ đăng lên TikTok sau khi được duyệt

## ✅ Đã làm xong (06–07/11/2026)

- **Code agent hoàn chỉnh** trong `tiktok_agent/` (Python):
  - `scraper.py`: lấy video đối thủ qua Apify
  - `content.py`: Claude phân tích xu hướng và viết kịch bản (structured output)
  - `video.py`: giọng đọc AI tiếng Việt + clip Pexels + phụ đề → MP4 1080x1920
  - `telegram_bot.py`: gửi video để duyệt với các nút Duyệt / Sửa / Làm lại / Bỏ, chạy lịch hằng ngày
  - `publisher.py`: đăng TikTok qua Content Posting API (OAuth, inbox, direct post)
  - `pipeline.py`, `db.py` (SQLite), `main.py` (CLI: `bot`, `run-once`, `research`, `tiktok-auth`)
- **File cấu hình:** `config.yaml` (ngách, đối thủ, giọng đọc, giờ chạy, chế độ đăng), `.env.example`, `README.md` (hướng dẫn cài đặt)
- **Đã chạy thử:**
  - Dựng video thật chạy được, phụ đề tiếng Việt hiển thị đủ dấu
  - Luồng tạo bài và lưu database chạy đúng (bước gọi Claude được thay bằng dữ liệu giả)
  - Bot Telegram kết nối thành công, đã lấy và điền `TELEGRAM_CHAT_ID`
- **Git:** code được merge vào `main` qua PR #1

## 🧭 Các quyết định quan trọng

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Kiểu video | Faceless: giọng AI + video stock (Pexels) + phụ đề | Rẻ, tự động 100%, hợp với nội dung kể chuyện |
| Lấy dữ liệu đối thủ | Apify (actor `clockworks/tiktok-scraper`) | Ổn định hơn thư viện miễn phí, ít bị chặn |
| Cách đăng | Làm cả hai chế độ: `inbox` (mặc định) và `direct` | TikTok chỉ cho đăng công khai qua API sau khi app qua audit; trước đó dùng inbox |
| Nội dung | Học pattern từ đối thủ, viết nội dung **mới** | Tránh bản quyền và bị TikTok giảm phân phối vì nội dung không nguyên bản |
| Mô hình AI | Claude Opus 5.5, bật fallback phía server | Chất lượng viết tiếng Việt tốt, tự chuyển sang model khác nếu bị từ chối |
| Giọng đọc | edge-tts (`vi-VN-HoaiMyNeural`) | Miễn phí, giọng Việt tự nhiên |
| Bảo mật | Key nằm trong `.env` (đã nằm trong `.gitignore`), bot chỉ nhận lệnh từ `TELEGRAM_CHAT_ID` | Không lộ key lên GitHub, người lạ không điều khiển được bot |

## 🐞 Lỗi / vấn đề còn tồn tại

1. **Còn thiếu key:** `ANTHROPIC_API_KEY`, `PEXELS_API_KEY`, `APIFY_TOKEN`, `TIKTOK_CLIENT_KEY/SECRET` vẫn trống, nên chưa tạo được video thật.
2. **Chưa chạy thử với dịch vụ thật:** Claude, Apify, Pexels, đăng TikTok. Cấu trúc dữ liệu của Apify và TikTok được viết theo hiểu biết hiện có, có thể phải chỉnh khi chạy lần đầu.
3. **Bot không chạy liên tục:** bot dừng khi phiên Codespaces kết thúc, nên lịch 07:00 hằng ngày chưa hoạt động.
4. **edge-tts hay lỗi ở lần gọi đầu** ("No audio was received"). Tạm xử lý bằng cách thử lại tối đa 5 lần.
5. **Hạn chế đã biết:**
   - Chưa hỗ trợ upload video lớn hơn 64MB (cần chia chunk); video hiện tại nhỏ hơn nhiều.
   - Thời điểm hiện phụ đề được ước lượng theo độ dài chữ, không khớp chính xác từng từ.
   - Chế độ `direct` chỉ đăng được riêng tư (SELF_ONLY) cho tới khi app TikTok qua audit.

## 📋 3 việc cần làm tiếp theo

1. **Điền `ANTHROPIC_API_KEY` và `PEXELS_API_KEY` vào `.env`**, chạy bot rồi gửi `/run` trên Telegram để tạo video đầu tiên. Sửa lỗi nếu có.
2. **Lấy `APIFY_TOKEN` và điền username đối thủ vào `config.yaml`**, chạy `python -m tiktok_agent.main research` để kiểm tra bước quét và phân tích.
3. **Tạo app trên TikTok for Developers** (Login Kit + Content Posting API, bật Sandbox), chạy `tiktok-auth`, rồi thử bấm Duyệt để đẩy video vào inbox TikTok.

_Sau đó:_ đưa bot lên VPS chạy 24/7 và gửi app TikTok đi audit để đăng thẳng công khai.
