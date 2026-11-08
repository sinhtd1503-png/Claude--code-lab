# Tiến độ dự án — TikTok Content Agent

_Cập nhật: 08/11/2026_

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

### Hôm nay (08/11/2026)
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

## 🧭 Các quyết định quan trọng

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Định dạng video | ~30 giây = 4 cảnh × 1 clip AI 8 giây + giọng đọc + phụ đề | Vừa đúng lượng clip miễn phí mỗi ngày của Google Flow |
| Nguồn clip AI | **Google Flow (miễn phí), tạo tay** (`provider: manual_flow`) | Gemini API không có gói miễn phí cho video. Flow không có API; tự động hoá web Flow vi phạm điều khoản Google, dễ bị khoá tài khoản |
| Clip AI tự động (dự phòng) | Gemini Omni Flash qua API (`provider: gemini_api`), 360p | Veo trên Gemini API đã bị tắt; cần bật thanh toán, ~$0.19/cảnh 8 giây |
| Video stock | Pixabay | Pexels khoá API; dùng khi thiếu clip AI |
| Đối thủ | 4 kênh hoài niệm làm bằng AI + 2 kênh hoài niệm 8x 9x lớn | Kênh AI chưa có kênh nào thật lớn; kênh lớn giúp học chủ đề và hook |
| Cách đăng | `inbox` (mặc định) và `direct` | TikTok chỉ cho đăng công khai qua API sau khi app qua audit |
| Chạy 24/7 | VPS Ubuntu + systemd, cài bằng `deploy/setup.sh`, chạy bằng user không có quyền root | Không phụ thuộc Codespaces; tự khởi động lại khi lỗi/reboot; cập nhật bằng cách chạy lại script |
| Trang pháp lý TikTok | GitHub Pages, liên hệ bằng email chủ kênh | Miễn phí, https, không ảnh hưởng code |
| Nội dung | Học pattern từ đối thủ, viết nội dung mới | Tránh bản quyền và bị giảm phân phối |
| Mô hình viết | Claude Opus 5.5, bật fallback phía server | Viết tiếng Việt tốt |
| Giọng đọc | edge-tts (`vi-VN-HoaiMyNeural`) | Miễn phí, giọng Việt tự nhiên |
| Bảo mật | Key chỉ trong `.env` (nằm trong `.gitignore`), không in ra log; bot chỉ nhận lệnh từ `TELEGRAM_CHAT_ID` | Không lộ key |

## 🐞 Lỗi / vấn đề còn tồn tại

1. **Chưa chạy thật:**
   - Ghép clip thật từ Google Flow (mới thử bằng clip giả)
   - Đăng TikTok: đã có `TIKTOK_CLIENT_KEY/SECRET` và trang pháp lý nhưng **chưa chạy `tiktok-auth`**
   - Gemini API tự động: key chưa có quyền model video (cần bật thanh toán trên Google AI Studio)
2. **Vẫn còn một bước làm tay mỗi ngày:** tạo 4 clip trên Google Flow (khoảng 5 phút). Muốn tự động hoàn toàn phải dùng Gemini API trả phí.
3. **VPS chưa được kiểm chứng:** script triển khai mới kiểm tra cú pháp, chưa chạy trên VPS thật. Bot trên Codespaces đã tắt, nên **hiện không có bot nào chạy** cho tới khi VPS khởi động xong.
4. **edge-tts lỗi theo đợt** ("No audio was received"); đã có thử lại với thời gian chờ tăng dần (tổng ~1,5 phút).
5. **Hạn chế đã biết:**
   - Clip Pixabay (khi thiếu clip AI) thường không khớp nội dung và mang bối cảnh phương Tây
   - Giữ nhân vật nhất quán giữa các clip chỉ dựa vào mô tả chữ trong prompt
   - Số credit Flow miễn phí (50/ngày) lấy từ nguồn bên ngoài, Google không công bố chính thức
   - Chưa hỗ trợ upload lên TikTok video lớn hơn 64MB; phụ đề căn thời gian theo ước lượng
   - Chế độ `direct` chỉ đăng được riêng tư (SELF_ONLY) cho tới khi app TikTok qua audit

## 📋 3 việc cần làm tiếp theo

1. **Hoàn tất VPS**: thuê VPS Ubuntu, chạy lệnh cài trong `deploy/setup.sh`, chép `.env`, `systemctl start tiktok-agent`, kiểm tra bằng `/status` trên Telegram và xem log `journalctl -u tiktok-agent -f`.
2. **Chạy thử trên VPS với clip thật**: gửi `/run`, tạo 4 clip trên Google Flow theo prompt, gửi cho bot và kiểm tra video ~30 giây ghép từ clip thật.
3. **Kết nối TikTok trên VPS**: `cd /opt/tiktok-agent && sudo -u agent .venv/bin/python -m tiktok_agent.main tiktok-auth`, rồi bấm ✅ Duyệt một video để thử đẩy vào hộp nháp TikTok.

_Sau đó:_ gửi app TikTok đi audit để đăng thẳng; cân nhắc bật thanh toán Gemini nếu muốn bỏ bước tạo clip bằng tay.
