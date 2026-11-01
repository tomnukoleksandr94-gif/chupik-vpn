#!/usr/bin/env bash
# CHUPIK VPN — інсталятор "однією командою" на свіжий Ubuntu VPS (22.04/24.04)
# Запуск від root:
#   bash <(curl -fsSL https://raw.githubusercontent.com/tomnukoleksandr94-gif/chupik-vpn/main/install.sh)
# Робить ВСІ кроки DEPLOY.md: WireGuard + backend + systemd + автозапуск.
# Секрети генерує сам (openssl rand), від тебе потрібен лише BOT_TOKEN.

set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "❌ Запусти від root: sudo bash install.sh"; exit 1; }

REPO="https://github.com/tomnukoleksandr94-gif/chupik-vpn.git"
DIR="/opt/chupik-vpn"
SERVICE="chupik-vpn"

echo "== CHUPIK VPN: інсталяція одним рядком =="

# 1) Код
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv curl openssl >/dev/null
if [ -d "$DIR" ]; then git -C "$DIR" pull -q; else git clone -q "$REPO" "$DIR"; fi
cd "$DIR/server"

# 2) WireGuard (wg0, UDP 51820)
bash deploy_wg.sh

# 3) .env — генеруємо секрети автоматично
cd "$DIR"
IP="$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
if [ ! -f .env ]; then
  read -rp "Встав BOT_TOKEN (від @BotFather, /mybots → API Token): " BOT_TOKEN
  [ -n "$BOT_TOKEN" ] || { echo "❌ BOT_TOKEN обов'язковий"; exit 1; }
  SECRET="$(openssl rand -hex 32)"
  ADMIN="$(openssl rand -hex 12)"
  cat > .env <<EOF
BOT_TOKEN=$BOT_TOKEN
SECRET_KEY=$SECRET
ADMIN_TOKEN=$ADMIN
BASE_URL=http://$IP:8766
MINIAPP_URL=https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/
BRAND_PHOTO_URL=https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/chupik-brand.jpg
DB_PATH=$DIR/chupik.db
WG_DRIVER=local
WG_IFACE=wg0

STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
EOF
  chmod 600 .env
  echo "✅ .env створено: SECRET_KEY і ADMIN_TOKEN згенеровані автоматично"
  echo "   ADMIN_TOKEN (пароль адмінки, збережи): $ADMIN"
fi

# 4) Backend + systemd
cd "$DIR/server"
bash deploy_backend.sh

# 5) Друкуємо підсумок
PUBKEY="$(cat /etc/wireguard/wg0.pub 2>/dev/null || echo '?')"
echo
echo "=========================================================="
echo "  CHUPIK VPN запущено!"
echo "  IP сервера:     $IP"
echo "  API:            http://$IP:8766/api/health"
echo "  Адмінка:        http://$IP:8766/admin   (пароль: ADMIN_TOKEN з .env)"
echo "  WG public key:  $PUBKEY   (додай сервер в адмінці)"
echo "  WG endpoint:    $IP:51820"
echo ""
echo "  Кнопка меню бота (вистав через @BotFather → Menu Button):"
echo "  https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/?api=http://$IP:8766"
echo "=========================================================="
