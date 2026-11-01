#!/usr/bin/env bash
# CHUPIK VPN — деплой WireGuard-сервера на VPS
# Запуск: bash deploy_wg.sh  (на самому VPS, від root)
# Скрипт: встановлює WireGuard, створює wg0, відкриває порт, готує SSH-ключ для backend.

set -euo pipefail

WG_IFACE="${WG_IFACE:-wg0}"
WG_PORT="${WG_PORT:-51820}"
WG_SUBNET="${WG_SUBNET:-10.7.0.0/24}"        # підсіть клієнтів
WG_SERVER_IP="${WG_SERVER_IP:-10.7.0.1/24}"   # адреса сервера в тунелі
NIC="${NIC:-$(ip -4 route show default | awk '{print $5; exit}')}"

echo "== CHUPIK VPN: WireGuard setup =="
echo "   iface=$WG_IFACE port=$WG_PORT subnet=$WG_SUBNET nic=$NIC"

if ! command -v wg >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq wireguard qrencode 2>/dev/null || apt-get install -y wireguard-tools
fi

# 1) Ключі сервера (генеруються, якщо ще нема)
PRIV="/etc/wireguard/${WG_IFACE}.conf"
if [ ! -f "$PRIV" ]; then
  umask 077
  wg genkey | tee "/etc/wireguard/${WG_IFACE}.key" | wg pubkey > "/etc/wireguard/${WG_IFACE}.pub"
  cat > "$PRIV" <<EOF
[Interface]
Address = ${WG_SERVER_IP}
ListenPort = ${WG_PORT}
PrivateKey = $(cat /etc/wireguard/${WG_IFACE}.key)
SaveConfig = false
# NAT для клієнтського трафіку:
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -t nat -A POSTROUTING -o ${NIC} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -t nat -D POSTROUTING -o ${NIC} -j MASQUERADE
EOF
  echo 1 > /proc/sys/net/ipv4/ip_forward
  grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
  sysctl -p -q
fi

# 2) Запуск інтерфейсу
wg-quick up "$WG_IFACE" 2>/dev/null || true
systemctl enable "wg-quick@${WG_IFACE}" 2>/dev/null || true

# 3) Firewall: дозволити SSH, WireGuard і HTTP/HTTPS (для backend, якщо тут же)
if command -v ufw >/dev/null; then
  ufw allow 22/tcp >/dev/null 2>&1 || true
  ufw allow ${WG_PORT}/udp >/dev/null 2>&1 || true
  ufw allow 80/tcp  >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
fi

echo
echo "== Готово =="
echo "Server public key: $(cat /etc/wireguard/${WG_IFACE}.pub)"
echo "Endpoint: <ЦЕЙ_VPS_IP>:${WG_PORT}"
echo
echo "Наступні кроки:"
echo "1) Додай сервер у БД backend (admin-панель): public key вище, endpoint IP, subnet ${WG_SUBNET}"
echo "2) Створи окремого користувача 'chupik' (не root) для SSH від backend:"
echo "     useradd -m -s /bin/bash chupik && mkdir -p /home/chupik/.ssh"
echo "     # поклади public ключ backend у /home/chupik/.ssh/authorized_keys, chmod 600"
echo "   і в .env backend: WG_DRIVER=ssh WG_SSH_HOST=<VPS_IP> WG_SSH_USER=chupik"
