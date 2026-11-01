# CHUPIK VPN — запуск наживо (безкоштовний варіант)

Повна інструкція: як підняти сервіс на 100% безкоштовно.
Витрати: **€0/міс** (Oracle Always Free + GitHub Pages + Render не потрібен, якщо backend на VPS).

## Архітектура

```
GitHub Pages (безкоштовно)         Oracle Cloud Always Free (безкоштовно)
├─ сайт                            ├─ WireGuard (wg0, UDP 51820)
├─ miniapp/?api=http://IP:8766 ──▶ ├─ backend FastAPI :8766 (бот + API + воркер)
└─ адмінка /admin                  └─ SQLite на диску VPS (дані не зникають)
```

## Крок 1. Oracle Cloud (безкоштовний VPS)

1. Зайди на [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) → Start for free.
   - Потрібна банківська карта для верифікації (списують $1 і повертають — це холд, не оплата).
2. Створи обчислювальний інстанс:
   - Shape: **VM.Standard.A1.Flex** (ARM), 2 OCPU / 12 GB RAM — влізає в Always Free.
   - Image: Ubuntu 22.04/24.04, додай SSH-ключ (свій public key).
   - Security List / NSG: відкрий порти **22 (TCP)**, **8766 (TCP)**, **51820 (UDP)**.
   - Увага: у популярних регіонах A1 буває "Out of capacity" — тоді спробуй інший регіон (напр. Frankfurt, Amsterdam, Paris) або повтори пізніше.
3. Залишок лимітів Always Free: 200 ГБ диску, 10 ТБ трафіку/міс — з головою для VPN.

Перевірка: `ssh ubuntu@<IP>` працює.

## Крок 2. WireGuard на VPS

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/tomnukoleksandr94-gif/chupik-vpn.git
cd chupik-vpn/server
sudo bash deploy_wg.sh        # поставить WireGuard, покаже public key і endpoint
```

## Крок 3. Backend на тому ж VPS

```bash
cd ~/chupik-vpn
cp .env.example .env
nano .env    # заповни:
#   BOT_TOKEN=...           (від @BotFather)
#   SECRET_KEY=...           (openssl rand -hex 32)
#   ADMIN_TOKEN=...          (свій пароль адмінки)
#   WG_DRIVER=local          (WireGuard на цій же машині)
#   BASE_URL=http://<IP>:8766
cd server
sudo bash deploy_backend.sh
```

Далі:
1. Відкрий `http://<IP>:8766/admin` → додай сервер: public key з кроку 2, endpoint `<IP>:51820`, subnet `10.7.0.0/24`.
2. У BotFather (або через адмінку) вистав кнопці меню бота URL:
   `https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/?api=http://<IP>:8766`
   — тоді Mini App одразу працює в LIVE-режимі.

**Обмеження без HTTPS:** Stars-оплати в боті працюють і без HTTPS, але Stripe (картки) вимагає HTTPS-адресу для webhook. Рішення безкоштовно:
- зареєструй безкоштовний субдомен на [duckdns.org](https://www.duckdns.org/) (напр. `chupik-vpn.duckdns.org`) → вкажи IP VPS;
- на VPS: `sudo apt install -y certbot && sudo certbot certonly --standalone -d chupik-vpn.duckdns.org` — і постав Caddy/nginx на 443 → 8766;
- у `.env` онови `BASE_URL=https://chupik-vpn.duckdns.org`.

## Крок 4. Оплата картками (Visa/Mastercard) — як це працює

"Visa" — це не окремий сервіс: покупці платять **своїми картками Visa/MC**, а ти отримуєш гроші через платіжного провайдера. Найпростіший для Франції — **Stripe**:

1. Зареєструйся на [stripe.com](https://stripe.com) (безкоштовно, потрібні паспорт/карта/IBAN для виплат).
2. Створи ".payment link" не треба — у нас вже все готово: увімкни Stripe у `.env`:
   - `STRIPE_SECRET_KEY=sk_live_...` (Stripe Dashboard → Developers → API keys)
   - `BASE_URL=https://<твій-домен>` (обов'язково HTTPS)
   - Webhook: Dashboard → Developers → Webhooks → `https://<твій-домен>/stripe/webhook`,
     подія `checkout.session.completed` → скопіюй signing secret у `STRIPE_WEBHOOK_SECRET`.
3. Перезапусти сервіс: `sudo systemctl restart chupik-vpn`.
4. На сайті кнопки "Обрати BASIC/PREMIUM/ULTIMATE" ведуть у бот — карткою платять через
   сторінку `https://<твій-домен>/pay?plan=...` (посилання додамо на сайт після запуску).

Комісія Stripe у ЄС: ~1.5% + €0.25 з платежу (без абонплати). Альтернатива — Paddle/LemonSqueezy
(Merchant of Record, самі платять ПДВ, комісія ~5%+€0.50) — якщо не хочеш возитися з ПДВ.

## Крок 5. Безпека токена (обов'язково)

Токен бота вже був надісланий у чат — він вважається скомпрометованим:
1. @BotFather → `/mybots` → Chupik_vpnBot → **API Token → Revoke current token**.
2. Онови `BOT_TOKEN` у `.env` на VPS → `sudo systemctl restart chupik-vpn`.

## Альтернатива: Render free (замість кроку 3)

Якщо хочеш саме Render: **New → Blueprint** → цей репозиторій (`server/render.yaml`).
⚠️ На Render free диск ефемерний — SQLite зтирається при кожному redeploy, а сервіс засинає
без вхідних запитів ~15 хв. Для бот-платежів це критично: потрібен зовнішній Postgres
(Supabase free) + пінгер (cron-job.org). Якщо обираєш цей шлях — напиши, я перенесу базу
на Postgres. Рекомендований шлях — крок 3 на VPS.

## Чек-лист запуску

- [ ] VPS Oracle створено, SSH працює
- [ ] `deploy_wg.sh` виконано, сервер додано в адмінці
- [ ] Backend запущено (`systemctl status chupik-vpn`)
- [ ] Кнопка меню бота веде на miniapp з `?api=`
- [ ] Тест: Stars-інвойс у боті оплачується (тестова покупка)
- [ ] Тест: пристрій додається, конфіг/QR завантажується, підключення працює
- [ ] Токен бота ревокнуто і замінений
