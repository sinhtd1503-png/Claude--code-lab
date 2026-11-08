#!/usr/bin/env bash
# Cài TikTok Content Agent lên VPS Ubuntu 22.04/24.04. Chạy bằng root:
#   curl -fsSL https://raw.githubusercontent.com/sinhtd1503-png/Claude--code-lab/main/deploy/setup.sh | bash
set -euo pipefail

REPO="https://github.com/sinhtd1503-png/Claude--code-lab.git"
APP=/opt/tiktok-agent

[ "$(id -u)" -eq 0 ] || { echo "Hãy chạy bằng root (sudo -i)"; exit 1; }

echo "==> Cài gói hệ thống"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv python3-pip ffmpeg fonts-dejavu-core sqlite3 >/dev/null

echo "==> Tạo user 'agent' (không có quyền root)"
id agent >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash agent

echo "==> Tải code"
if [ -d "$APP/.git" ]; then
  sudo -u agent git -C "$APP" pull --ff-only
else
  mkdir -p "$APP" && chown agent:agent "$APP"
  sudo -u agent git clone -q "$REPO" "$APP"
fi

echo "==> Cài thư viện Python"
sudo -u agent python3 -m venv "$APP/.venv"
sudo -u agent "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u agent "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "==> Cài service systemd"
install -m 644 "$APP/deploy/tiktok-agent.service" /etc/systemd/system/tiktok-agent.service
systemctl daemon-reload
systemctl enable tiktok-agent >/dev/null

echo "==> Dọn video cũ hơn 14 ngày mỗi đêm (giữ ổ đĩa không đầy)"
cat > /etc/cron.d/tiktok-agent-cleanup <<CRON
30 3 * * * agent find $APP/data/videos -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} + 2>/dev/null
CRON

if [ ! -f "$APP/.env" ]; then
  cat <<MSG

✅ Cài xong. Còn 2 bước:
  1) Đưa file .env vào $APP/.env   (xem hướng dẫn)
  2) chown agent:agent $APP/.env && chmod 600 $APP/.env && systemctl start tiktok-agent
MSG
else
  chown agent:agent "$APP/.env" && chmod 600 "$APP/.env"
  systemctl restart tiktok-agent
  echo "✅ Đã khởi động lại bot. Xem log: journalctl -u tiktok-agent -f"
fi
