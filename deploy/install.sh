#!/usr/bin/env bash
# Cài (hoặc cập nhật) toàn bộ hệ thống lên VPS Ubuntu 22.04/24.04 — chạy bằng root:
#
#   curl -fsSL https://raw.githubusercontent.com/sinhtd1503-png/Claude--code-lab/main/deploy/install.sh | bash
#
# Lần đầu: script hỏi các khoá API rồi dựng Caddy (HTTPS) + n8n + worker bằng Docker, nạp sẵn workflow n8n
# và đăng ký webhook Telegram. Chạy lại bất cứ lúc nào để cập nhật code / áp dụng thay đổi trong .env.
set -euo pipefail

REPO="${REPO:-https://github.com/sinhtd1503-png/Claude--code-lab.git}"
BRANCH="${BRANCH:-main}"
APP=/opt/tiktok-agent
WF_ID=TikTokAgent0001

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Hãy chạy bằng root: gõ  sudo -i  rồi chạy lại lệnh."; exit 1; }

say "1/8 Cài gói hệ thống và Docker"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates openssl >/dev/null
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true

say "2/8 Tắt bot Python cũ (nếu có) — Telegram chỉ cho một nơi nhận tin của bot"
systemctl disable --now tiktok-agent >/dev/null 2>&1 || true

say "3/8 Mở cổng 80/443 (HTTPS)"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi
# Oracle Cloud (Ubuntu) chặn sẵn bằng iptables → thêm luật cho phép và lưu lại
if iptables -S INPUT 2>/dev/null | grep -q REJECT; then
  for p in 80 443; do
    iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$p" -j ACCEPT
  done
  command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true
fi

say "4/8 Tải code về $APP"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$BRANCH" && git -C "$APP" checkout -q "$BRANCH" && git -C "$APP" pull -q --ff-only
else
  git clone -q -b "$BRANCH" "$REPO" "$APP"
fi
cd "$APP"

say "5/8 Cấu hình khoá bí mật (.env)"
[ -f .env ] || cp .env.example .env
chmod 600 .env
get_env() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
set_env() {
  local k="$1" v="$2"
  if grep -qE "^$k=" .env; then
    local esc; esc=$(printf '%s' "$v" | sed -e 's/[\/&|]/\\&/g')
    sed -i "s|^$k=.*|$k=$esc|" .env
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}
ask() {  # ask KEY "Mô tả" [optional]
  local k="$1" label="$2" cur; cur="$(get_env "$k")"
  if [ -n "$cur" ]; then return; fi
  if ! (exec </dev/tty) 2>/dev/null; then [ "${3:-}" = optional ] || warn "Thiếu $k trong .env"; return 0; fi
  local v=""
  while :; do
    read -r -p "  $label${3:+ (Enter để bỏ qua)}: " v < /dev/tty || true
    [ -n "$v" ] || [ "${3:-}" = optional ] && break
  done
  if [ -n "$v" ]; then set_env "$k" "$v"; fi
}
ask TELEGRAM_BOT_TOKEN "Token bot Telegram (từ @BotFather)"
ask ANTHROPIC_API_KEY  "Claude API key (console.anthropic.com, bắt đầu bằng sk-ant-)"
ask APIFY_TOKEN        "Apify token (quét đối thủ)" optional
ask TIKTOK_CLIENT_KEY  "TikTok Client Key" optional
ask TIKTOK_CLIENT_SECRET "TikTok Client Secret" optional
ask PIXABAY_API_KEY    "Pixabay API key (video dự phòng)" optional
ask GEMINI_API_KEY     "Gemini API key (tự tạo clip AI, trả phí, luôn hỏi duyệt ngân sách)" optional

[ -n "$(get_env WORKER_TOKEN)" ] || set_env WORKER_TOKEN "$(openssl rand -hex 24)"
[ -n "$(get_env TELEGRAM_WEBHOOK_SECRET)" ] || set_env TELEGRAM_WEBHOOK_SECRET "$(openssl rand -hex 24)"
[ -n "$(get_env N8N_ENCRYPTION_KEY)" ] || set_env N8N_ENCRYPTION_KEY "$(openssl rand -hex 24)"
if [ -z "$(get_env DOMAIN)" ]; then
  IP="$(curl -4 -fsS https://api.ipify.org || curl -4 -fsS https://ifconfig.me)"
  set_env DOMAIN "${IP//./-}.sslip.io"   # tên miền miễn phí trỏ về IP của VPS
fi
DOMAIN="$(get_env DOMAIN)"
TOKEN="$(get_env TELEGRAM_BOT_TOKEN)"
mkdir -p data && chown -R 1000:1000 data

say "6/8 Dựng và khởi động Caddy + n8n + worker (lần đầu mất 3–8 phút)"
docker compose up -d --build --remove-orphans

wait_n8n() {
  for _ in $(seq 1 90); do
    docker compose exec -T n8n wget -qO- http://localhost:5678/healthz >/dev/null 2>&1 && return 0
    sleep 3
  done
  warn "n8n chưa sẵn sàng — xem log: docker compose logs n8n"; return 1
}
wait_n8n

say "7/8 Nạp workflow n8n và bật chạy"
# Chỉ nạp lần đầu, để không ghi đè chỉnh sửa bạn làm trong giao diện n8n.
# Muốn nạp lại bản mới nhất từ GitHub:  UPDATE_WORKFLOW=1 bash deploy/install.sh
if [ "${UPDATE_WORKFLOW:-0}" = 1 ] || ! docker compose exec -T n8n n8n list:workflow 2>/dev/null | grep -q "^$WF_ID|"; then
  docker compose exec -T n8n n8n import:workflow --input=/workflows/tiktok-agent.json 2>&1 | grep -E "imported|Error" || true
  docker compose exec -T n8n n8n publish:workflow --id="$WF_ID" 2>&1 | grep -E "Publishing|Error" || true
  docker compose restart n8n >/dev/null
  wait_n8n
else
  echo "  Workflow đã có sẵn — giữ nguyên (đặt UPDATE_WORKFLOW=1 để nạp lại)."
fi

say "8/8 Đăng ký webhook Telegram → https://$DOMAIN/webhook/telegram"
for _ in $(seq 1 40); do  # chờ Caddy xin xong chứng chỉ HTTPS
  curl -fsS -o /dev/null "https://$DOMAIN/healthz" 2>/dev/null && break
  sleep 3
done
curl -fsS -o /dev/null "https://$DOMAIN/healthz" 2>/dev/null || warn "Chưa vào được https://$DOMAIN — kiểm tra đã mở cổng 80/443 trên trang quản lý VPS (xem HUONG_DAN.md)."
RES="$(curl -fsS "https://api.telegram.org/bot$TOKEN/setWebhook" \
  --data-urlencode "url=https://$DOMAIN/webhook/telegram" \
  --data-urlencode "secret_token=$(get_env TELEGRAM_WEBHOOK_SECRET)" \
  --data-urlencode 'allowed_updates=["message","callback_query"]' 2>&1 || true)"
echo "$RES" | grep -q '"ok":true' && echo "  Telegram: OK" || warn "Đăng ký webhook lỗi: $RES"

cat > /etc/cron.d/tiktok-agent-cleanup <<CRON
# Xoá video/clip cũ hơn 14 ngày mỗi đêm cho ổ đĩa không đầy
30 3 * * * root find $APP/data/videos $APP/data/clips -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} + 2>/dev/null
CRON

echo
echo "✅ XONG!"
echo "   • Giao diện n8n:  https://$DOMAIN   (lần đầu: tạo tài khoản quản trị)"
if [ -z "$(get_env TELEGRAM_CHAT_ID)" ]; then
  echo "   • Mở Telegram, gửi /start cho bot → bot trả lời Chat ID."
  echo "     Rồi chạy:  cd $APP && nano .env   (điền TELEGRAM_CHAT_ID=...)  và chạy lại lệnh cài."
else
  echo "   • Mở Telegram, gửi /kehoach cho bot để lập kế hoạch 30 ngày đầu tiên."
fi
echo "   • Link kết nối TikTok (Redirect URI):  https://$DOMAIN/tiktok/callback"
