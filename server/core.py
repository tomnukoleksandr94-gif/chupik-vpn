"""Ядро бізнес-логіки: тарифи, підписки, пристрої, реферали.

Один шлях активації для ОБОХ способів оплати (Stars і картка):
activate_subscription() — єдине місце, де змінюється тариф.
"""
import asyncio
import time

from . import db, wg
from .db import ex, q, q1

DAY = 86400

PLANS = {
    "free":     {"name": "FREE",     "price_eur": 0.0,   "devices": 1, "stars_key": None},
    "basic":    {"name": "BASIC",    "price_eur": 1.99,  "devices": 2, "stars_key": "stars_basic"},
    "premium":  {"name": "PREMIUM",  "price_eur": 4.99, "devices": 4, "stars_key": "stars_premium"},
    "ultimate": {"name": "ULTIMATE", "price_eur": 7.99, "devices": 5, "stars_key": "stars_ultimate"},
}

REF_TIERS = [  # (кількість запрошених, години PREMIUM-бонусу)
    (1, 3), (3, 12), (5, 24), (10, 72), (20, 168),
]

BOT = None  # інстанс aiogram Bot, ставиться при старті (для повідомлень користувачам)


# ---------------- користувачі ----------------

def get_user(tg_id: int):
    return q1("SELECT * FROM users WHERE tg_id=?", (tg_id,))


def upsert_user(tg_id: int, username=None, first_name=None, lang=None, referrer_id=None):
    u = get_user(tg_id)
    if u is None:
        ex("INSERT INTO users(tg_id,username,first_name,lang,referrer_id,created_at) VALUES(?,?,?,?,?,?)",
           (tg_id, username, first_name, lang or "ru", referrer_id, int(time.time())))
    else:
        ex("UPDATE users SET username=COALESCE(?,username), first_name=COALESCE(?,first_name) WHERE tg_id=?",
           (username, first_name, tg_id))
        if lang and u["lang"] in (None, "ru") and lang != "ru":
            ex("UPDATE users SET lang=? WHERE tg_id=?", (lang, tg_id))
    # автопідвищення адмінів за ADMIN_TG_IDS (один раз, логується)
    from .config import settings
    from .db import log_admin
    if tg_id in settings.ADMIN_TG_IDS:
        cur = get_user(tg_id)
        if cur and not cur["is_admin"]:
            ex("UPDATE users SET is_admin=1 WHERE tg_id=?", (tg_id,))
            log_admin(0, f"auto-admin granted to {tg_id} via ADMIN_TG_IDS")
    return get_user(tg_id)


def effective_plan(user) -> tuple:
    """(plan, expires_at) з урахуванням бонусного часу (VIP trial / реферали)."""
    now = int(time.time())
    plan = user["plan"] or "free"
    exp = user["plan_expires_at"]
    active = exp is not None and exp > now
    if not active:
        plan = "free"
    # бонуси діють лише поверх активної базової підписки не нижче BASIC (за ТЗ: бонус відкриває
    # можливості, але без активної підписки вищого рівня — обмежений доступ)
    bonus_plan, bonus_until = user["bonus_plan"], user["bonus_until"]
    if bonus_plan and bonus_until and bonus_until > now and plan in ("free", "basic", "premium"):
        rank = {"free": 0, "basic": 1, "premium": 2, "ultimate": 3}
        if rank.get(bonus_plan, 0) > rank.get(plan, 0):
            plan = bonus_plan
    return plan, (exp if active else None)


def device_limit(plan: str) -> int:
    return PLANS.get(plan, PLANS["free"])["devices"]


# ---------------- підписки ----------------

def activate_subscription(tg_id: int, plan: str, source: str, days: int = 30) -> dict:
    """ЄДИНА точка активації після підтвердженої оплати (Stars, картка або адмін)."""
    now = int(time.time())
    user = get_user(tg_id)
    if user is None:
        raise ValueError("user not found")
    # подовження того ж тарифу — накопичуємо дні
    cur_exp = user["plan_expires_at"] or 0
    if user["plan"] == plan and cur_exp > now:
        new_exp = cur_exp + days * DAY
    else:
        new_exp = now + days * DAY
    ex("UPDATE users SET plan=?, plan_expires_at=? WHERE tg_id=?", (plan, new_exp, tg_id))
    ex("INSERT INTO subscriptions(tg_id,plan,starts_at,expires_at,source,created_at) VALUES(?,?,?,?,?,?)",
       (tg_id, plan, now, new_exp, source, now))
    db.log_admin(source, "subscription_activated",
                 f"tg={tg_id} plan={plan} until={new_exp}")
    return {"plan": plan, "expires_at": new_exp}


def add_bonus(tg_id: int, plan: str, hours: float, reason: str):
    """VIP trial / реферальні нагороди: тимчасовий тариф поверх основного."""
    now = int(time.time())
    user = get_user(tg_id)
    cur = user["bonus_until"] or 0
    base = max(cur, now)
    new_until = base + int(hours * 3600)
    ex("UPDATE users SET bonus_plan=?, bonus_until=? WHERE tg_id=?", (plan, new_until, tg_id))
    db.log_admin(reason, "bonus_granted", f"tg={tg_id} plan={plan} hours={hours}")


def invited_count(tg_id: int) -> int:
    return q1("SELECT COUNT(*) c FROM referral_events WHERE referrer_id=? AND kind='joined'",
              (tg_id,))["c"]


def register_referral(referrer_id: int, invited_id: int) -> bool:
    """Записуємо, що запрошений користувач реально зареєструвався (без самозапрошень)."""
    if referrer_id == invited_id or not get_user(referrer_id):
        return False
    cur = ex("INSERT OR IGNORE INTO referral_events(referrer_id,invited_id,kind,created_at) "
             "VALUES(?,?,'joined',?)", (referrer_id, invited_id, int(time.time())))
    return cur.rowcount > 0


def on_invitee_paid(invited_id: int):
    """Після ПЕРШОЇ успішної оплати запрошеного — нараховуємо нагороду тому, хто запросив."""
    if db.get_setting("referral_enabled", "1") != "1":
        return
    row = q1("SELECT referrer_id FROM referral_events WHERE invited_id=? AND kind='joined'",
             (invited_id,))
    if not row:
        return
    referrer = row["referrer_id"]
    already = q1("SELECT 1 FROM referral_events WHERE referrer_id=? AND invited_id=? AND kind='first_paid'",
                 (referrer, invited_id))
    if already:
        return
    ex("INSERT OR IGNORE INTO referral_events(referrer_id,invited_id,kind,created_at) "
       "VALUES(?,?,'first_paid',?)", (referrer, invited_id, int(time.time())))
    # рахуємо, скільки запрошених вже оплатили, і нараховуємо бонус за досягнуту сходинку
    paid_count = q1("SELECT COUNT(*) c FROM referral_events WHERE referrer_id=? AND kind='first_paid'",
                    (referrer,))["c"]
    for n, hours in REF_TIERS:
        if paid_count == n:
            add_bonus(referrer, "premium", hours, "referral_reward")
            _notify(referrer, f"🎁 Награда за {n} оплаченных друзей: {hours} ч PREMIUM!")
            break


def _notify(tg_id: int, text: str):
    """Надіслати повідомлення користувачу (веб-акаунти з негативним ID просто ігноруються)."""
    if BOT is None or tg_id <= 0:
        return

    async def _send():
        try:
            await BOT.send_message(tg_id, text)
        except Exception:
            pass  # чат може бути закритий — не ламаємо основний потік

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send())
    except RuntimeError:
        pass


# ---------------- пристрої ----------------

def _next_ip(server_id: int, subnet: str) -> str:
    used = {r["client_ip"] for r in q(
        "SELECT client_ip FROM devices WHERE server_id=? AND status='active'", (server_id,))}
    prefix = subnet.rsplit(".", 1)[0]  # 10.7.0
    for i in range(2, 255):
        ip = f"{prefix}.{i}"
        if ip not in used:
            return ip
    raise wg.WgError("no free IPs on server")


async def add_device(tg_id: int, server_id: int, name: str) -> dict:
    user = get_user(tg_id)
    if user is None:
        raise ValueError("user not found")
    if user["is_banned"]:
        raise PermissionError("user banned")
    plan, _ = effective_plan(user)
    active = q("SELECT * FROM devices WHERE tg_id=? AND status='active'", (tg_id,))
    limit = device_limit(plan)
    if len(active) >= limit:
        raise PermissionError(f"device limit reached ({limit} для {PLANS[plan]['name']})")
    srv = q1("SELECT * FROM servers WHERE id=? AND is_active=1", (server_id,))
    if srv is None:
        raise ValueError("server not found")
    priv, pub = wg.gen_keypair()
    client_ip = _next_ip(server_id, srv["subnet"])
    # створюємо peer на реальному сервері WireGuard
    driver = wg.ServerDriver(srv)
    await driver.add_peer(pub, client_ip)
    # сервер сам обчислює публічний ключ клієнта з приватного — зберігаємо приватний зашифрованим
    ex("INSERT INTO devices(tg_id,server_id,name,wg_public_key,wg_private_key,client_ip,created_at) "
       "VALUES(?,?,?,?,?,?,?)",
       (tg_id, server_id, (name or "Моё устройство")[:40], pub, wg.enc_secret(priv), client_ip,
        int(time.time())))
    d = q1("SELECT * FROM devices WHERE tg_id=? AND status='active' ORDER BY id DESC LIMIT 1", (tg_id,))
    db.log_admin("system", "device_added", f"tg={tg_id} device={d['id']} server={server_id}")
    return dict(d)


async def remove_device(tg_id: int, device_id: int) -> bool:
    d = q1("SELECT * FROM devices WHERE id=? AND tg_id=? AND status='active'", (device_id, tg_id))
    if d is None:
        return False
    srv = q1("SELECT * FROM servers WHERE id=?", (d["server_id"],))
    if srv:
        driver = wg.ServerDriver(srv)
        try:
            await driver.remove_peer(d["wg_public_key"])
        except wg.WgError:
            # peer міг бути знятий раніше — не блокуємо видалення запису
            pass
    ex("UPDATE devices SET status='removed' WHERE id=?", (device_id,))
    db.log_admin("system", "device_removed", f"tg={tg_id} device={device_id}")
    return True


def get_device_config(tg_id: int, device_id: int) -> dict | None:
    d = q1("SELECT * FROM devices WHERE id=? AND tg_id=?", (device_id, tg_id))
    srv = q1("SELECT * FROM servers WHERE id=?", (d["server_id"],)) if d else None
    if not d or not srv:
        return None
    priv = wg.dec_secret(d["wg_private_key"])
    return {
        "device": dict(d),
        "config": wg.build_client_config(priv, d["client_ip"], srv),
    }


def device_qr_b64(config_text: str) -> str:
    import base64
    import io
    import qrcode
    img = qrcode.make(config_text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------- завершення підписок ----------------

async def expire_check():
    """Автоматичне завершення: зняти тариф, вимкнути всі peer-и, повідомити користувача."""
    now = int(time.time())
    expired = q("SELECT * FROM users WHERE plan!='free' AND plan_expires_at IS NOT NULL "
                "AND plan_expires_at<=?", (now,))
    for user in expired:
        tg_id = user["tg_id"]
        ex("UPDATE users SET plan='free', plan_expires_at=NULL WHERE tg_id=?", (tg_id,))
        ex("UPDATE subscriptions SET status='expired' WHERE tg_id=? AND status='active'", (tg_id,))
        removed = 0
        for d in q("SELECT * FROM devices WHERE tg_id=? AND status='active'", (tg_id,)):
            srv = q1("SELECT * FROM servers WHERE id=?", (d["server_id"],))
            if srv:
                try:
                    await (wg.ServerDriver(srv)).remove_peer(d["wg_public_key"])
                    removed += 1
                except wg.WgError:
                    pass
            ex("UPDATE devices SET status='removed' WHERE id=?", (d["id"],))
        db.log_admin("system", "subscription_expired", f"tg={tg_id} devices_removed={removed}")
        _notify(tg_id, "⏳ Ваша подписка завершилась. VPN-доступ отключён. "
                       "Продлите подписку через бота или Mini App — и доступ вернётся.")


async def run_expiry_worker():
    from .config import settings
    while True:
        try:
            await expire_check()
        except Exception as e:
            print(f"[expiry worker] {e}")
        await asyncio.sleep(settings.EXPIRY_CHECK_SECONDS)
