#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash scripts/install.sh"
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip nginx

if [[ ! -d "$APP_DIR/.venv" ]]; then
  sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$RUN_USER":"$RUN_USER" "$APP_DIR/.env"
  echo "Created $APP_DIR/.env -- fill in DASHSCOPE_API_KEY and DASHSCOPE_WORKSPACE_ID"
fi

cat > /etc/systemd/system/qwen-live-translate.service <<EOF
[Unit]
Description=Qwen Live Translate Pad
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo
echo "Install complete."
echo "1) Edit: $APP_DIR/.env"
echo "2) Test: bash $APP_DIR/scripts/run.sh"
echo "3) Service: sudo systemctl enable --now qwen-live-translate"
echo "4) For browser microphone access, serve it through HTTPS."
