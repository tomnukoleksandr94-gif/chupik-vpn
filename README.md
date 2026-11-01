# ЧУПИК VPN — сайт + Telegram Mini App + бот + backend (WireGuard + платежі)

Репозиторій проєкту **CHUPIK VPN** (етапи 1–4 за ТЗ: сайт, Mini App, бот, реальний VPN-бэкенд з двома платіжними системами).

## Що всередині

| Шлях | Що це |
|---|---|
| `index.html` | Основний сайт (GitHub Pages) |
| `miniapp/index.html` | Telegram Mini App: карта світу, сервери, пристрої, QR-коди, тарифи, профіль, реферали |
| `bot/bot.py` | Статичний бот (застарілий, залишено для історії) — робочий бот тепер у `server/` |
| `server/` | **Backend**: FastAPI + aiogram + WireGuard-менеджер + платежі (Stars і Stripe) |
| `server/api.py` | FastAPI-застосунок: REST API для Mini App, Stripe webhook, статика адмінки |
| `server/bot_app.py` | Telegram-бот (aiogram 3): реєстрація, оплати Stars, реферали, 8 мов |
| `server/wg.py` | Менеджер WireGuard: генерація ключів, peer'и через SSH або локально, статистика |
| `server/payments.py` | Платежі: Telegram Stars (XTR) + Stripe Checkout + webhook з ідемпотентністю |
| `server/core.py` | Бізнес-логіка: підписки, ліміти пристроїв, реферали, expiry-воркер |
| `server/db.py` | SQLite (async) з міграціями — users, devices, servers, payments, referral_events |
| `server/admin/index.html` | Адмін-панель (потребує ADMIN_TOKEN) |
| `server/deploy_wg.sh` | Скрипт налаштування WireGuard на VPS |
| `.env.example` | Шаблон змінних середовища (скопіюй у `.env`) |
| `server/render.yaml` | Деплой backend на Render |
| `tests/test_e2e.py` | 37 e2e-тестів (backend + API + платежі) |

## Посилання

- Сайт: https://tomnukoleksandr94-gif.github.io/chupik-vpn/
- Mini App: https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/
- Бот: https://t.me/Chupik_vpnBot

## Архітектура

```
GitHub Pages (статика)          Backend (Render/VPS)                VPS з WireGuard
├─ сайт (index.html)   ──/pay──▶ FastAPI :8766                     wg0 (UDP 51820)
├─ miniapp/  ◀──REST API─────  ├─ /api/*  (Mini App, X-Init-Data)   ▲
└─ адмінка (server/admin)────▶  ├─ бот (aiogram polling)            SSH (peer add/remove)
                               ├─ Stripe webhook (підпис перевіряється)
                               └─ expiry-воркер (кожні 60 с: downgrade + revoke)
```

- **Один процес** FastAPI одночасно обслуговує бот (polling), API та воркер.
- **Один канал активації**: і Stars, і Stripe викликають `activate_subscription()` — оплата проходить лише після підтвердження (successful_payment / webhook), захист від повторної обробки через `provider_ref UNIQUE`.
- **Картка — тільки на сайті** (`/pay?plan=...` → Stripe Checkout), у Telegram — лише Stars, за правилами Telegram.
- Ключі клієнтів WireGuard шифруються (Fernet) у БД; `SECRET_KEY` у змінних оточення.

## Запуск backend

```bash
cd server
cp ../.env.example ../.env   # заповни BOT_TOKEN, SECRET_KEY, ADMIN_TOKEN
pip install -r requirements.txt
python api.py         # uvicorn на :8766 + бот + expiry-воркер
```

### Змінні середовища (див. `server/.env.example`)

| Змінна | Для чого |
|---|---|
| `BOT_TOKEN` | токен бота (ніколи не комітити) |
| `SECRET_KEY` | шифрування ключів WireGuard + валідація initData |
| `ADMIN_TOKEN` | пароль адмін-панелі `/admin` |
| `BASE_URL` | публічний URL backend (для Stripe webhook/redirect) |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | карткові платежі (поки не задані — Stripe вимкнено) |
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
cd tests && python test_e2e.py   # 37 тестів: API, підписки, платежі, ідемпотентність
```

## Безпека токена — ВАЖЛИВО

Токен, надісланий у чаті, вже вважається скомпрометованим. Після налагодження:
1. @BotFather → `/mybots` → Chupik_vpnBot → **API Token → Revoke current token**.
2. Оновіть `BOT_TOKEN` у змінних середовища хостингу.

## Що далі

- Публічний деплой backend (Render / VPS) — потребує рішення власника
- Реєстрація Stripe (або MoR: Paddle/LemonSqueezy) — потребує рішення власника
- Оновити ціни на сайті з "—" на реальні після запуску платежів
