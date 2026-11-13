# Sổ tay cài TikTok Agent trên VPS Contabo

> Trên GitHub, mỗi khung lệnh có nút **sao chép** (biểu tượng 2 ô vuông ở góc phải khung).
> Bấm sao chép → sang **Termius** chạm giữ màn hình → **Paste** → **Enter**.

| # | Việc | Thời gian | Chi phí |
|---|---|---|---|
| 1 | Thuê VPS Contabo | 15 phút + chờ giao máy | khoảng 5–7 €/tháng |
| 2 | Kết nối VPS bằng Termius | 5 phút | 0 |
| 3 | Cài hệ thống bằng 1 lệnh | 10 phút | 0 |
| 4 | Lấy Chat ID, kích hoạt bot | 5 phút | 0 |
| 5 | Tạo tài khoản quản trị n8n | 2 phút | 0 |
| 6 | Chạy thử trọn quy trình | 30 phút | Claude khoảng 1 USD |
| 7 | Kết nối TikTok | 20 phút + chờ TikTok duyệt | 0 |
| 8 | Vận hành hằng ngày | 5–10 phút/ngày | Claude khoảng 2–5 USD/tháng |

---

## Bước 1 – Thuê VPS Contabo

1. Safari → `contabo.com` → **VPS** → gói rẻ nhất (**Cloud VPS 10**).
2. **Term:** 1 month · **Region:** European Union (Germany) · **Image:** Ubuntu 24.04
   (không chọn bản có Plesk/cPanel/Apps).
3. Đặt **mật khẩu root** mạnh và ghi ngay vào app Ghi chú.
4. **Add-ons:** không chọn gì → thanh toán.
5. Chờ email **"Your login data"** → ghi lại **địa chỉ IP**.

## Bước 2 – Kết nối VPS bằng Termius

1. Termius → **Hosts** → **+** → **New Host**.
2. **Address:** IP trong email · **Password:** mật khẩu root đã đặt · **Username:**

```
root
```

3. **Save** → chạm vào host → hỏi fingerprint thì bấm **Continue**.
   Thấy `root@...:~#` là đã vào được.

## Bước 3 – Cài hệ thống

```bash
curl -fsSL https://raw.githubusercontent.com/sinhtd1503-png/Claude--code-lab/main/deploy/install.sh | bash
```

| Script hỏi | Bạn làm |
|---|---|
| Token bot Telegram | Dán từ Ghi chú → Enter |
| Claude API key (`sk-ant-…`) | Dán từ Ghi chú → Enter |
| Apify token | Dán, hoặc Enter nếu chưa có |
| TikTok Client Key / Secret, Pixabay | Chỉ nhấn Enter |

Khoá dán vào có thể không hiện trên màn hình, điều đó bình thường. Chờ đến khi hiện **✅ XONG!**
và **Telegram: OK**, rồi ghi lại địa chỉ `https://…sslip.io`.

## Bước 4 – Lấy Chat ID, kích hoạt bot

Mở Telegram, gửi cho bot:

```
/start
```

Bot trả lời một dãy số (Chat ID) → chép lại. Sang Termius mở file cấu hình:

```bash
cd /opt/tiktok-agent && nano .env
```

- Tìm dòng `TELEGRAM_CHAT_ID=`, dán dãy số ngay sau dấu `=`.
- Lưu: **Ctrl + O** → **Enter** → **Ctrl + X** (nút Ctrl có trên thanh phím Termius).

Chạy lại lệnh cài để áp dụng (nhanh, không hỏi lại khoá):

```bash
curl -fsSL https://raw.githubusercontent.com/sinhtd1503-png/Claude--code-lab/main/deploy/install.sh | bash
```

## Bước 5 – Tạo tài khoản quản trị n8n

1. Safari → mở địa chỉ `https://…sslip.io` từ Bước 3.
2. Tạo tài khoản quản trị (email + mật khẩu mạnh). Workflow **TikTok Agent** đã bật sẵn, không cần chỉnh gì.

## Bước 6 – Chạy thử trọn quy trình

Lập kế hoạch 30 ngày (chờ 2–5 phút) → bấm **✅ Duyệt**:

```
/kehoach
```

Lấy kịch bản ngày 1 → bấm **Duyệt**:

```
/kichban
```

Tạo clip trên Google Flow:

```
https://labs.google/flow
```

1. Mỗi cảnh: chép prompt → tạo clip 8 giây → tải về.
2. Telegram: **Trả lời** vào tin "Cảnh N" của bot → đính kèm clip → gửi.
3. Đủ clip, bot tự ghép video có giọng đọc và phụ đề → bấm **Duyệt video**.

Lỗi giọng đọc hoặc ghép video: bấm nút **🔁 Thử lại** trên tin báo lỗi.

## Bước 7 – Kết nối TikTok (làm sau khi chạy thử ổn)

1. `developers.tiktok.com` → **Manage apps** → **Connect an app**.
2. Thêm **Login Kit** và **Content Posting API**.
3. **Redirect URI**: thay `IP-CUA-BAN` bằng IP của VPS, **đổi dấu chấm thành dấu gạch ngang**
   (ví dụ IP `1.2.3.4` → `https://1-2-3-4.sslip.io/tiktok/callback`):

```
https://IP-CUA-BAN.sslip.io/tiktok/callback
```

4. Scopes:

```
user.info.basic,video.upload,video.publish
```

5. Lấy **Client Key** và **Client Secret**, mở file cấu hình và điền vào `TIKTOK_CLIENT_KEY=`, `TIKTOK_CLIENT_SECRET=`:

```bash
cd /opt/tiktok-agent && nano .env
```

6. Lưu file rồi chạy lại lệnh cài:

```bash
curl -fsSL https://raw.githubusercontent.com/sinhtd1503-png/Claude--code-lab/main/deploy/install.sh | bash
```

7. Gửi cho bot → bấm link → đăng nhập TikTok → cho phép:

```
/tiktok
```

Video sẽ vào **hộp nháp TikTok**, bạn mở app TikTok để bấm đăng. Đăng thẳng công khai chỉ dùng được
sau khi TikTok duyệt app (audit).

## Bước 8 – Vận hành hằng ngày

- **06:30**: hệ thống tự lập kế hoạch tháng mới khi chỉ còn 2 ngày hoặc ít hơn.
- **07:00**: bot gửi kịch bản → duyệt → tạo clip Flow → duyệt video → đăng.

| Lệnh Telegram | Tác dụng |
|---|---|
| `/trangthai` | Xem tiến độ |
| `/ngansach` | Chi phí tháng |
| `/nhanvat` | Bộ nhân vật |
| 📷 Gửi ảnh + chú thích | Nhân vật theo ảnh mẫu |
| `/nhac` | Thư viện nhạc nền (gửi file mp3 để thêm) |
| `/phongcach` | Xem/sửa phong cách kênh mẫu |
| `/clip` | Gửi lại prompt clip |

## Khi gặp lỗi

Chạy trong Termius rồi chụp màn hình gửi Claude.

Xem các phần đang chạy:

```bash
cd /opt/tiktok-agent && docker compose ps
```

Xem nhật ký lỗi gần nhất:

```bash
cd /opt/tiktok-agent && docker compose logs --tail 40 worker n8n
```

Kiểm tra cổng 80/443 có bị chiếm:

```bash
ss -ltnp | grep -E ':(80|443) '
```

## Bảo mật

- Không gửi mật khẩu root hay khoá API cho ai, kể cả dán vào chat với Claude.
- Không dùng nữa thì vào my.contabo.com → **Cancel** VPS trước ngày gia hạn.
