# Tiến độ dự án — TikTok Content Agent

_Cập nhật: 09/11/2026_

## 🎯 Mục tiêu dự án

Xây agent tự động cho kênh TikTok ngách **tuổi thơ, kỷ niệm gia đình, hồi ức thế hệ 8x 9x và năng lượng tích cực** (giúp con cái, vợ chồng nhớ lại những điều quý giá thay vì bị cuốn theo cuộc sống hằng ngày). Mỗi ngày agent:

1. Quét các kênh đối thủ có lượng like, theo dõi lớn
2. Phân tích vì sao nội dung của họ hiệu quả
3. Viết kịch bản mới và tạo video khoảng 30 giây (giọng đọc AI + clip AI 8 giây + phụ đề)
4. Gửi video qua Telegram để chủ kênh duyệt
5. Chỉ đăng lên TikTok sau khi được duyệt

## ✅ Đã làm xong

### 06/11/2026
- Code agent hoàn chỉnh trong `tiktok_agent/`: scraper Apify, Claude phân tích và viết kịch bản, dựng video FFmpeg, bot Telegram duyệt bài, đăng TikTok qua Content Posting API, SQLite, CLI
- Merge vào `main` qua PR #1; bot Telegram kết nối được, đã có `TELEGRAM_CHAT_ID`

### 07/11/2026
- Claude chạy thật thành công; sửa lỗi gửi video Telegram bị timeout
- Đổi Pexels → Pixabay (Pexels khoá API), chạy thật thành công
- Thêm nguồn video AI qua Gemini API (`ai_video.py`); phát hiện Veo trên Gemini API đã bị tắt (22/10/2026) nên dùng Gemini Omni Flash
- Tìm 6 kênh đối thủ qua Apify và đưa vào `config.yaml`
- Tạo trang Terms of Service, Privacy Policy và Redirect URI cho app TikTok bằng GitHub Pages (nhánh `gh-pages`): https://sinhtd1503-png.github.io/Claude--code-lab/
- Chạy thật cả luồng: Apify quét 180 video từ 6 kênh → Claude rút 5 pattern và 3 ý tưởng → video #2 "Túi đồ mẹ gói mỗi lần con đi"
- Thử Gemini API thật: key **chưa có quyền model video** (1.1 và 1.2 báo 404, bản preview lỗi/treo) → cơ chế dự phòng Pixabay hoạt động đúng, không tốn phí

### 08/11/2026
- **Phương án video ~30 giây từ clip AI miễn phí** (`manual_clips.py`):
  - Claude viết đúng 4 cảnh, mỗi cảnh 15–19 từ cho vừa 1 clip 8 giây
  - Bot gửi 4 prompt qua Telegram; chủ kênh tạo clip trên Google Flow (50 credit miễn phí/ngày ≈ 4 clip) và gửi lại cả loạt
  - Bot tự ghép: lời đọc dài hơn clip thì làm chậm clip tối đa 1,25 lần; thiếu clip thì dùng Pixabay
- Chạy thật: video #3 "8x 9x ai từng ngồi ghế gỗ trước xe đạp của bố?" (4 cảnh, ~32 giây), 4 prompt đã gửi lên Telegram. Ghép thử bằng 4 clip giả: video 32,2 giây, dùng đủ 4 clip
- Tăng thời gian chờ thử lại cho edge-tts; sửa prompt dư dấu chấm
- Merge vào `main` qua PR #2, cập nhật `TIEN_DO.md` qua PR #3
- **Triển khai VPS** (PR #4): `deploy/setup.sh` cài bằng 1 lệnh trên Ubuntu (ffmpeg, font, user `agent` không có quyền root, code tại `/opt/tiktok-agent`, service systemd chạy 24/7, tự khởi động lại, dọn video cũ hơn 14 ngày lúc 3:30 sáng); `deploy/tiktok-agent.service`
- Đã **tắt bot trên Codespaces** để chuyển sang VPS (Telegram chỉ cho một bot chạy với cùng một token)

**Trạng thái video (dữ liệu trên Codespaces):** #1 chờ duyệt · #2 đã bỏ · #3 chờ 4 clip từ Google Flow. Bot trên VPS bắt đầu với dữ liệu trống, nên các video này không chuyển sang.

### 09/11/2026 — Chuyển sang n8n, làm hoàn toàn trên iPad
- **Kiến trúc mới (Docker trên 1 VPS):** Caddy (HTTPS miễn phí qua sslip.io) + **n8n 2.44.6** (lịch, Telegram, điều phối) + **worker** FastAPI (`tiktok_agent/worker.py`)
- **Kế hoạch 30 ngày** (`series.py`): Claude quét đối thủ, tạo *series bible* (một gia đình nhân vật cố định, bối cảnh, phong cách) và 30 chủ đề; chủ kênh duyệt hoặc góp ý trên Telegram. Tự lập kế hoạch mới khi còn ≤ 2 ngày
- **Duyệt 2 lần:** 07:00 gửi kịch bản → duyệt/viết lại/góp ý (bot ghi nhớ góp ý) → gửi prompt clip → nhận clip → ghép video → duyệt → đăng
- **Nhân vật nhất quán:** mọi prompt clip lặp nguyên văn mô tả nhân vật/bối cảnh; lệnh `/nhanvat` cho prompt ảnh nhân vật chuẩn dùng làm *Ingredients* trên Flow; sau mỗi clip bot gửi khung hình cuối để làm khung bắt đầu cho cảnh sau
- **Cơ chế duyệt ngân sách** (`budget.py`): Claude, Apify, Gemini có hạn mức tháng; vượt hạn mức → bot gửi yêu cầu duyệt, chưa duyệt thì không tiêu tiền; `/ngansach` xem chi phí
- **Nhiều kênh TikTok:** `tiktok.accounts` trong `config.yaml`, kết nối từng kênh bằng `/tiktok` (OAuth tự động qua `https://<domain>/tiktok/callback`)
- **Cài 1 lệnh:** `deploy/install.sh` (Docker, mở cổng Oracle, tạo khoá nội bộ, nạp + bật workflow n8n, đăng ký webhook Telegram, dọn video cũ). Bỏ `deploy/setup.sh` và service systemd cũ (script mới tự tắt bot cũ)
- **Hướng dẫn cho người mới trên iPad:** `HUONG_DAN.md`
- Sửa lỗi: Claude gọi bằng streaming (kế hoạch 30 ngày vượt giới hạn non-streaming 10 phút)
- **Đã kiểm thử end-to-end trong Docker** với Telegram và Claude giả lập: n8n import/publish workflow qua CLI, lệnh → duyệt ngân sách → kế hoạch → kịch bản → góp ý → duyệt → 4 prompt → nhận clip (reply + album) → gửi ảnh khung cuối → ghép video 27,7 giây có phụ đề tiếng Việt → gửi video qua n8n; chặn chat lạ, sai secret, truy cập file trái phép

## 🧭 Các quyết định quan trọng

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Định dạng video | ~30 giây = 4 cảnh × 1 clip AI 8 giây + giọng đọc + phụ đề | Vừa đúng lượng clip miễn phí mỗi ngày của Google Flow |
| Nguồn clip AI | **Google Flow (miễn phí), tạo tay** (`provider: manual_flow`) | Gemini API không có gói miễn phí cho video. Flow không có API; tự động hoá web Flow vi phạm điều khoản Google, dễ bị khoá tài khoản |
| Clip AI tự động (dự phòng) | Gemini Omni Flash qua API (`provider: gemini_api`), 360p | Veo trên Gemini API đã bị tắt; cần bật thanh toán, ~$0.19/cảnh 8 giây |
| Video stock | Pixabay | Pexels khoá API; dùng khi thiếu clip AI |
| Đối thủ | 4 kênh hoài niệm làm bằng AI + 2 kênh hoài niệm 8x 9x lớn | Kênh AI chưa có kênh nào thật lớn; kênh lớn giúp học chủ đề và hook |
| Cách đăng | `inbox` (mặc định) và `direct` | TikTok chỉ cho đăng công khai qua API sau khi app qua audit |
| Điều phối | **n8n** (theo yêu cầu chủ kênh) + worker Python | n8n dễ xem/chỉnh trên iPad; logic phức tạp (Claude, FFmpeg, TikTok) nằm trong Python để kiểm thử được |
| Chạy 24/7 | VPS Ubuntu + Docker Compose (Caddy, n8n, worker), cài bằng `deploy/install.sh` | Một lệnh, chạy lại để cập nhật; HTTPS miễn phí bằng sslip.io |
| Kế hoạch | 30 ngày, một gia đình nhân vật cố định | Người xem nhận ra "gia đình" của kênh; clip AI nhất quán hơn |
| Chi phí | Mọi dịch vụ trả phí qua cổng duyệt ngân sách | Theo yêu cầu: báo cáo và duyệt trước khi tiêu |
| Trang pháp lý TikTok | GitHub Pages, liên hệ bằng email chủ kênh | Miễn phí, https, không ảnh hưởng code |
| Nội dung | Học pattern từ đối thủ, viết nội dung mới | Tránh bản quyền và bị giảm phân phối |
| Mô hình viết | Claude Opus 5.5, bật fallback phía server | Viết tiếng Việt tốt |
| Giọng đọc | edge-tts (`vi-VN-HoaiMyNeural`) | Miễn phí, giọng Việt tự nhiên |
| Bảo mật | Key chỉ trong `.env` (nằm trong `.gitignore`), không in ra log; bot chỉ nhận lệnh từ `TELEGRAM_CHAT_ID` | Không lộ key |

## 🐞 Lỗi / vấn đề còn tồn tại

1. **Chưa chạy trên VPS thật và Telegram/Claude thật với bản n8n** (mới kiểm thử bằng dịch vụ giả lập trong Docker).
2. **Vẫn còn bước làm tay mỗi ngày:** tạo 3–4 clip trên Google Flow (~5 phút). Tự động hoàn toàn chỉ có qua Gemini API trả phí (key hiện chưa có quyền model video).
3. **Đăng TikTok chưa chạy thật:** cần tạo app, thêm tài khoản vào Sandbox, kết nối bằng `/tiktok`. `direct` chỉ đăng riêng tư tới khi app qua audit.
4. **edge-tts lỗi theo đợt** ("No audio was received") — đã có thử lại + nút 🔁 Thử lại trên Telegram.
5. **Hạn chế đã biết:** độ nhất quán nhân vật phụ thuộc Flow (prompt cố định + ảnh nhân vật + khung cuối); Telegram chỉ cho bot tải file ≤ 20 MB; chưa hỗ trợ upload TikTok > 64 MB; phụ đề căn thời gian theo ước lượng; số lượt Flow miễn phí do Google quyết định và có thể thay đổi.

## 📋 3 việc cần làm tiếp theo

1. **Merge PR, thuê VPS và chạy `deploy/install.sh`** theo `HUONG_DAN.md` (Bước 4–7), kiểm tra `/start` trên Telegram.
2. **Chạy thật 1 vòng:** `/kehoach` → duyệt ngân sách + kế hoạch → `/nhanvat` → `/kichban` → tạo 4 clip trên Flow → duyệt video.
3. **Kết nối TikTok** (Bước 8) và đăng thử vào hộp nháp; sau đó gửi app đi audit để đăng thẳng.
