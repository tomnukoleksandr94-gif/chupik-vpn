# ЧУПИК VPN — сайт + Telegram Mini App + бот

Репозиторій проєкту **CHUPIK VPN** (етап 1 за ТЗ).

## Що всередині

| Шлях | Що це |
|---|---|
| `index.html` | Основний сайт (GitHub Pages) |
| `miniapp/index.html` | Telegram Mini App: карта світу, сервери, пристрої, тарифи, профіль, реферали |
| `miniapp/chupik-brand.jpg` | Фірмове зображення (використовується ботом для /start) |
| `bot/bot.py` | Telegram-бот на Python (aiogram 3): реєстрація, профіль, рефералки, 8 мов, кнопка Mini App |
| `bot/render.yaml` | Деплой бота на Render (безкоштовний тариф) |

## Посилання (після увімкнення GitHub Pages)

- Сайт: https://tomnukoleksandr94-gif.github.io/chupik-vpn/
- Mini App: https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/
- Бот: https://t.me/Chupik_vpnBot

## Як підключити Mini App до бота (через @BotFather)

1. Відкрий @BotFather → `/mybots` → Chupik_vpnBot.
2. **Bot Settings → Menu Button** → URL: `https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/` (вже налаштовано автоматично).
3. **Bot Settings → Configure Mini App** (за потреби) — той самий URL.

Кнопка меню бота (зліва внизу біля 🎈) одразу відкриє Mini App.

## Як запустити бота

Токен зберігається **тільки у змінних оточення** — репозиторій публічний, токен у коді = викрадений бот.

```bash
cd bot
pip install -r requirements.txt
export BOT_TOKEN="твій_токен_от_BotFather"
export MINIAPP_URL="https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/"
python bot.py
```

### Безкоштовний хостинг бота 24/7 (Render)

1. Зареєструйся на [render.com](https://render.com) (безкоштовно, прив'язка картки не потрібна).
2. **New → Blueprint** → вибери цей репозиторій — Render сам прочитає `bot/render.yaml`.
3. У змінних середовища додай `BOT_TOKEN` (секретне значення).
4. Deploy — бот працюватиме цілодобово (на free-плані він засинає без активності ~15 хв, потім прокидається на перше повідомлення).

### Чому не Glitch

Glitch закрив безкоштовний хостинг застосунків у 2025 році. Безкоштовна комбінація зараз: **GitHub Pages** (сайти) + **Render** (бот).

## Безпека токена — ВАЖЛИВО

Токен, надісланий у чаті, вже вважається скомпрометованим. Після налагодження:
1. @BotFather → `/mybots` → Chupik_vpnBot → **API Token → Revoke current token**.
2. Оновіть `BOT_TOKEN` у Render (Environment).

## Структура за ТЗ (далі)

- Етап 2: WireGuard-сервери, видача/відклик конфігурацій
- Етап 3: оплата Stars (XTR) + картка на сайті
- Етап 4–5: розширення Mini App, адмін-панель
