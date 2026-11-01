"""Дві системи оплати → одна система підписок.

А. Telegram Stars (XTR):
   - create_stars_invoice_link() для Mini App (tg.openInvoice)
   - send_invoice у чат бота
   - активація ТІЛЬКИ після successful_payment (офіційне підтвердження Telegram)
   - повернення коштів: refundStarPayment

Б. Банківська картка (Stripe) — тільки на зовнішньому сайті (правила Telegram:
   цифрові товари всередині Telegram продаються виключно за Stars):
   - Checkout Session на серверній стороні
   - webhook з перевіркою підпису
   - захист від повторної обробки: payments.provider_ref UNIQUE + таблиця оброблених подій
"""
import json
import time

from . import db, core
from .config import settings
from .db import ex, q1

# ---------------- спільне ----------------

def record_payment(provider: str, provider_ref: str, tg_id: int, plan: str,
                   amount: float, currency: str, status: str, raw: str = "") -> bool:
    """INSERT OR IGNORE по provider_ref — одна транзакція обробляється один раз."""
    cur = ex("INSERT OR IGNORE INTO payments(provider,provider_ref,tg_id,plan,amount,currency,"
             "status,created_at,raw) VALUES(?,?,?,?,?,?,?,?,?)",
             (provider, provider_ref, tg_id, plan, amount, currency, status, int(time.time()), raw))
    return cur.rowcount > 0


def mark_payment(provider_ref: str, status: str):
    ex("UPDATE payments SET status=?, completed_at=? WHERE provider_ref=?",
       (status, int(time.time()), provider_ref))


def activate_after_payment(provider: str, provider_ref: str, tg_id: int, plan: str,
                           amount: float, currency: str, raw: str = "") -> dict:
    """Єдиний шлях: платіж підтверджено → активувати підписку + нарахувати рефералку."""
    inserted = record_payment(provider, provider_ref, tg_id, plan, amount, currency,
                             "succeeded", raw)
    if not inserted:
        row = q1("SELECT status FROM payments WHERE provider_ref=?", (provider_ref,))
        if row and row["status"] == "succeeded":
            return {"ok": True, "duplicate": True}
        return {"ok": False, "error": "payment record conflict"}
    result = core.activate_subscription(tg_id, plan, provider, days=30)
    core.on_invitee_paid(tg_id)  # реферальна нагорода тому, хто запросив
    return {"ok": True, "subscription": result, "duplicate": False}


# ---------------- Telegram Stars ----------------

STARS_TITLES = {
    "basic": ("ЧУПИК VPN · BASIC", "2 устройства · 1 месяц"),
    "premium": ("ЧУПИК VPN · PREMIUM", "4 устройства · 1 месяц"),
    "ultimate": ("ЧУПИК VPN · ULTIMATE", "5 устройств · 1 месяц"),
}


def stars_price(plan: str) -> int:
    key = core.PLANS[plan]["stars_key"]
    return int(db.get_setting(key, "0"))


async def create_stars_invoice_link(bot, tg_id: int, plan: str) -> str:
    title, desc = STARS_TITLES[plan]
    amount = stars_price(plan)
    if amount <= 0:
        raise ValueError("зв'яжіться с админом: цена в Stars не настроена")
    link = await bot.create_invoice_link(
        title=title,
        description=desc,
        payload=json.dumps({"tg_id": tg_id, "plan": plan}),
        currency="XTR",
        prices=[{"label": desc, "amount": amount}],
    )
    return link


async def refund_stars(bot, user_id: int, charge_id: str) -> bool:
    """Повернення Stars (Telegram підтримує повернення внутрішніх платежів)."""
    try:
        await bot.refund_star_payment(user_id, charge_id)
        mark_payment(charge_id, "refunded")
        row = q1("SELECT tg_id, plan FROM payments WHERE provider_ref=?", (charge_id,))
        if row:
            ex("UPDATE subscriptions SET status='refunded' WHERE tg_id=? AND status='active'",
               (row["tg_id"],))
            db.log_admin("admin", "stars_refunded", f"charge={charge_id} tg={row['tg_id']}")
        return True
    except Exception as e:
        db.log_admin("admin", "stars_refund_failed", f"charge={charge_id} err={e}")
        return False


# ---------------- Stripe (картка, зовнішній сайт) ----------------

def stripe_configured() -> bool:
    return bool(settings.STRIPE_SECRET_KEY and settings.STRIPE_WEBHOOK_SECRET and settings.BASE_URL)


def create_card_checkout(tg_id: int, plan: str) -> str:
    """Створює Stripe Checkout Session. Повертає URL для переадресації."""
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    plan_info = core.PLANS[plan]
    if plan == "free":
        raise ValueError("FREE не продається")
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "eur",
                "unit_amount": int(plan_info["price_eur"] * 100),
                "product_data": {"name": f"ЧУПИК VPN · {plan_info['name']} · 1 месяц"},
            },
            "quantity": 1,
        }],
        metadata={"tg_id": str(tg_id), "plan": plan},
        success_url=f"{settings.BASE_URL}/pay/success",
        cancel_url=f"{settings.BASE_URL}/pay/cancel",
    )
    record_payment("card", session.id, tg_id, plan,
                   plan_info["price_eur"], "EUR", "pending", json.dumps({"session": session.id}))
    return session.url


def verify_and_process_webhook(payload: bytes, sig_header: str) -> dict:
    """Перевірка підпису Stripe → активація. Повторна обробка ігнорується."""
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET).to_dict()
    except Exception:
        return {"ok": False, "error": "invalid signature"}

    etype = event["type"]
    data = event["data"]["object"]
    cur = ex("INSERT OR IGNORE INTO admin_log(ts,actor,action,details) VALUES(?,?,?,?)",
             (int(time.time()), "stripe_webhook", etype, event["id"]))
    if cur.rowcount == 0:
        return {"ok": True, "duplicate": True}  # подія вже оброблялась

    if etype == "checkout.session.completed" and data.get("payment_status") == "paid":
        meta = data.get("metadata", {})
        tg_id, plan = int(meta.get("tg_id", 0)), meta.get("plan")
        if tg_id and plan in core.PLANS and plan != "free":
            return activate_after_payment(
                "card", data["id"], tg_id, plan,
                data.get("amount_total", 0) / 100, data.get("currency", "eur"),
                json.dumps({"event": event["id"]}))
    elif etype == "checkout.session.expired":
        mark_payment(data["id"], "failed")
    elif etype == "charge.refunded":
        # повернення коштів: позначаємо і відкликаємо підписку
        pid = data.get("payment_intent")
        row = q1("SELECT tg_id FROM payments WHERE provider_ref LIKE ?", (f"%{pid}%",))
        if row:
            ex("UPDATE payments SET status='refunded' WHERE provider_ref LIKE ?", (f"%{pid}%",))
            ex("UPDATE subscriptions SET status='refunded' WHERE tg_id=? AND status='active'",
               (row["tg_id"],))
            db.log_admin("stripe", "card_refunded", f"tg={row['tg_id']}")
    return {"ok": True}
