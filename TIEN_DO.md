# Tiến độ dự án — TikTok Content Agent

_Cập nhật: 07/11/2026_

## 🎯 Mục tiêu dự án

Xây agent tự động cho kênh TikTok ngách **tuổi thơ, kỷ niệm gia đình, hồi ức và năng lượng tích cực** (giúp con cái, vợ chồng nhớ lại những điều quý giá thay vì bị cuốn theo cuộc sống hằng ngày). Mỗi ngày agent:

1. Quét các kênh đối thủ có lượng like, theo dõi lớn
2. Phân tích vì sao nội dung của họ hiệu quả
3. Tự viết kịch bản mới và tạo video (giọng đọc AI + video stock Pixabay + video AI)
4. Gửi video qua Telegram để chủ kênh duyệt
5. Chỉ đăng lên TikTok sau khi được duyệt

## ✅ Đã làm xong

### 06/11/2026
- Code agent hoàn chỉnh trong `tiktok_agent/` (Python): scraper Apify, Claude phân tích và viết kịch bản, dựng video bằng FFmpeg, bot Telegram duyệt bài, đăng TikTok qua Content Posting API, SQLite, CLI
- Merge vào `main` qua PR #1. Bot Telegram kết nối thành công, đã điền `TELEGRAM_CHAT_ID`

### Hôm nay (07/11/2026)
- **Chạy thật lần đầu:** Claude phân tích ngách và viết kịch bản thành công. Video #1 "Chiếc quạt nan và những đêm mất điện" (49 giây) đã gửi lên Telegram để duyệt
- **Sửa lỗi Telegram:** gửi video bị timeout → tăng thời gian chờ và tự thử lại 3 lần
- **Đổi Pexels → Pixabay** (Pexels khoá API): đổi code, `.env`, tài liệu. Chạy thật thành công, 6/6 cảnh có clip. Sửa luôn lỗi log in URL có chứa key Pixabay
- **Thêm nguồn video AI** (`tiktok_agent/ai_video.py`), dùng Gemini Omni 1.2 Flash qua Gemini API:
  - Tách mỗi cảnh thành các shot tối đa 8 giây, viết prompt cho từng shot. Mọi prompt dùng chung mô tả nhân vật, bối cảnh, phong cách (`visual_bible` do Claude viết) để giữ nhất quán
  - Gọi API, chờ kết quả, tải về, ghép bằng FFmpeg cùng giọng đọc và phụ đề
  - Cấu hình cảnh nào dùng AI, cảnh nào dùng Pixabay (`ai_video.scenes`)
  - Giới hạn số giây video AI mỗi ngày (`daily_limit_sec`). Hết hạn mức hoặc lỗi thì cảnh đó tự chuyển sang Pixabay
  - Ghi chi phí ước tính từng lần chạy vào `data/logs/ai_cost.jsonl`
  - Đã chạy thử bằng API giả lập: tách shot, chuyển sang Pixabay khi lỗi, hạn mức, log đều đúng
- **Tìm kênh đối thủ** qua Apify (175 video) và đưa 6 kênh vào `config.yaml`: 4 kênh hoài niệm làm bằng AI (`happybabyworld2026`, `aique64`, `cachsudungvo3ai`, `inconmainhaxua1`) và 2 kênh hoài niệm 8x 9x lớn (`thehien.kyuc8x9x` 530K follow, `nammotnganchintramhoido` 355K follow)

## 🧭 Các quyết định quan trọng

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Kiểu video | Faceless: giọng AI + video stock + video AI + phụ đề | Rẻ, tự động, hợp với nội dung kể chuyện |
| Video stock | **Pixabay** (đổi từ Pexels) | Pexels khoá API |
| Video AI | **Gemini Omni 1.2 Flash**, 360p, chỉ cảnh mở đầu, tối đa 60 giây/ngày | Veo trên Gemini API đã bị Google tắt từ 22/10/2026, Omni Flash là model thay thế dùng cùng `GEMINI_API_KEY`. Bắt đầu ở mức rẻ nhất (~$0.023/giây) để thử nghiệm |
| Lấy dữ liệu đối thủ | Apify (actor `clockworks/tiktok-scraper`) | Ổn định hơn thư viện miễn phí |
| Chọn đối thủ | Kết hợp kênh AI (25K–125K follow) và kênh hoài niệm lớn không dùng AI | Kênh hoài niệm làm bằng AI chưa có kênh nào thật lớn; kênh lớn giúp học chủ đề và hook |
| Cách đăng | Hai chế độ: `inbox` (mặc định) và `direct` | TikTok chỉ cho đăng công khai qua API sau khi app qua audit |
| Nội dung | Học pattern từ đối thủ, viết nội dung **mới** | Tránh bản quyền và bị giảm phân phối |
| Mô hình viết | Claude Opus 5.5, bật fallback phía server | Viết tiếng Việt tốt |
| Giọng đọc | edge-tts (`vi-VN-HoaiMyNeural`) | Miễn phí, giọng Việt tự nhiên |
| Bảo mật | Key chỉ nằm trong `.env` (nằm trong `.gitignore`), không in ra log; bot chỉ nhận lệnh từ `TELEGRAM_CHAT_ID` | Không lộ key |

## 🐞 Lỗi / vấn đề còn tồn tại

1. **Chưa chạy thật:**
   - Video AI Gemini Omni (đã có `GEMINI_API_KEY` nhưng chưa chạy lần nào; cách gọi API viết theo SDK `google-genai` 3.0, có thể phải chỉnh)
   - Quét Apify 6 kênh đối thủ hằng ngày
   - Đăng TikTok (còn thiếu `TIKTOK_CLIENT_KEY/SECRET`, nên bấm Duyệt sẽ báo lỗi)
2. **Bot không chạy liên tục:** bot dừng mỗi khi phiên Codespaces kết thúc, hiện **không chạy**, nên lịch 07:00 hằng ngày chưa hoạt động.
3. **Clip Pixabay chưa khớp nội dung:** một số cảnh lệch chủ đề (ví dụ câu về quạt nan ra cảnh chùa Nhật), phần lớn là người và bối cảnh phương Tây.
4. **edge-tts hay lỗi ở lần gọi đầu** ("No audio was received"). Tạm xử lý bằng cách thử lại tối đa 5 lần.
5. **Hạn chế đã biết:**
   - Video AI 360p sẽ mờ khi phóng lên 1080x1920
   - Giữ nhân vật nhất quán mới chỉ dựa vào mô tả chữ, chưa dùng ảnh tham chiếu
   - Chưa hỗ trợ upload lên TikTok video lớn hơn 64MB
   - Thời điểm hiện phụ đề được ước lượng theo độ dài chữ
   - Chế độ `direct` chỉ đăng được riêng tư (SELF_ONLY) cho tới khi app TikTok qua audit

## 📋 3 việc cần làm tiếp theo

1. **Chạy thử thật toàn bộ:** chạy `research` với 6 kênh đối thủ, rồi `run-once` để tạo một video có cảnh AI. Kiểm tra `data/logs/ai_cost.jsonl` và sửa lỗi gọi Gemini nếu có.
2. **Cải thiện độ khớp clip Pixabay:** cho Claude đọc thẻ mô tả của các clip tìm được để chọn clip khớp nhất với lời đọc, và ưu tiên từ khoá bối cảnh châu Á, Việt Nam.
3. **Tạo app trên TikTok for Developers** (Login Kit + Content Posting API, bật Sandbox), chạy `tiktok-auth`, rồi thử bấm Duyệt để đẩy video vào inbox TikTok.

_Sau đó:_ đưa bot lên VPS chạy 24/7, cân nhắc nâng video AI lên 720p, và gửi app TikTok đi audit để đăng thẳng công khai.
