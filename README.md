# ЧУПИК VPN — сайт + кабінет + Telegram Mini App + бот + backend (WireGuard + платежі)

Репозиторій проєкту **CHUPIK VPN**: сайт із реєстрацією/входом та особистим кабінетом,
Telegram Mini App, бот, реальний VPN-бэкенд з WireGuard і двома платіжними системами
(Telegram Stars + картка: LiqPay або Stripe).

> 🚀 **Запуск наживо (безкоштовно): див. [DEPLOY.md](DEPLOY.md)** — Oracle Cloud Always Free + WireGuard + LiqPay/Stripe.

## Що всередині

| Шлях | Що це |
|---|---|
| `index.html` | Основний сайт (GitHub Pages): тарифи, FAQ, посилання на кабінет |
| `cabinet.html` | **Веб-кабінет сайту (premium dashboard)**: сайдбар-навігація, приладдя, пристрої (з типами Windows/Android/iOS/macOS/Linux), сервери, тарифи, групи, реферали, тікети підтримки, профіль (8 мов, сповіщення, зміна/скидання пароля, прив'язка Telegram, історія входів, видалення акаунта) |
| `miniapp/index.html` | Telegram Mini App: карта світу, сервери, пристрої, QR-коди, тарифи, профіль, реферали |
| `bot/bot.py` | Статичний бот (застарілий, залишено для історії) — робочий бот тепер у `server/` |
| `server/` | **Backend**: FastAPI + aiogram + WireGuard-менеджер + платежі (Stars, LiqPay, Stripe) |
| `server/api.py` | FastAPI: REST API для Mini App і сайту (Bearer-сесії), webhook-и, статика адмінки |
| `server/webauth.py` | Веб-акаунти сайту: реєстрація/вхід, PBKDF2-паролі, сесії |
| `server/bot_app.py` | Telegram-бот (aiogram 3): реєстрація, оплати Stars, реферали, 8 мов, кеш фото |
| `server/wg.py` | Менеджер WireGuard: генерація ключів, peer'и через SSH або локально, статистика |
| `server/payments.py` | Платежі: Telegram Stars (XTR) + LiqPay + Stripe Checkout + webhook-и з ідемпотентністю |
| `server/core.py` | Бізнес-логіка: підписки, ліміти пристроїв, реферали, expiry-воркер, мови/сповіщення |
| `server/groups.py` | **Групи**: слоти за тарифом (BASIC=1, PREMIUM=3, ULTIMATE=5), одноразові запрошення, автовідкликання VPN при закінченні підписки власника |
| `server/db.py` | SQLite з міграціями — users, devices, servers, payments, referral_events, web_users, web_sessions, groups, group_invites, group_members, tickets, ticket_messages, web_logins, web_reset_tokens |
| `server/admin/index.html` | Адмін-панель (потребує ADMIN_TOKEN) |
| `server/deploy_wg.sh` | Скрипт налаштування WireGuard на VPS |
| `.env.example` | Шаблон змінних середовища (скопіюй у `.env`) |
| `server/render.yaml` | Деплой backend на Render |
| `tests/test_e2e.py` | e2e-тести (backend + API + платежі + веб-акаунти) |

## Посилання

- Сайт: https://tomnukoleksandr94-gif.github.io/chupik-vpn/
- Кабінет (реєстрація/вхід): https://tomnukoleksandr94-gif.github.io/chupik-vpn/cabinet.html
- Mini App: https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/
- Бот: https://t.me/Chupik_vpnBot

## Архітектура

```
GitHub Pages (статика)          Backend (VPS/Render)                 VPS з WireGuard
├─ сайт (index.html)   ──API──▶  FastAPI :8766                       wg0 (UDP 51820)
├─ cabinet.html ◀─Bearer сесії──  ├─ /api/*  (Mini App + сайт)         ▲
├─ miniapp/  ◀──X-Init-Data────   ├─ бот (aiogram polling)            SSH (peer add/remove)
└─ адмінка (server/admin)───────▶ ├─ LiqPay/Stripe webhook             │
                                   └─ expiry-воркер (кожні 60 с)        │
```

- **Один процес** FastAPI одночасно обслуговує бот (polling), API та воркер.
- **Два способи входу в API**: підпис Telegram initData (Mini App) і Bearer-токен веб-сесії (сайт). Веб-акаунт отримує синтетичний негативний ID і користується тією ж логікою підписок/пристроїв.
- **Один канал активації**: Stars, LiqPay і Stripe викликають `activate_subscription()` — оплата проходить лише після підтвердження (successful_payment / webhook), захист від повторної обробки через `provider_ref UNIQUE`.
- **Картка — тільки на сайті** (LiqPay Checkout або Stripe Checkout), у Telegram — лише Stars, за правилами Telegram.
- **LiqPay** — виплати на картку ПриватБанку (карта прив'язується в кабінеті liqpay.ua). **Stripe** — альтернатива з виплатами на IBAN.
- Ключі клієнтів WireGuard шифруються (Fernet) у БД; `SECRET_KEY` у змінних оточення. Паролі сайту — PBKDF2 (200k ітерацій).

## Запуск backend

```bash
cd server
cp ../.env.example ../.env   # заповни BOT_TOKEN, SECRET_KEY, ADMIN_TOKEN
pip install -r requirements.txt
python api.py         # uvicorn на :8766 + бот + expiry-воркер
```

### Змінні середовища (див. `.env.example`)

| Змінна | Для чого |
|---|---|
| `BOT_TOKEN` | токен бота (ніколи не комітити) |
| `SECRET_KEY` | шифрування ключів WireGuard + валідація initData |
| `ADMIN_TOKEN` | пароль адмін-панелі `/admin` |
| `BASE_URL` | публічний URL backend (для webhook-ів оплати; для карток — обов'язково HTTPS) |
| `WEB_SITE_URL` | адреса сайту (реферальні посилання веб-акаунтів) |
| `LIQPAY_PUBLIC_KEY`, `LIQPAY_PRIVATE_KEY` | карткові платежі з виплатами на картку ПриватБанку |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | карткові платежі (альтернатива LiqPay) |
| `WG_DRIVER` | `local` (wg на цій же машині) або `ssh` (VPS) |
| `WG_SSH_HOST`, `WG_SSH_USER`, `WG_SSH_KEY` | підключення до VPS з WireGuard |
| `DB_PATH` | файл SQLite |

### Додати VPS з WireGuard

1. Орендуй VPS (Ubuntu 22.04+, 1 vCPU / 1 GB RAM достатньо), відкрий UDP 51820.
2. На VPS: `bash deploy_wg.sh` — поставить WireGuard, покаже public key і endpoint.
3. Створи користувача `chupik` і поклади public-ключ backend у `authorized_keys`.
4. У БД (адмінка `/admin`) додай сервер: public key, endpoint, subnet `10.7.0.0/24`.
5. У `.env`: `WG_DRIVER=ssh WG_SSH_HOST=<IP> WG_SSH_USER=chupik`.

### Тести

```bash
cd tests && python test_e2e.py   # e2e: API, підписки, платежі (Stars/Stripe/LiqPay), веб-акаунти, ідемпотентність
```

## Безпека токена — ВАЖЛИВО

Токен, надісланий у чаті, вже вважається скомпрометованим. Після налагодження:
1. @BotFather → `/mybots` → Chupik_vpnBot → **API Token → Revoke current token**.
2. Оновіть `BOT_TOKEN` у змінних середовища хостингу.

Те саме стосується номеру картки: ніколи не зберігайте номер карти в коді чи чаті —
виплати на неї налаштовуються в кабінеті платіжного провайдера (LiqPay).

## Що далі

- Запуск backend на постійному VPS (безкоштовний варіант — Oracle Always Free, див. DEPLOY.md)
- Реєстрація LiqPay (виплати на карту) або Stripe — потребує рішення власника
- Додавання реальних WireGuard-серверів через адмінку
