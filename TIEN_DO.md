# Tiến độ dự án — TikTok Content Agent

_Cập nhật: 26/11/2026_

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

### 📌 Tóm tắt thay đổi hôm nay (26/11/2026) — Lời răn dạy: chọn thời lượng, giọng AI theo mô tả, duyệt nhạc; mô tả font từng phần
- **Yêu cầu của chủ kênh:**
  - Chủ đề 3: chọn thời lượng trước khi viết.
  - Mô tả giọng đọc bằng lời và nghe thử, không chỉ chỉnh tốc độ và độ trầm của giọng cũ.
  - Duyệt font: mô tả riêng cho chữ đầu, phụ đề, câu kết.
  - Thêm bước duyệt nhạc nền, hoặc mô tả nhạc nền.
- **Đã làm (PR #43):**
  - **Chọn thời lượng:** 30 giây / 45 giây / 1 phút / 90 giây / 2 phút (`/kichban 3 90`). Số chữ và số câu tính theo thời lượng; viết lại giữ nguyên thời lượng.
  - **Giọng đọc (bước 4/5, nghe riêng giọng):**
    - **✍️ Mô tả giọng** → Claude chọn 1 trong 30 giọng **Gemini TTS** kèm chỉ dẫn cách đọc. Hệ thống đọc từng câu để phụ đề khớp từng câu, rồi gửi nghe thử.
    - Có 2 giọng AI mẫu (Ông cụ kể chuyện, Người mẹ dịu dàng). Ngân sách `gemini_tts` khoảng $0,02/phút.
    - Gemini TTS lỗi thì giữ giọng cũ và báo lý do. Chưa có key thì vẫn chỉ chỉnh nam/nữ, độ trầm, tốc độ.
  - **Bước 5/5 – nhạc nền (mới):** nghe giọng + nhạc như video thật; nút 🎵 Bài khác, 🔇 Không nhạc, ✍️ Mô tả nhạc (chọn bài trong thư viện theo tên/không khí, chỉnh to nhỏ).
  - **Font (cả 3 chủ đề):** nút ✍️ Tả font đầu / phụ đề / kết; trả lời mô tả thì chỉ phần đó đổi.
- **Kiểm thử:** thêm `test_loiday2` (29 kiểm tra), cập nhật `test_wisdom`. PR #43 tự merge khi cả 15 bộ test đạt.

### 📌 Tóm tắt thay đổi 25/11/2026 — Chủ đề 3 · Những lời răn dạy; sửa tạo ảnh; đăng lại TikTok; gõ mô tả cho cả 3 chủ đề
- **Chủ đề 3 · Những lời răn dạy (PR #41):**
  - Không thoại nhân vật: một ảnh nền, một giọng đọc trầm ấm đọc triết lý sống về gia đình, phụ đề theo lời đọc, nhạc nhẹ. Không cần tạo clip trên Flow.
  - Duyệt lần lượt: kịch bản → ảnh nền → chữ & phụ đề → giọng đọc → video.
  - **Ảnh nền:** Gemini, hoặc Pixabay; có thể gửi ảnh của mình hoặc mô tả ảnh muốn có.
  - **Chữ & phụ đề:** ảnh minh hoạ dựng trên chính ảnh nền; đổi được font, kiểu phụ đề (màu, cỡ, nền mờ, vị trí), hoặc mô tả.
  - **Giọng đọc:** 4 giọng mẫu, hoặc mô tả.
  - **Video:** ảnh zoom chậm, phụ đề hiện đúng câu đang đọc, chữ ảnh bìa, câu kết.
  - Phát hiện và sửa khi kiểm tra: phụ đề có kiểu mới bị đẩy ra ngoài khung (libass tính theo khung 288 đơn vị); test giờ kiểm tra phụ đề thật sự hiện.
- **Sửa lỗi và bổ sung (PR #42):**
  - **Lỗi tạo ảnh** "only supported in Gemini Enterprise Agent Platform mode":
    - Nguyên nhân: Imagen chỉ chạy trên Vertex AI. Chuyển sang `gemini-2.5-flash-image`.
    - Khi lỗi, bot gửi **✋ Tạo ảnh nền bằng tay** kèm prompt; chủ kênh trả lời đúng tin đó bằng ảnh.
  - **Đăng TikTok không thấy trong hộp thư:**
    - Bot giờ đọc **trạng thái thật** từ TikTok (đã vào hộp thư / đã đăng / đang xử lý / lỗi + lý do + cách sửa).
    - Luôn có nút **🔁 Đăng lại TikTok** và **🔎 Kiểm tra trạng thái**. Video đăng lỗi hiện trong `/trangthai`.
  - **Gõ mô tả cho cả 3 chủ đề:**
    - Nhạc nền: trả lời tin nhạc, ví dụ "piano buồn, to hơn", "bỏ nhạc".
    - Phụ đề: chủ đề 1 và 2 có thêm 🎨 và mô tả.
    - Hình ảnh chủ đề 1 và 2: trả lời tin prompt của một cảnh hoặc đoạn kéo dài bằng mô tả, Claude viết lại prompt đó.
- **Kiểm thử:** thêm `test_wisdom` và `test_fixes`; 13/13 rồi 14/14 bộ test đạt trước khi merge.

### 📌 Tóm tắt thay đổi 23/11/2026 — Chọn chủ đề kịch bản: 1 Ký ức tuổi thơ / 2 Gia đình
- **Yêu cầu của chủ kênh:**
  - Khi đến bước viết kịch bản hằng ngày, hoặc khi gõ `/kichban`, được chọn chủ đề.
  - **Chủ đề 1:** ký ức tuổi thơ, làm như hiện tại.
  - **Chủ đề 2:** những điều cần nhắc nhở trong gia đình (bố mẹ – con cái – ông bà) và những thứ dễ bị lãng quên trong xã hội hiện đại. Mục tiêu là người xem lắng lại, hướng về chân – thiện – mỹ, và muốn chia sẻ cho người thân.
  - Nhân vật chủ đề 2 có thể điều chỉnh cho hợp, nhưng thoại, hình ảnh, bối cảnh phải nhất quán.
- **Đã làm (PR #38):**
  - `/kichban` và lịch 07:00 hỏi chủ đề bằng 2 nút **1️⃣ Ký ức tuổi thơ** / **2️⃣ Gia đình**; gõ nhanh `/kichban 1`, `/kichban 2`.
  - Đặt `topics: ask: false` thì bot luôn dùng chủ đề 1 như trước.
  - Chủ đề 1 không đổi gì: vẫn theo kế hoạch 30 ngày, bối cảnh thập niên 1990.
  - **Chủ đề 2:**
    - Không cần kế hoạch 30 ngày: mỗi lần lấy trụ cột tiếp theo (8 trụ cột: lời cảm ơn, bữa cơm đủ mặt, thời gian bên ông bà, lắng nghe, điện thoại và sự kết nối, hy sinh của bố mẹ, hiếu thảo, anh chị em).
    - Claude tự chọn tình huống gia đình ngày nay và tránh trùng các video chủ đề 2 đã làm.
    - Giọng văn lắng đọng, không giáo điều, kết bằng một hành động nhỏ ấm áp; caption mời chia sẻ cho người thân.
  - **Nhân vật hiện đại ở chủ đề 2** (chủ kênh nhận xét lần đầu chỉ đổi trang phục là chưa đủ):
    - Cùng gia đình 6 nhân vật, cùng tên và giọng, nhưng ở **phiên bản hiện đại**: vai, ngoại hình + trang phục, cách nói theo cuộc sống ngày nay (`topics.giadinh.cast`).
    - Ví dụ: ông bà nghỉ hưu sống cùng con cháu; bố đi làm văn phòng; mẹ nhắc bỏ điện thoại khi ăn cơm; Cu Tí học cấp hai, cầm điện thoại; Bé Na học tiểu học.
    - **Không dùng ảnh mẫu thời xưa** cho chủ đề 2 (để Flow khỏi chép quần áo cũ). Ảnh mẫu hiện đại riêng: gửi ảnh chú thích `2 Bà` / `Bà hiện đại`.
    - `/nhanvat 2` hiện thẻ hiện đại kèm prompt tạo ảnh. Trả lời thẻ bằng `Vai/Ngoại hình/Cách nói` để sửa (lưu trong `nhan_vat.yaml`, mục `modern`).
    - **Một ngôi nhà ngày nay cố định** (`setting_en`), ghi nguyên văn trong mọi prompt clip. Prompt clip mô tả phim theo chủ đề, không còn câu cố định "1990s childhood memory".
  - Lý do chủ kênh thấy `/kichban` vẫn viết chủ đề ký ức: lúc đó PR #38 chưa merge nên VPS còn chạy bản cũ (n8n không lỗi).
- **Trạng thái cuối ngày:** PR #38 (chọn chủ đề + nhân vật hiện đại) và PR #39 (tiến độ) **đã merge**; chủ kênh **đã cập nhật VPS**. Việc tiếp theo: thử `/kichban` → chọn 2️⃣, tạo ảnh mẫu hiện đại bằng `/nhanvat 2`, làm trọn 1 video chủ đề 2.
  - Viết lại kịch bản vẫn giữ đúng chủ đề; tin kịch bản ghi rõ **🕰 Chủ đề 1** hay **👨‍👩‍👧 Chủ đề 2**.
  - Mọi nội dung chủ đề 2 sửa được trong `config.yaml`, mục `topics:`.
- **Kiểm thử:**
  - Thêm bộ test chủ đề, gồm cả phần nhân vật hiện đại (ảnh chú thích `2 Bà`, sửa thẻ hiện đại, `/nhanvat 2`, chủ đề 2 không dùng ảnh thời xưa); đạt hết.
  - Sửa bộ test tần suất (lịch 07:00 giờ hỏi chủ đề trước).
  - PR #38 được merge theo yêu cầu khi lượt chạy lại toàn bộ test mới xong một phần; sau đó cả **12/12 bộ test đều đạt**.

### 📌 Tóm tắt thay đổi 20/11/2026 — Chọn "kéo dài" (1 video, không ghép) hoặc "clip độc lập"; ghép không làm lệch giọng
- **Yêu cầu của chủ kênh:**
  - Cách kéo dài (Extend) trong Flow đã cho ra một video hoàn chỉnh, rất khó tải từng đoạn về rồi gửi lại theo đúng thứ tự.
  - Muốn chọn giữa kéo dài và clip độc lập. Chọn kéo dài thì chỉ gửi lại 1 video cuối, không cần ghép.
  - Clip đã cắt sẵn, nghe đã giống nhau, nhưng ghép bằng code thì giọng lại lệch.
  - Trong mọi trường hợp phải giữ nhân vật, bối cảnh, giọng nói nhất quán.
- **Đã làm (PR #36):**
  - Bước 4/4 có 3 lựa chọn: 🤖 Gemini tự động · 🎞 **Kéo dài – gửi 1 video** · 🧩 **Clip độc lập – gửi từng clip**.
  - **Kéo dài:**
    - Bot gửi prompt đầy đủ cho Đoạn 1, các prompt kéo dài cho đoạn 2, 3…, và tin **📥 Gửi video kéo dài**.
    - Chủ kênh trả lời tin đó bằng đúng 1 video. Bot không cắt, không ghép, không chỉnh giọng; chỉ thêm nhạc nền, phụ đề, chữ ảnh bìa và câu kết.
    - Phụ đề tự đặt theo chỗ ngắt lời của từng cảnh.
  - **Clip độc lập:** mỗi cảnh một prompt 8 giây, không còn prompt kéo dài lẫn vào. Có nút đổi cách làm; `/clip N` gửi đúng loại prompt.
  - **Sửa giọng bị lệch khi ghép:**
    - Khớp giọng giờ chỉ so các câu trong **cùng video** (`match_memory: false`), không kéo về giọng của video trước.
    - Lệch dưới 1 nửa cung hoặc dưới 3 dB thì để nguyên. Câu không chắc ai nói thì không bao giờ chỉnh.
  - **Chỗ nối giữa các clip:** độ mờ âm thanh giảm từ 60 ms xuống 3 ms nên hết hụt tiếng ở chỗ nối.
  - Prompt kéo dài nhắc lại ngoại hình từng nhân vật và yêu cầu giữ đúng giọng như đoạn trước.
- **Sửa thêm:** video không có câu thoại nào giờ dựng được, trước đây ffmpeg báo lỗi vì file phụ đề rỗng.
- **Kiểm thử:** 11 bộ test đều đạt (video tổng hợp). Video kéo dài giữ nguyên giọng (lệch < 0,3 dB) và 6 phụ đề khớp lời. Chưa thử với video thật từ Flow.

### 📌 Tóm tắt thay đổi 19/11/2026 — Duyệt nhạc trước, ảnh bìa + câu kết, duyệt font, giọng nhân vật nhất quán
- **Yêu cầu của chủ kênh:**
  - Clip Flow không có nhạc. Muốn duyệt nhạc nền trước, rồi mới hỏi tạo clip tự động hay bằng tay (chọn tay mới ra prompt).
  - Ảnh bìa nổi bật thông điệp; clip cuối có câu kết.
  - Font chữ đầu/cuối video hơi xấu: cần duyệt font cho chữ đầu, phụ đề và câu kết, kèm ảnh minh hoạ.
  - Cùng một nhân vật nhưng giọng khác nhau giữa các clip 8 giây.
- **PR #33 (đã merge) — luồng duyệt mới, ảnh bìa, câu kết:**
  - Luồng: duyệt kịch bản → duyệt nhạc nền → chọn 🤖 tự động (Gemini) hay ✋ tạo tay (Flow); chỉ khi chọn tạo tay mới gửi prompt.
  - Bản nghe thử nhạc dài bằng cả video. Gửi mp3 mới trong lúc chờ duyệt nhạc → có nút nghe thử ngay với bài đó.
  - Kịch bản có thêm **chữ ảnh bìa** (3–7 từ, thông điệp) và **câu kết** (6–14 từ), hiện cho chủ kênh duyệt.
  - Video: chữ thông điệp to ở 1,6 giây đầu (ảnh bìa, gửi kèm `thumbnail.jpg` khi duyệt video; đăng thẳng TikTok lấy đúng khung này). Câu kết hiện ở đoạn kết, tăng lên 4,5 giây.
- **PR #34 (đã merge) — font chữ và giọng nhất quán:**
  - Đóng gói **9 font đủ dấu tiếng Việt** (Google Fonts, giấy phép OFL) vào image.
  - **Bước mới "Duyệt chữ"** (bước 3/4, sau duyệt nhạc): ảnh minh hoạ 3 khung (ảnh bìa, phụ đề, câu kết) dựng đúng như video thật. Bấm 🔤 để đổi font từng phần; lựa chọn được nhớ cho các video sau. Lệnh `/font` xem/đổi bất kỳ lúc nào.
  - Chế độ giọng mới **"Khớp giọng nhân vật"** (mặc định): đo cao độ + âm lượng từng câu, kéo câu lệch về **giọng chuẩn** của nhân vật (học từ video đầu, nhớ cho các video sau). Tốc độ nói và khẩu hình giữ nguyên; giới hạn ±4 nửa cung, ±8 dB. `/giong` hiện giọng chuẩn đã học, có nút học lại.
  - Lần cập nhật VPS này dựng lại image (thêm font + numpy) nên lâu hơn vài phút.
- **Kiểm thử:** toàn bộ bộ test trong Docker đạt. Ví dụ: Bà nói ở 200 / 240 / 180 Hz qua 3 clip → sau khớp đều ≈ 200 Hz, âm lượng đều. Chưa thử với clip Flow thật.
- **Giới hạn:** khớp giọng chỉnh được cao độ và âm lượng, chưa đổi được chất giọng; nếu Flow tạo hai giọng khác hẳn nhau thì vẫn còn khác (dùng Extend cho cảnh 2+ giúp ổn định hơn).

### 📌 Tóm tắt thay đổi 17/11/2026 — Prompt nối tiếp từ cảnh 2, nhạc nền luôn nghe rõ, sửa kênh đối thủ qua Telegram
- **Yêu cầu:** hình ảnh giữa các clip tạo tay chưa nhất quán; âm thanh nền mỗi clip chưa ổn, cần nhạc xuyên suốt không im lặng; cập nhật kênh đối thủ qua Telegram để kế hoạch/kịch bản đổi theo.
- **PR #32 (đã merge):**
  - Prompt từ cảnh 2 mở đầu bằng khối **"NỐI TIẾP clip trước"** (không cắt cảnh, cùng người, quần áo, nơi chốn, ống kính, ánh sáng; nhắc lại clip trước kết thúc ở đâu). Thêm **prompt ngắn cho Extend** của Flow.
  - Prompt cấm clip tự có nhạc. Mức nhạc nền **tự cân** theo giọng trong clip (nằm dưới giọng ~12 dB, bài nhỏ tự tăng, bài to tự giảm, không bao giờ tụt mất). Làm mềm tiếng nền ở điểm nối clip.
  - `/doithu`, `/kenhmau`: thêm/xoá/đặt lại kênh (nhận cả link TikTok), nút **🔄 Lập kế hoạch mới theo danh sách này**.

### 📌 Tóm tắt thay đổi 16/11/2026 (tiếp) — Cảnh liền mạch, số cảnh, giữ âm thanh clip, nhạc liên tục, đoạn kết
- **PR #30 (đã merge):**
  - Kịch bản có mô tả bối cảnh không đổi và khung đầu/khung cuối từng cảnh; khung đầu cảnh sau = khung cuối cảnh trước. Gemini tạo lần lượt, dùng khung cuối clip trước làm khung đầu clip sau. Bỏ hiệu ứng tối màn hình giữa các cảnh.
  - `/socanh N` (2–6 cảnh, mỗi cảnh 8 giây).
  - Giữ nguyên âm thanh clip khi ghép (không cắt, không đổi tốc độ, không bị giảm 3 dB khi mono → stereo). Phụ đề khớp chỗ có giọng thật.
- **PR #31 (đã merge):**
  - Không cắt clip 8 giây ở chế độ lời dẫn; lời đọc tách theo cảnh, giữ tiếng nền clip.
  - Nhạc nền một bài liền từ đầu đến cuối, lặp có crossfade; chỉ hạ nhẹ khi có người nói (không bị tiếng mưa/gió kéo xuống), không dưới 50%. Tự kiểm tra nhạc từng đoạn 8 giây, báo trên Telegram nếu quá nhỏ hoặc thư viện trống.
  - Đoạn kết: giữ khung cuối, zoom-out chậm, mờ sang tối ấm, nhạc tắt dần.

### 📌 Tóm tắt thay đổi 16/11/2026 — Sửa lỗi prompt dài làm hỏng tin Telegram; tần suất ra video
- **Yêu cầu của chủ kênh:**
  - Bot báo `can't parse entities: Can't find end tag … "code"` khi gửi prompt của kịch bản mới.
  - Muốn đổi tần suất ra video (ví dụ 2 ngày 1 video) bất kỳ lúc nào, hiệu lực từ ngày hôm sau.
- **Nguyên nhân lỗi:** bot cắt mỗi tin ở 4096 ký tự sau khi đã mã hoá HTML. Mỗi dấu `"` trong thoại thành `&quot;` (6 ký tự), nên prompt dài bị cắt mất thẻ đóng `</code>`.
- **Đã sửa (PR #28, đã merge; chủ kênh đã cập nhật VPS):**
  - Prompt được chia thành các đoạn dưới 3500 ký tự **trước** (cắt ở chỗ xuống dòng, rồi cuối câu), sau đó mới bọc `<code>` cho từng đoạn.
    - Mỗi phần giữ tiêu đề "Cảnh N". Prompt nhiều phần kèm dòng nhắc chép lần lượt, dán nối tiếp vào cùng một ô prompt trong Flow.
    - Dấu `"` không còn bị mã hoá.
  - Tin quá dài được gửi dạng chữ thường, không bao giờ cắt giữa thẻ HTML.
  - n8n: Telegram vẫn báo `can't parse entities` → tự gửi lại đúng tin đó dạng chữ thường.
  - Áp dụng cho lúc duyệt kịch bản, `/clip`, `/clip N` và prompt viết lại bằng 🛡.
  - **`/tansuat N`** (hoặc nút chọn nhanh): N ngày ra 1 kịch bản (1–30), hiệu lực từ ngày mai.
    - Job 07:00 bỏ qua những ngày không đến lượt.
    - `/tansuat` và `/trangthai` hiện các ngày sẽ gửi kịch bản; mặc định trong `config.yaml` (`content.every_days`).
    - Kế hoạch 30 ý tưởng giãn ra theo tần suất; `/kichban` vẫn dùng được bất kỳ lúc nào.
- **Kiểm thử:**
  - Prompt 10.000 ký tự có `"`, `<`, `>`, `&`: mọi tin là HTML hợp lệ và ghép lại khớp từng chữ với prompt gốc.
  - Kịch bản dài giả lập chạy qua duyệt, `/clip`, `/clip 2`, 🛡.
  - Tần suất và job 07:00; phần gửi lại dạng chữ thường của n8n.
  - Bộ test cũ vẫn đạt.
  - Chưa thử được với kịch bản dài nhất thật trên VPS.
- **Đang chờ kiểm tra thật:** `/clip` nhận đủ Cảnh 1–3 không lỗi; đặt `/tansuat`; làm trọn 1 video để đánh giá mặt, giọng, phụ đề, nhạc.

### 📌 Tóm tắt thay đổi 15/11/2026 (tối) — Gửi đủ prompt các cảnh, thay ảnh/thêm nhân vật, nhập giọng theo ý
- **Yêu cầu của chủ kênh:**
  - Khi chuyển sang tạo clip tay, bot chỉ gửi prompt cảnh 1.
  - Muốn thay ảnh đại diện, thêm nhân vật mới.
  - Muốn nhập cao độ (pitch) và tốc độ (rate) giọng trực tiếp thay vì chỉ chọn vài giọng gợi ý.
- **PR #26 (đã merge):**
  - n8n gửi mỗi tin cách nhau 1,1 giây (Telegram giới hạn khoảng 1 tin/giây), tự thử lại tối đa 5 lần. Tin vẫn lỗi thì bot báo cho chủ kênh.
  - Chú thích ảnh quá 1024 ký tự được rút gọn an toàn; thẻ nhân vật dài gửi thành ảnh + tin chữ.
  - `/clip 2` gửi lại riêng cảnh 2.
  - Nút **📷 Thay ảnh** trên thẻ nhân vật.
  - Thêm nhân vật mới: gửi ảnh chú thích `Tên - vai` → **➕ Nhân vật mới**, hoặc `/themnv Tên - vai`. Nhân vật thêm xoá được; 6 nhân vật chính chỉ sửa.
- **PR #27 (đã merge):**
  - Nhập giọng bằng `/giong Ông pitch -10 rate -5`, nút **✏️ Nhập giọng**, hoặc Trả lời bản nghe thử.
  - Hiểu cả yêu cầu bằng lời: `trầm hơn`, `nhanh hơn nhiều`, `giọng nam`.
  - Giá trị tự chuẩn hoá theo định dạng edge-tts (`+5Hz`, `+0%`), nên giá trị như `0%` không còn làm hỏng giọng.

### 📌 Tóm tắt thay đổi hôm nay (15/11/2026, chiều) — Sửa lỗi Gemini key; 6 nhân vật cố định, ảnh mẫu, giọng cố định
- **Yêu cầu của chủ kênh:**
  - Bước ghép video báo lỗi `API key not valid … API_KEY_INVALID`
  - Hình ảnh và giọng nói không nhất quán giữa các clip, cả khi tạo tự động lẫn khi dán prompt tay trên Flow
  - Muốn 6 tuyến nhân vật (Ông, Bà, Bố, Mẹ, bé trai, bé gái) cấu hình từ đầu, gồm cả hình ảnh
- **Sửa lỗi Gemini key (PR #23, đã merge):**
  - Nguyên nhân: `GEMINI_API_KEY` trong `.env` trên VPS không hợp lệ.
  - Giờ bot báo rõ "Gemini API key không hợp lệ" kèm 3 bước sửa, không tính phí. Bot gửi luôn prompt để tạo tay trên Flow và nút **🔁 Thử tạo tự động lại**.
  - Mọi lỗi tạo clip tự động khác cũng quay về bước chờ clip, không còn dừng hẳn ở "Ghép video lỗi".
- **6 nhân vật cố định (PR #24, đã merge):**
  - **Cấu hình:** mục `cast:` trong `config.yaml` là giá trị ban đầu.
    - 6 nhân vật: Ông, Bà, Bố, Mẹ, Cu Tí (bé trai), Bé Na (bé gái).
    - Mỗi người có: ngoại hình và trang phục cố định, giọng cho clip AI, cách xưng hô, giọng ghép cố định.
    - Lần chạy đầu, hệ thống chép sang `/opt/tiktok-agent/data/nhan_vat.yaml`. Sửa ở file đó; cập nhật code không ghi đè.
  - **Telegram:**
    - `/nhanvat`: 6 thẻ nhân vật. Thẻ chưa có ảnh kèm prompt tạo ảnh chân dung.
    - Gửi ảnh, chú thích là tên (vd `Ông`): bot lưu ảnh mẫu và tự mô tả ngoại hình theo ảnh. Không ghi chú thích thì bot hỏi ảnh đó là của ai.
    - Trả lời thẻ bằng các dòng `Tên:` / `Ngoại hình:` / `Giọng:` / `Cách nói:` để sửa.
    - `/giong`: nghe và đổi giọng từng người, chọn **giọng cố định** hay **giọng trong clip**.
  - **Kịch bản:** chỉ dùng đúng 6 nhân vật, xưng hô theo từng người, mỗi cảnh tối đa 3 người. Tên người nói tự chuẩn hoá.
  - **Prompt clip:** nhắc lại nguyên văn ngoại hình và giọng của nhân vật trong cảnh.
    - Tạo tay: bot gửi ảnh mẫu và ghi **🧩 Ingredients: …** dưới mỗi cảnh để thêm ảnh vào Flow.
    - Gemini: gửi kèm ảnh mẫu. Model không nhận ảnh thì bot tự tạo lại chỉ với prompt.
  - **Giọng cố định (mặc định):**
    - Khi ghép, mỗi nhân vật luôn một giọng riêng ở mọi clip, kể cả clip tạo tay.
    - Thoại xếp lần lượt, không chồng nhau; câu dài được đọc nhanh hơn một chút cho vừa cảnh; cảm xúc chỉnh nhẹ tốc độ và âm lượng.
    - Bản nghe thử lúc duyệt kịch bản chính là âm thanh thành phẩm, được dùng lại khi ghép. Phụ đề khớp theo.
  - Ảnh nhân vật gửi trước đây tự gắn vào nhân vật cùng tên.
  - Kiểm thử trong Docker (Claude, Gemini và giọng đọc giả lập): bộ test nhân vật 51/51 đạt, bộ test cũ vẫn đạt.
- **Hạn chế đã biết:**
  - Giọng miễn phí chỉ có 2 giọng tiếng Việt (Hoài My, Nam Minh). 6 nhân vật phân biệt bằng cao độ và tốc độ, nên giọng trẻ con hơi "máy".
  - Chế độ giọng cố định có thể lệch khẩu hình nhẹ.
  - Gemini nhận ảnh mẫu hay không: chưa thử với tài khoản thật.
- **PR #22** (tóm tắt buổi sáng) cũng đã merge.

### 📌 Tóm tắt thay đổi hôm nay (15/11/2026, sáng)
- **Yêu cầu của chủ kênh:** thêm mức ngân sách nhỏ ($5, $10) thay vì chỉ $25; prompt hay bị Google Flow chặn vì chính sách; gửi nhạc qua Telegram nhưng bot báo thư viện trống; muốn video ghép xong lưu lên Google Drive
- **Đã làm (PR #21, đã merge vào `main`; chủ kênh đã cập nhật VPS):**
  - Duyệt ngân sách bằng nút chọn mức: Gemini $1/$5/$10/$25, Claude $2/$5/$10, Apify $1/$2/$5
  - Kịch bản và prompt tuân thủ chính sách Google (bộ quy tắc cho biên kịch + tự lọc từ rủi ro); nút 🛡 viết lại prompt cảnh bị Flow chặn; Gemini bị chặn → tự viết lại và thử lại 1 lần
  - Sửa lỗi nhạc nền: nhận file nhạc gửi dạng tài liệu, trả lời hướng dẫn khi file không dùng được, lệnh cài tự nạp lại workflow n8n khi có bản mới
  - Video ghép xong tự lưu lên Drive (thư mục TikTok Agent/Video), link gửi kèm video và ghi vào file kịch bản
- **Đang chờ kiểm tra thật:** gửi lại nhạc (bot phải trả lời "🎵 Đã thêm nhạc nền"), làm 1 video thử với ngân sách Gemini $1 hoặc tạo tay trên Flow, xem video trên Drive, đánh giá giọng nhân vật, khẩu hình, nhạc, hình anime

### 15/11/2026 — Chọn mức ngân sách, prompt đúng chính sách Google, sửa lỗi nhạc nền, video lên Drive
- **Ngân sách chọn được nhiều mức:** bot xin duyệt hiện các nút **$1 / $5 / $10 / $25** cho Gemini (Claude: $2/$5/$10, Apify: $1/$2/$5; sửa trong `options_usd`), chỉ hiện các mức đủ cho yêu cầu hiện tại; mặc định đề xuất Gemini giảm còn $5 (≈ 8 video)
- **Prompt đúng chính sách Google Flow/Veo** (`tiktok_agent/policy.py`): biên kịch nhận bộ quy tắc an toàn (trẻ em luôn mặc đủ, an toàn, không ghi tuổi cụ thể; không bạo lực/trừng phạt, nguy hiểm, rượu thuốc, vũ khí, thương hiệu, nhân vật bản quyền, tên hãng phim…); prompt clip tự lọc từ ngữ rủi ro (giữ nguyên câu thoại); bỏ vai/tuổi tiếng Việt khỏi prompt
- **Nút "🛡 Flow báo vi phạm chính sách → viết lại"** dưới mỗi prompt cảnh: Claude viết lại cảnh đó an toàn hơn (~$0.03) và gửi lại prompt mới. Tạo tự động bằng Gemini bị chặn chính sách → tự viết lại và thử lại 1 lần, vẫn bị chặn thì chuyển sang tạo tay
- **Sửa lỗi nhạc nền "thư viện trống":** nhận cả file nhạc gửi dạng tài liệu (mp3/m4a… kể cả khi Telegram không nhận ra loại file); file bot chưa dùng được thì bot trả lời hướng dẫn thay vì im lặng; **lệnh cài tự nạp lại workflow n8n khi file workflow đổi** (trước đây nếu quên `UPDATE_WORKFLOW=1`, n8n cũ không chuyển file nhạc tới hệ thống)
- **Video ghép xong tự tải lên Google Drive**, thư mục *TikTok Agent/Video* (tên file `#id Tiêu đề ngày giờ.mp4`), link gửi kèm video và ghi vào file kịch bản trên Drive
- Đã kiểm thử trong Docker (mức ngân sách, lọc chính sách, viết lại prompt, Gemini bị chặn → thử lại, phân loại file n8n, tải video lên Drive giả lập)

### 14/11/2026 (tiếp) — Tự lưu kịch bản đã duyệt lên Google Drive
- Mỗi lần **Duyệt kịch bản**, hệ thống cập nhật **một file Google Docs** "Kịch bản đã duyệt – TikTok Agent" (thư mục *TikTok Agent*): toàn bộ kịch bản đã duyệt kèm **thoại từng cảnh** (người nói, cảm xúc), nhạc nền, thông điệp, caption; mới nhất ở trên (`tiktok_agent/gdrive.py`)
- Kết nối bằng **mã thiết bị** của Google: `/drive` → mở google.com/device trên iPad, nhập mã → Cho phép. Quyền `drive.file`: app chỉ thấy file nó tạo
- Lỗi khi lưu không chặn việc duyệt; lần duyệt sau tự lưu lại toàn bộ; `/drive` → Đồng bộ ngay / Ngắt kết nối
- Cần tạo OAuth client loại **TVs and Limited Input devices** trên Google Cloud (hướng dẫn: `HUONG_DAN.md` Bước 8b), đặt app ở **In production** để quyền không hết hạn sau 7 ngày; lệnh cài hỏi thêm `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- Đã kiểm thử trong Docker với Google API giả lập (mã thiết bị, chờ xác nhận, tạo thư mục + file, ghi đè cùng file, lỗi upload, làm mới token)

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
3. **Đăng TikTok chưa xác nhận chạy thật:** lần duyệt đăng 25/11 không thấy video trong hộp thư. Đã thêm đọc trạng thái thật + nút đăng lại; cần bấm **🔁 Đăng lại TikTok** để xem TikTok báo lý do gì (Sandbox, quá nhiều bản nháp, phiên hết hạn…). `direct` chỉ đăng riêng tư tới khi app qua audit.
4. **Giọng nhân vật (clip độc lập):** khớp giọng chỉ chỉnh cao độ + âm lượng; nếu Flow tạo hai chất giọng khác hẳn nhau giữa các clip thì vẫn còn khác. Cách **🎞 Kéo dài** tránh được vấn đề này vì cả video do Flow tạo liền.
5. **Chủ đề 2 chưa có ảnh mẫu hiện đại:** cần tạo 6 ảnh chân dung hiện đại (prompt trong `/nhanvat 2`) và gửi với chú thích `2 Tên`. Chưa có ảnh thì khuôn mặt giữa các clip chỉ dựa vào mô tả chữ, dễ lệch hơn.
6. **Prompt rất dài** được chia thành nhiều phần; chủ kênh phải chép lần lượt và dán nối tiếp vào cùng một ô prompt trong Flow.
7. **Chủ đề 3 – giới hạn đã biết:**
   - Chưa có `GEMINI_API_KEY` thì ảnh nền lấy từ Pixabay (ảnh chụp, không cùng phong cách anime).
   - Giọng Gemini TTS đọc từng câu riêng nên ngữ điệu giữa các câu có thể hơi khác. Model `gemini-2.5-flash-preview-tts` là bản preview, Google có thể đổi tên.
   - Nhạc theo mô tả chỉ chọn trong thư viện nhạc đã gửi, không tự sáng tác.
   - Chưa thử với ảnh, giọng Gemini thật trên VPS.
8. **edge-tts lỗi theo đợt** ("No audio was received") — đã có thử lại + nút 🔁 Thử lại trên Telegram.
9. **Hạn chế đã biết:**
   - Độ nhất quán khuôn mặt vẫn phụ thuộc model video (đã có mô tả cố định, ảnh mẫu, khung cuối, prompt nối tiếp, Extend).
   - Đăng vào hộp nháp TikTok: chủ kênh tự chọn ảnh bìa ở giây đầu khi đăng trong app.
   - Còn lại: Telegram chỉ cho bot tải file ≤ 20 MB; chưa hỗ trợ upload TikTok > 64 MB; phụ đề căn thời gian theo ước lượng; số lượt Flow miễn phí do Google quyết định và có thể thay đổi.

## 📋 Các bước cần cấu hình tiếp

1. **Cập nhật VPS** sau khi PR #43 merge (lệnh đầu file). Kiểm tra `.env` trên VPS có `GEMINI_API_KEY` để dùng ảnh nền và giọng AI theo mô tả.
2. `/font` → chọn font cho chữ đầu video, phụ đề, câu kết (xem ảnh minh hoạ).
3. `/giong` → kiểm tra đang chọn **Khớp giọng nhân vật**.
4. `/nhac` → có ít nhất 1 bài nhạc nền không bản quyền.
5. `/doithu` → cập nhật kênh đối thủ nếu cần → **🔄 Lập kế hoạch mới** → duyệt.
6. **Ảnh mẫu hiện đại:** `/nhanvat 2` → tạo ảnh từng người → gửi ảnh chú thích `2 Ông`, `2 Bà`…
7. **Làm trọn 1 video mỗi chủ đề:** `/kichban 1` (ký ức) và `/kichban 2` (gia đình) → duyệt kịch bản → duyệt nhạc → duyệt chữ → chọn tạo tay/tự động; `/kichban 3` → chọn thời lượng → duyệt kịch bản → ảnh nền → chữ → giọng (thử ✍️ Mô tả giọng) → nhạc → video.
   - Tạo tay: ưu tiên **🎞 Kéo dài** → Đoạn 1 dán prompt đầy đủ (thêm ảnh nhân vật ở Ingredients), đoạn sau bấm Extend và dán prompt kéo dài → tải toàn bộ video → trả lời tin **📥 Gửi video kéo dài**.
8. **Đánh giá video:** ảnh bìa có chữ thông điệp; nhạc liền từ đầu đến cuối; câu kết ở đoạn cuối; giọng mỗi nhân vật giữ nguyên qua các clip; nhân vật giống nhau → gửi nhận xét cho Claude.
9. **TikTok:** trên video đã duyệt bấm **🔁 Đăng lại TikTok** → đọc trạng thái bot báo → sửa theo gợi ý (kết nối lại `/tiktok`, thêm tài khoản vào Sandbox, đăng/xoá bớt bản nháp).
10. Huỷ hoặc gia hạn Contabo trước ngày hết kỳ 1 tháng.
