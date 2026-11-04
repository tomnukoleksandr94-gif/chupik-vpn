"""Веб-акаунти для сайту (email + пароль) — реєстрація та вхід.

Веб-користувач отримує синтетичний tg_id (негативне число), тому вся логіка
(підписки, пристрої, платежі, реферали) спільна з Telegram-користувачами.

Безпека:
- паролі зберігаються як PBKDF2-HMAC-SHA256 (200k ітерацій + сіль);
- сесії — випадкові токени (secrets.token_urlsafe), термін життя обмежений;
- пароль/хеш ніколи не потрапляють у логи.
"""
import hashlib
import hmac
import re
import secrets
import time

from . import core
from .config import settings
from .db import ex, q1

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 6
# синтетичні ID: -(10 000 000 000 + id) — не перетинаються з реальними Telegram ID
_WEB_ID_OFFSET = 10_000_000_000


class AuthError(Exception):
    """Помилка реєстрації/входу (текст можна показувати клієнту)."""


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


def _password_fields(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"{salt.hex()}${_hash_password(password, salt)}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest = stored.split("$", 1)
        calc = _hash_password(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(calc, digest)
    except Exception:
        return False


def register(email: str, password: str, referrer_id: int | None = None) -> dict:
    """Створює веб-акаунти → сесійний токен + дані користувача."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Некоректний email")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError(f"Пароль має містити щонайменше {MIN_PASSWORD_LEN} символів")
    if q1("SELECT 1 FROM web_users WHERE email=?", (email,)):
        raise AuthError("Користувач з таким email вже існує — увійдіть")

    cur = ex("INSERT INTO web_users(email,password_hash,tg_id,created_at) VALUES(?,?,?,?)",
             (email, _password_fields(password), 0, int(time.time())))
    web_id = cur.lastrowid
    tg_id = -(_WEB_ID_OFFSET + web_id)
    # спільний запис користувача (як у Telegram, мова сайту за замовчуванням)
    core.upsert_user(tg_id, email.split("@")[0], email.split("@")[0], lang="uk")
    ex("UPDATE web_users SET tg_id=? WHERE id=?", (tg_id, web_id))

    # рефералка: новий веб-користувач зареєструвався за посиланням
    if referrer_id:
        core.register_referral(int(referrer_id), tg_id)

    token = _issue_session(email)
    return {"token": token, "tg_id": tg_id, "email": email}


def login(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    row = q1("SELECT * FROM web_users WHERE email=?", (email,))
    if not row or not _verify_password(password or "", row["password_hash"]):
        raise AuthError("Невірний email або пароль")
    return {"token": _issue_session(email), "tg_id": row["tg_id"], "email": email}


def _issue_session(email: str) -> str:
    # закрито старі сесії цього ж акаунта (не більше 10)
    old = ex("SELECT token FROM web_sessions WHERE email=? ORDER BY created_at DESC", (email,))
    tokens = [r["token"] for r in old.fetchall()] if old else []
    for t in tokens[9:]:
        ex("DELETE FROM web_sessions WHERE token=?", (t,))
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    ex("INSERT INTO web_sessions(token,email,created_at,expires_at) VALUES(?,?,?,?)",
       (token, email, now, now + settings.WEB_SESSION_DAYS * 86400))
    return token


def logout(token: str):
    ex("DELETE FROM web_sessions WHERE token=?", (token,))


def resolve_session(token: str) -> dict | None:
    """Токен → {tg_id, email} або None. Видалення прострочених сесій."""
    if not token:
        return None
    row = q1("SELECT * FROM web_sessions WHERE token=?", (token,))
    if not row:
        return None
    if row["expires_at"] < time.time():
        ex("DELETE FROM web_sessions WHERE token=?", (token,))
        return None
    user = q1("SELECT tg_id FROM web_users WHERE email=?", (row["email"],))
    if not user:
        return None
    return {"tg_id": user["tg_id"], "email": row["email"]}
