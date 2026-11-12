# Kiểm thử toàn bộ luồng + đăng TikTok Sandbox

> Mỗi bước có **Kết quả đúng** để so sánh. Bước nào ra khác thì dừng lại, chụp màn hình gửi Claude.
> Trên GitHub, mỗi khung lệnh có nút **sao chép** ở góc phải.

## Điều kiện trước khi test

- [ ] Đã chạy lệnh cài trên VPS Contabo, màn hình hiện **✅ XONG!** và **Telegram: OK**
- [ ] Đã điền `TELEGRAM_CHAT_ID` trong `.env` và chạy lại lệnh cài
- [ ] Đã tạo app TikTok: Login Kit, Content Posting API, 3 scopes, **Sandbox** có tài khoản kênh trong **Target users**
- [ ] Đã điền **Client key/secret của Sandbox** vào `.env` và chạy lại lệnh cài

**Mẹo test nhanh:** ở Phần 4 chưa cần tạo clip trên Google Flow — dùng **4 video ngắn bất kỳ trong app Ảnh**
(mỗi video 5–10 giây) là đủ để kiểm tra luồng ghép video và đăng.

---

## Phần 1 – Kiểm tra hệ thống đang chạy (2 phút)

Trong Termius:

```bash
cd /opt/tiktok-agent && docker compose ps
```

**Kết quả đúng:** 3 dòng `caddy`, `n8n`, `worker`, cột STATUS đều là **Up**.

Trong Telegram, gửi cho bot:

```
/trangthai
```

**Kết quả đúng:** bot trả lời bảng trạng thái; dòng TikTok **chưa có ✅** (chưa kết nối kênh).

## Phần 2 – Kết nối TikTok Sandbox (3 phút)

1. Gửi cho bot:

```
/tiktok
```

2. Mở link bot gửi bằng **Safari** (không mở bằng app TikTok).
3. Đăng nhập **đúng tài khoản đã thêm vào Target users** → **Authorize / Cho phép**.
4. Kiểm tra lại:

```
/trangthai
```

**Kết quả đúng:** trang web báo **"✅ Đã kết nối"**; trong `/trangthai` dòng TikTok có ✅.

| Nếu thấy | Nguyên nhân |
|---|---|
| `redirect_uri` lỗi | Redirect URI trong app TikTok khác `https://<ip-gạch-ngang>.sslip.io/tiktok/callback` |
| `client_key` lỗi | Đang dùng khoá Production thay vì khoá Sandbox |
| Không có quyền | Tài khoản chưa nằm trong Target users |

## Phần 3 – Kế hoạch và kịch bản, có Claude thật (5–10 phút)

1. Lập kế hoạch:

```
/kehoach
```

   **Kết quả đúng:** lần đầu bot hỏi **"Duyệt ngân sách Claude…"** (cổng ngân sách đang làm đúng việc) → bấm **Duyệt**.
2. Chờ 2–5 phút. **Kết quả đúng:** bot báo "⏳ Đang xử lý", rồi gửi **kế hoạch 30 ngày** và bộ nhân vật.
3. Test góp ý: bấm **Trả lời** vào tin kế hoạch, gõ ví dụ "Thêm chủ đề về ông bà" → gửi.
   **Kết quả đúng:** bot làm lại kế hoạch theo góp ý.
4. Bấm **✅ Duyệt** kế hoạch.
5. Xem bộ nhân vật:

```
/nhanvat
```

   **Kết quả đúng:** bot gửi prompt ảnh nhân vật.
6. Lấy kịch bản ngày 1:

```
/kichban
```

   **Kết quả đúng:** bot gửi kịch bản 3–4 cảnh.
7. Test viết lại: **Trả lời** vào tin kịch bản, gõ góp ý (ví dụ "Lời đọc ngắn hơn") → gửi.
   **Kết quả đúng:** bot gửi kịch bản mới.
8. Bấm **Duyệt** kịch bản. **Kết quả đúng:** bot gửi các tin **"Cảnh 1… Cảnh 4"**, mỗi tin có prompt tiếng Anh.

## Phần 4 – Gửi clip và ghép video (5 phút)

1. Bấm **Trả lời** vào tin **"Cảnh 1"** → đính kèm 1 video → gửi.
   **Kết quả đúng:** bot báo đã nhận cảnh 1 và gửi **ảnh khung cuối** của clip.
2. Làm tương tự cho các cảnh còn lại.
3. Khi đủ clip. **Kết quả đúng:** bot báo "⏳ Đang ghép video", sau 1–3 phút gửi **video 24–32 giây** có giọng đọc
   tiếng Việt và phụ đề.
4. Mở video, kiểm tra: giọng đọc có tiếng, phụ đề hiện đúng dấu, các cảnh đúng thứ tự.

**Lưu ý:**
- Mỗi clip phải **dưới 20 MB** (Telegram không cho bot tải file lớn hơn).
- Bot báo lỗi giọng đọc → bấm **🔁 Thử lại**.

## Phần 5 – Đăng TikTok Sandbox, cách 1: hộp nháp (mặc định)

1. Bấm **Duyệt video**. **Kết quả đúng:** bot báo đã đẩy lên TikTok và gửi kèm **caption** để sao chép.
2. Mở **app TikTok** bằng tài khoản kênh → **Hộp thư / Thông báo**: có tin "video đã được tải lên từ <tên app>",
   hoặc video nằm trong **Bản nháp**.
3. Mở video → dán caption → chọn **Chỉ mình tôi** cho lần test → **Đăng**.

✅ Đến đây là đã test xong **toàn bộ luồng**.

## Phần 6 – Đăng TikTok Sandbox, cách 2: đăng thẳng (tuỳ chọn)

App chưa qua audit thì TikTok chỉ cho đăng thẳng ở chế độ **riêng tư**, và tài khoản kênh phải đặt **riêng tư** trong
lúc test (app TikTok → Cài đặt → Quyền riêng tư → **Tài khoản riêng tư**).

1. Mở file cấu hình:

```bash
cd /opt/tiktok-agent && nano config.yaml
```

2. Tìm và sửa 2 dòng thành:

```
  post_mode: "direct"
  privacy_level: "SELF_ONLY"
```

3. Lưu (**Ctrl + O** → **Enter** → **Ctrl + X**), rồi khởi động lại worker:

```bash
docker compose restart worker
```

4. Gửi `/kichban` để làm video thứ hai, đi lại Phần 4 → **Duyệt video**.

**Kết quả đúng:** video xuất hiện thẳng trên trang cá nhân TikTok ở chế độ **Chỉ mình tôi**.

⚠️ **Test xong phải trả `config.yaml` về như cũ** (nếu không, lần chạy lệnh cài sau sẽ lỗi khi cập nhật code):

```bash
cd /opt/tiktok-agent && git checkout config.yaml && docker compose restart worker
```

Sau đó đặt lại tài khoản TikTok về **công khai**.

## Phần 7 – Kiểm tra lịch tự động (ngày hôm sau)

- **07:00:** bot tự gửi kịch bản ngày tiếp theo, không cần gõ lệnh.
- Xem chi phí đã dùng (lần test đầu khoảng **$1–1.5** cho Claude):

```
/ngansach
```

## Khi có lỗi

Chạy rồi chụp màn hình gửi Claude:

```bash
cd /opt/tiktok-agent && docker compose logs --tail 40 worker
```

Lịch sử từng lần chạy: mở `https://<ip-gạch-ngang>.sslip.io` → tab **Executions** trong n8n.

## Bảng tick kết quả

| Phần | Nội dung | Đạt? |
|---|---|---|
| 1 | 3 container Up, `/trangthai` trả lời | ☐ |
| 2 | Kết nối TikTok Sandbox ✅ | ☐ |
| 3 | Duyệt ngân sách → kế hoạch → góp ý → kịch bản → viết lại → prompt cảnh | ☐ |
| 4 | Nhận clip, ảnh khung cuối, video có giọng + phụ đề | ☐ |
| 5 | Video vào hộp nháp TikTok, đăng được | ☐ |
| 6 | (Tuỳ chọn) Đăng thẳng SELF_ONLY, đã trả `config.yaml` về cũ | ☐ |
| 7 | 07:00 tự gửi kịch bản; `/ngansach` đúng | ☐ |
