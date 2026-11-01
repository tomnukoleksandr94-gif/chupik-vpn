#!/usr/bin/env bash
# CHUPIK VPN — деплой backend на VPS (Ubuntu 22.04/24.04, разом з WireGuard)
# Запуск: sudo bash deploy_backend.sh   (у папці server/ на VPS)
# Створює venv, systemd-сервіс chupik-vpn (автозапуск, рестарт при падінні).

set -euo pipefail
cd "$(dirname "$0")"
SERVICE_NAME="chupik-vpn"
ENV_FILE="$(cd .. && pwd)/.env"   # .env у корені репозиторію

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ Не знайдено $ENV_FILE"
  echo "   Скопіюй: cp .env.example .env  і заповни BOT_TOKEN, SECRET_KEY, ADMIN_TOKEN"
  exit 1
fi

echo "== CHUPIK VPN: backend setup =="
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null

python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt

# systemd-сервіс: FastAPI на 0.0.0.0:8766 (бот + API + expiry-воркер в одному процесі)
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=CHUPIK VPN backend (FastAPI + aiogram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$(pwd)
EnvironmentFile=$ENV_FILE
ExecStart=$(pwd)/venv/bin/python api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}
sleep 3
systemctl --no-pager -l status ${SERVICE_NAME} | head -8
echo
echo "== Готово =="
echo "API:      http://<IP_ЦЬОГО_VPS>:8766/api/health"
echo "Адмінка:  http://<IP_ЦЬОГО_VPS>:8766/admin  (ADMIN_TOKEN)"
echo "Оплата:   http://<IP_ЦЬОГО_VPS>:8766/pay?plan=basic  (для Stripe потрібен HTTPS — див. DEPLOY.md)"
