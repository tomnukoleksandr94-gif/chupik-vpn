"""Налаштування CHUPIK VPN backend — усе через змінні середовища (секрети НЕ в коді)."""
import os


class Settings:
    # Telegram
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    MINIAPP_URL = os.environ.get(
        "MINIAPP_URL", "https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/")
    BRAND_PHOTO_URL = os.environ.get(
        "BRAND_PHOTO_URL",
        "https://tomnukoleksandr94-gif.github.io/chupik-vpn/miniapp/chupik-brand.jpg")

    # Захист
    SECRET_KEY = os.environ.get("SECRET_KEY", "")          # підпис сесій + шифрування ключів WG
    ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")        # доступ до адмін-панелі

    # Telegram ID, які АВТОМАТИЧНО стають адмінами (через кому)
    ADMIN_TG_IDS = {
        int(x) for x in os.environ.get("ADMIN_TG_IDS", "").replace(" ", "").split(",")
        if x.strip().isdigit()
    }

    # Файл з актуальною публічною URL (пишеться keeper-ом тунелю; для /admin-посилань)
    PANEL_URL_FILE = os.environ.get("PANEL_URL_FILE", "")

    # База
    DB_PATH = os.environ.get("DB_PATH", "chupik.db")

    # Публічна URL цього backend (для Stripe success_url)
    BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

    # Stripe (оплата картою на сайті). Пусто = вимкнено.
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # SSH для керування VPN-серверами
    WG_SSH_USER = os.environ.get("WG_SSH_USER", "root")
    WG_SSH_KEY = os.environ.get("WG_SSH_KEY", "")           # вміст приватного ключа або шлях

    # Наскрізна перевірка підписок (сек)
    EXPIRY_CHECK_SECONDS = int(os.environ.get("EXPIRY_CHECK_SECONDS", "60"))


settings = Settings()

if not settings.SECRET_KEY or not settings.ADMIN_TOKEN:
    # на етапі розробки/тестів генеруємо ефемерні, у проді обов'язкові через env
    if not settings.SECRET_KEY:
        settings.SECRET_KEY = os.environ.get("EPHEMERAL_SECRET", "dev-secret-do-not-use-in-prod")
    if not settings.ADMIN_TOKEN:
        settings.ADMIN_TOKEN = os.environ.get("EPHEMERAL_ADMIN", "dev-admin-token")
