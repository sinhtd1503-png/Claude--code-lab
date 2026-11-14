# Tiến độ dự án — TikTok Content Agent

_Cập nhật: 14/11/2026_

## 🔄 Lệnh cập nhật VPS (sao chép và dán vào Termius)

Mỗi khi có bản mới trên GitHub, chạy lệnh này trên VPS (đang đăng nhập `root`):

```bash
curl -fsSL https://raw.githubusercontent.com/sinhtd1503-png/Claude--code-lab/main/deploy/install.sh | UPDATE_WORKFLOW=1 bash
```

Nếu lệnh trên báo lỗi khi tải code (do từng sửa `config.yaml` trên VPS), chạy lệnh này trước rồi chạy lại lệnh trên:

```bash
cd /opt/tiktok-agent && git checkout config.yaml
```

Kiểm tra hệ thống sau khi cập nhật (3 dòng `caddy`, `n8n`, `worker` đều **Up**):

```bash
cd /opt/tiktok-agent && docker compose ps
```

Xem lỗi gần nhất của worker (chép phần chữ lỗi gửi Claude):

```bash
cd /opt/tiktok-agent && docker compose logs --tail 40 worker
```

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

### 10–11/11/2026 — Chọn VPS và chuẩn bị cài thật
- **PR #6 (bản n8n) đã merge vào `main`**: lệnh cài 1 dòng đã lấy đúng bản mới
- Chủ kênh đã chuẩn bị 3 khoá trong app Ghi chú: token bot Telegram, Claude API key (đã nạp $5), Apify token
- **Thử VPS:** Oracle Cloud Free bị tắc; VPS Contabo đi mượn (178.238.224.220) có sẵn web `chaoem.xyz` (nginx) chiếm cổng 80/443 nên hệ thống chưa cài được ở đó, bỏ hướng này
- **Quyết định: chủ kênh tự thuê VPS Contabo** (Cloud VPS 10, Ubuntu 24.04, EU, kỳ 1 tháng, khoảng 5–7 €). **Đã thuê xong 11/11/2026**
- Thêm **`CAI_DAT_CONTABO.md`** (PR #7, đã merge): sổ tay 8 bước, mỗi lệnh một khung để bấm sao chép trên iPad; kèm trang hướng dẫn có nút sao chép trên claude.ai
- Đang ở **Bước 2–3**: kết nối Termius (user `root`) và chạy `deploy/install.sh`

**Tóm tắt hôm nay (11/11/2026):**
- ✅ Thuê xong VPS Contabo
- ✅ Sổ tay cài đặt `CAI_DAT_CONTABO.md` (PR #7) và cập nhật tiến độ (PR #8) đã merge vào `main`
- ✅ Đã có hướng dẫn chi tiết cấu hình **TikTok Login Kit + Content Posting API** (xem mục dưới)
- ⏳ Chưa làm: kết nối Termius và chạy lệnh cài trên VPS mới

### 🔧 Ghi chú cấu hình TikTok (Bước 7)
- **App:** developers.tiktok.com → Manage apps → Connect an app (Individual). Icon 1024×1024, Category Entertainment, Platform **Web**
- **Terms:** `https://sinhtd1503-png.github.io/Claude--code-lab/terms.html` · **Privacy:** `https://sinhtd1503-png.github.io/Claude--code-lab/privacy.html` · **Web URL:** `https://sinhtd1503-png.github.io/Claude--code-lab/`
- **Login Kit → Redirect URI (Web):** `https://<ip-gạch-ngang>.sslip.io/tiktok/callback`, khớp tuyệt đối, không có `/` cuối
- **Content Posting API:** bật **Direct Post**; bỏ qua Verify domain (hệ thống upload file trực tiếp)
- **Scopes:** `user.info.basic`, `video.upload`, `video.publish` (đủ cả 3, bấm Save)
- **Sandbox:** tạo Sandbox, cấu hình lại như trên, thêm tài khoản kênh vào **Target users**; dùng **Client key/secret của Sandbox** (khác Production)
- Điền `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` vào `/opt/tiktok-agent/.env` → chạy lại lệnh cài → Telegram `/tiktok` → cho phép → `/trangthai`
- Đăng công khai thẳng: Production → Submit for review; sau khi duyệt dùng khoá Production và đổi `post_mode: direct`

### 12–13/11/2026 — Đã test full luồng; đổi hướng theo kênh mẫu @kyuc.tuoitho.8x9x
- Chủ kênh đã **cài xong trên VPS Contabo và test full luồng** (kế hoạch → kịch bản → clip → video → TikTok Sandbox). Nội dung và kênh quét chưa đúng ý → đổi hướng theo kênh mẫu **@kyuc.tuoitho.8x9x**
- Kênh mẫu (theo mô tả của chủ kênh; TikTok chặn công cụ đọc tự động nên chưa xem trực tiếp được): clip AI chuyển động, **giọng đọc + nhạc nền**, phụ đề theo lời đọc, **15–30 giây**
- **Nội dung:** ngách đổi thành "Ký ức tuổi thơ 8x 9x": mỗi video là MỘT ký ức cụ thể (trò chơi, quà vặt, đồ vật, trường lớp, Tết, làng quê, gia đình), kể bằng chi tiết giác quan, kết bằng cảm xúc ấm áp; vẫn tích cực
- **Kênh mẫu được ưu tiên:** `reference_channels` trong `config.yaml`; khi lập kế hoạch, video của kênh mẫu được phân tích riêng và ưu tiên. Bỏ 2 kênh hoạt hình khỏi danh sách tham khảo
- **Mô tả phong cách** (`niche.style_reference`) đưa vào mọi prompt; sửa ngay trên Telegram bằng `/phongcach`
- **Độ dài:** 3 cảnh × 8 giây ≈ 22–26 giây
- **Nhạc nền:** gửi file mp3/m4a vào chat → lưu thư viện `data/music`; mỗi video xoay vòng 1 bài, nhạc tự nhỏ lại khi có giọng đọc, fade đầu/cuối; `/nhac` xem và xoá
- Đã kiểm thử trong Docker: lọc kênh mẫu, prompt, ghép video có nhạc (có/không ducking), nhận/xoá nhạc, `/phongcach`, n8n chuyển file nhạc tới worker

### 13/11/2026 — Gửi ảnh nhân vật qua Telegram
- Gửi **ảnh minh hoạ nhân vật** vào bot, kèm chú thích tên và vai (vd `Bé Na - con gái út 8 tuổi`) → Claude đọc ảnh (vision) và viết mô tả ngoại hình cố định bằng tiếng Anh, lưu vào danh sách nhân vật mẫu (tốn khoảng $0.05/ảnh, qua cổng ngân sách)
- Kế hoạch mới (`/kehoach`) **bắt buộc dùng đúng các nhân vật này** (giữ nguyên văn mô tả); nút **"Dùng cho kế hoạch đang chạy"** để áp dụng ngay; gửi lại ảnh cùng tên để thay; nút 🗑 để xoá
- `/nhanvat` gửi lại các ảnh mẫu để lưu và dùng làm **Ingredients** trong Google Flow
- Nhận cả ảnh gửi dạng file (PNG/JPG không nén). Đã kiểm thử trong Docker

### 13/11/2026 — Âm thanh liền mạch, duyệt cả giọng đọc + nhạc; chọn nhân vật bằng ảnh trước khi lập kế hoạch
- **Giọng đọc đọc một lần cho cả video** (`tiktok_agent/audio.py`) thay vì từng cảnh rời nhau → ngữ điệu liền mạch; thời điểm từng từ (edge-tts WordBoundary) dùng để **cắt cảnh theo giọng đọc** và làm **phụ đề khớp lời**, ngắt dòng ở dấu phẩy/dấu chấm
- **Nhạc nền chạy suốt video**, tự hạ nhỏ khi có giọng đọc, fade đầu/cuối
- **Duyệt âm thanh cùng kịch bản:** sau kịch bản bot gửi **bản nghe thử** (chính là âm thanh của video thành phẩm) với nút **✅ Duyệt kịch bản & âm thanh**, **🎵 Đổi nhạc** (xoay vòng thư viện + không nhạc), **🎙 Đổi giọng** (`voice.options`), viết lại/bỏ. Viết lại theo góp ý giữ nguyên lựa chọn nhạc và giọng. Video ghép dùng đúng file giọng đọc đã duyệt
- Nếu edge-tts lỗi lúc tạo bản nghe thử: vẫn duyệt được kịch bản bằng chữ, giọng đọc sẽ tạo lại lúc ghép video
- **`/kehoach` hỏi chọn nhân vật bằng ảnh trước:** chưa có ảnh nhân vật → bot hướng dẫn gửi ảnh (chú thích tên – vai) hoặc bấm "✨ Để AI tự tạo nhân vật"; đã có ảnh → lập kế hoạch với đúng các nhân vật đó
- Đã kiểm thử trong Docker (căn thời gian, phụ đề, bản nghe thử, đổi nhạc/giọng, dùng lại giọng đã duyệt khi ghép) và thử edge-tts thật với giọng tiếng Việt

### 📌 Tóm tắt thay đổi hôm nay (14/11/2026)
- **Lý do:** chủ kênh thấy giọng đọc thuyết minh khô khan; muốn video là **nhân vật thoại với nhau**, có cảm xúc, nhạc nền nhẹ, hình **anime điện ảnh**, và **tự tạo clip** sau khi duyệt
- **Đã làm (PR #18, đã merge vào `main`):**
  - Kịch bản dạng thoại: mỗi cảnh 8 giây có 1–3 câu thoại (ai nói, nói gì, cảm xúc), không còn người dẫn chuyện; mỗi nhân vật có giọng nói cố định
  - Prompt clip chứa sẵn thoại tiếng Việt + giọng nhân vật + yêu cầu khớp khẩu hình, để video AI tự lồng tiếng
  - Phong cách anime điện ảnh cố định cho mọi kế hoạch, clip, ảnh nhân vật
  - Ảnh nhân vật gửi 1 lần là tự áp dụng cho kế hoạch đang chạy và mọi kế hoạch sau
  - Duyệt kịch bản: xem thoại từng cảnh + nghe thử nhạc nền, đổi nhạc bằng nút
  - Ghép video giữ tiếng thoại trong clip, nhạc tự hạ khi nhân vật nói, phụ đề theo thoại; clip không có tiếng thì dùng giọng dự phòng
  - Tự tạo clip bằng Gemini API sau khi duyệt (trả phí, luôn hỏi duyệt ngân sách); hết hạn mức/credit thì báo trên Telegram, có nút thử lại và prompt tạo tay
  - Lệnh cài hỏi thêm `GEMINI_API_KEY` (tuỳ chọn); thêm mục "Lệnh cập nhật VPS" ở đầu file này (PR #17)
- **Chưa làm được:** tự động dùng trang tạo video miễn phí — không có trang miễn phí nào cho gọi tự động hợp lệ; tự động hoá web vi phạm điều khoản. Miễn phí vẫn là tạo tay trên Google Flow
- **Rủi ro cần kiểm tra khi chạy thật:** chất lượng tiếng Việt và khớp khẩu hình do video AI tạo; Gemini có trả về tiếng trong clip hay không (nếu không sẽ dùng giọng dự phòng, kém cảm xúc hơn)

### 14/11/2026 — Video thoại giữa các nhân vật, phong cách anime điện ảnh, tự tạo clip sau khi duyệt
- **Bỏ hoàn toàn giọng đọc thuyết minh.** Kịch bản mới (`series.py`): mỗi cảnh 8 giây có 1–3 câu thoại giữa các nhân vật (người nói, câu thoại, cảm xúc, cách nói), âm thanh môi trường, `music_mood`. Mỗi nhân vật có **giọng nói cố định** (`voice_en`, `voice_type`)
- **Prompt clip có sẵn thoại tiếng Việt** + giọng từng nhân vật + yêu cầu khớp khẩu hình → video AI (Flow/Gemini) tự lồng tiếng nhân vật; không có nhạc trong clip, không người dẫn chuyện
- **Phong cách hình ảnh cố định "anime điện ảnh"** (`niche.visual_style_en`) áp vào mọi kế hoạch, kịch bản, prompt, ảnh nhân vật
- **Ảnh nhân vật gửi 1 lần, dùng mãi:** mọi bible (kế hoạch đang chạy và các kế hoạch sau) tự áp nhân vật từ ảnh + phong cách cố định (`fix_bible`); không cần bấm áp dụng
- **Duyệt kịch bản:** bot gửi thoại từng cảnh + **nhạc nền nghe thử** với nút Duyệt kịch bản & nhạc / Đổi nhạc / Viết bản khác / Bỏ
- **Ghép video** (`dialogue.py`): giữ tiếng thoại trong clip, nhạc nền nhẹ tự hạ khi nhân vật nói, phụ đề theo câu thoại. Clip không có tiếng → giọng dự phòng edge-tts theo từng nhân vật (trẻ em/người lớn/người già, `voice.fallback`)
- **Tự tạo clip sau khi duyệt** (`ai_video.auto_generate`): có `GEMINI_API_KEY` → tự gọi Gemini API, luôn qua cổng duyệt ngân sách; từ chối ngân sách → gửi prompt tạo tay; **hết hạn mức/credit** giữa chừng → bot báo trên Telegram, nút "Thử tạo tự động lại" + prompt tạo tay. Lệnh cài hỏi thêm `GEMINI_API_KEY` (tuỳ chọn)
- **Chưa có trang tạo video miễn phí có API hợp lệ:** Flow/Kling/Hailuo… bản miễn phí chỉ dùng trên web; tự động hoá web vi phạm điều khoản, dễ bị khoá tài khoản → không làm. Miễn phí vẫn là tạo tay trên Flow
- Kịch bản cũ dạng thuyết minh vẫn ghép được như trước. Đã kiểm thử trong Docker (prompt thoại, nhạc nghe thử, ghép video giữ tiếng clip + giọng dự phòng, tự tạo clip, duyệt/từ chối ngân sách, hết hạn mức)

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
| Chạy 24/7 | VPS **Contabo** (trả phí khoảng 5–7 €/tháng, chủ kênh tự thuê) + Docker Compose (Caddy, n8n, worker), cài bằng `deploy/install.sh` | Một lệnh, chạy lại để cập nhật; HTTPS miễn phí bằng sslip.io |
| Kế hoạch | 30 ngày, một gia đình nhân vật cố định | Người xem nhận ra "gia đình" của kênh; clip AI nhất quán hơn |
| Chi phí | Mọi dịch vụ trả phí qua cổng duyệt ngân sách | Theo yêu cầu: báo cáo và duyệt trước khi tiêu |
| Trang pháp lý TikTok | GitHub Pages, liên hệ bằng email chủ kênh | Miễn phí, https, không ảnh hưởng code |
| Nội dung | Học pattern từ đối thủ, viết nội dung mới | Tránh bản quyền và bị giảm phân phối |
| Mô hình viết | Claude Opus 5.5, bật fallback phía server | Viết tiếng Việt tốt |
| Giọng đọc | edge-tts (`vi-VN-HoaiMyNeural`) | Miễn phí, giọng Việt tự nhiên |
| Bảo mật | Key chỉ trong `.env` (nằm trong `.gitignore`), không in ra log; bot chỉ nhận lệnh từ `TELEGRAM_CHAT_ID` | Không lộ key |

## 🐞 Lỗi / vấn đề còn tồn tại

1. **Phong cách kênh mẫu mới dựa trên mô tả**, chưa đối chiếu trực tiếp với video mẫu; cần chủ kênh xem kết quả và chỉnh bằng `/phongcach`.
2. **Vẫn còn bước làm tay mỗi ngày:** tạo 3–4 clip trên Google Flow (~5 phút). Tự động hoàn toàn chỉ có qua Gemini API trả phí (key hiện chưa có quyền model video).
3. **Đăng TikTok chưa chạy thật:** cần tạo app, thêm tài khoản vào Sandbox, kết nối bằng `/tiktok`. `direct` chỉ đăng riêng tư tới khi app qua audit.
4. **edge-tts lỗi theo đợt** ("No audio was received") — đã có thử lại + nút 🔁 Thử lại trên Telegram.
5. **Hạn chế đã biết:** độ nhất quán nhân vật phụ thuộc Flow (prompt cố định + ảnh nhân vật + khung cuối); Telegram chỉ cho bot tải file ≤ 20 MB; chưa hỗ trợ upload TikTok > 64 MB; phụ đề căn thời gian theo ước lượng; số lượt Flow miễn phí do Google quyết định và có thể thay đổi.

## 📋 Các bước cần cấu hình tiếp

1. **Cập nhật VPS** bằng lệnh ở mục "🔄 Lệnh cập nhật VPS" đầu file; khi được hỏi Gemini API key: dán khoá (tự tạo clip, trả phí) hoặc Enter (tạo tay trên Flow).
2. **Thêm 3–5 bài nhạc nền nhẹ, không bản quyền** bằng cách gửi file vào chat bot; kiểm tra `/nhac`.
3. **Gửi ảnh nhân vật** (mỗi ảnh một tin, chú thích `Tên - vai`) — chỉ 1 lần.
4. `/kehoach` → duyệt kế hoạch mới (để có giọng cố định cho nhân vật) → `/kichban` → xem thoại + nghe nhạc → duyệt → nhận video.
5. Đánh giá video đầu tiên: giọng nhân vật, nhịp thoại, khớp khẩu hình, âm lượng nhạc → báo lại để chỉnh.
6. Huỷ hoặc gia hạn Contabo trước ngày hết kỳ 1 tháng.
