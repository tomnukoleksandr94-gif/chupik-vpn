"""Telegram-бот CHUPIK VPN — розширена версія з оплатою Stars.

Запускається всередині server.api (один процес: бот + API + worker).
"""
import json

from aiogram import Bot, Dispatcher, F
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeDefault, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, LabeledPrice, MenuButtonWebApp, Message,
    ReplyKeyboardMarkup, SuccessfulPayment, WebAppInfo,
)

from . import core, db, payments
from .config import settings
from .db import q, q1
from .i18n import LANG_NAMES, T

BOT_USERNAME = "Chupik_vpnBot"

if settings.BOT_TOKEN:
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
else:  # тестовий режим без токена — API працює, бот ні
    bot = None

dp = Dispatcher()

core.BOT = bot


def L(user) -> str:
    return user["lang"] if user and user["lang"] in T else "ru"


def fmt_date(ts) -> str:
    if not ts:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


# ---------------- клавіатури ----------------
def main_kb(lang: str):
    m = T[lang]["menu"]
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=m[0]), KeyboardButton(text=m[1])],
        [KeyboardButton(text=m[2]), KeyboardButton(text=m[3])],
        [KeyboardButton(text=m[4]), KeyboardButton(text=m[5])],
        [KeyboardButton(text=m[6])],
    ], resize_keyboard=True)


def webapp_kb(lang: str):
    if settings.MINIAPP_URL:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=T[lang]["open_miniapp"],
                                 web_app=WebAppInfo(url=settings.MINIAPP_URL))]])
    return None


def lang_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"lang:{code}")]
        for code, name in LANG_NAMES.items()])


def plans_kb(lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"BASIC · {payments.stars_price('basic')} ⭐", callback_data="buy:basic")],
        [InlineKeyboardButton(text=f"PREMIUM · {payments.stars_price('premium')} ⭐", callback_data="buy:premium")],
        [InlineKeyboardButton(text=f"ULTIMATE · {payments.stars_price('ultimate')} ⭐", callback_data="buy:ultimate")],
    ])


# ---------------- /start ----------------
async def _send_welcome_photo(msg: Message, text: str, reply_markup):
    """Фото /start із кешем file_id: Telegram не завантажує його з GitHub щоразу.

    Перше відправлення за URL повільне (Telegram тягне файл із GitHub Pages) —
    тому зберігаємо file_id у БД і далі відправляємо миттєво.
    """
    if not settings.BRAND_PHOTO_URL:
        await msg.answer(text, reply_markup=reply_markup)
        return
    file_id = db.get_setting("brand_photo_file_id")
    m = None
    if file_id:
        try:
            m = await msg.answer_photo(file_id, caption=text, reply_markup=reply_markup)
        except Exception:
            m = None
    if m is None:  # перший запуск або файл протермінався
        try:
            m = await msg.answer_photo(settings.BRAND_PHOTO_URL, caption=text, reply_markup=reply_markup)
            if m and m.photo:
                db.set_setting("brand_photo_file_id", m.photo[-1].file_id)
        except Exception:
            await msg.answer(text, reply_markup=reply_markup)


@dp.message(CommandStart())
async def cmd_start(msg: Message, command=None):
    payload = command.args if command else ""
    referrer = None
    if payload and payload.startswith("ref_"):
        try:
            referrer = int(payload[4:])
        except ValueError:
            pass
    user = core.upsert_user(msg.from_user.id, msg.from_user.username,
                           msg.from_user.first_name, referrer_id=referrer)
    lang = L(user)
    if referrer and core.register_referral(referrer, msg.from_user.id):
        await msg.answer(T[lang]["new_ref"])
    # welcome + trial-підказка в ОДНОМУ повідомленні (менше повідомлень = швидше)
    text = T[lang]["welcome"].format(name=msg.from_user.first_name)
    if user["plan"] in (None, "free") and not q1(
            "SELECT 1 FROM referral_events WHERE invited_id=? AND kind='trial'", (msg.from_user.id,)):
        text += "\n\n" + T[lang]["trial_hint"]
    await _send_welcome_photo(msg, text, webapp_kb(lang))
    await msg.answer("Меню ниже 👇", reply_markup=main_kb(lang))


# ---------------- покупки (Stars) ----------------
async def send_buy_menu(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    lang = L(user)
    await msg.answer(T[lang]["buy"])
    await msg.answer(T[lang]["buy_pick"], reply_markup=plans_kb(lang))


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    if bot is None:
        await c.answer("Бот не налаштований", show_alert=True)
        return
    plan = c.data.split(":", 1)[1]
    user = core.get_user(c.from_user.id)
    lang = L(user)
    try:
        link = await payments.create_stars_invoice_link(bot, c.from_user.id, plan)
    except Exception as e:
        await c.answer(str(e)[:200], show_alert=True)
        return
    await c.message.answer(
        f"🧾 {core.PLANS[plan]['name']} · 1 месяц · {payments.stars_price(plan)} ⭐",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⭐ Оплатить Stars", url=link)]]))
    await c.answer()


@dp.pre_checkout_query()
async def pre_checkout(query):
    # обов'язкова відповідь — без неї Telegram скасовує оплату
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_paid(msg: Message):
    sp: SuccessfulPayment = msg.successful_payment
    payload = {}
    try:
        payload = json.loads(sp.invoice_payload or "{}")
    except ValueError:
        pass
    tg_id = payload.get("tg_id", msg.from_user.id)
    plan = payload.get("plan", "")
    user = core.get_user(tg_id) or core.upsert_user(tg_id)
    lang = L(user)
    if plan not in core.PLANS or plan == "free":
        await msg.answer("⚠️ Неизвестный тариф в платеже — обратитесь в /paysupport.")
        return
    result = payments.activate_after_payment(
        "stars", sp.telegram_payment_charge_id, tg_id, plan,
        sp.total_amount, sp.currency, raw=json.dumps({"payload": sp.invoice_payload}))
    if result.get("ok"):
        sub = result.get("subscription", {})
        await msg.answer(T[lang]["payment_ok"].format(
            plan=core.PLANS[plan]["name"], expires=fmt_date(sub.get("expires_at"))))


# ---------------- trial ----------------
@dp.message(Command("trial"))
async def cmd_trial(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    lang = L(user)
    cur = db.ex("INSERT OR IGNORE INTO referral_events(referrer_id,invited_id,kind,created_at) "
                "VALUES(0,?,'trial',?)", (msg.from_user.id, __import__("time").time()))
    if cur.rowcount == 0:
        await msg.answer(T[lang]["trial_used"])
        return
    core.add_bonus(msg.from_user.id, "premium", 3, "vip_trial")
    await msg.answer(T[lang]["trial_ok"])


# ---------------- меню / команди ----------------
def _profile_text(msg):
    user = core.get_user(msg.from_user.id)
    plan, exp = core.effective_plan(user)
    devices = q("SELECT COUNT(*) c FROM devices WHERE tg_id=? AND status='active'",
                (msg.from_user.id,))[0]["c"]
    return T[L(user)]["profile"].format(
        id=msg.from_user.id, plan=core.PLANS[plan]["name"],
        expires=fmt_date(exp), devices=f"{devices}/{core.device_limit(plan)}")


async def _servers_text(msg):
    user = core.get_user(msg.from_user.id)
    lang = L(user)
    rows = q("SELECT * FROM servers WHERE is_active=1")
    if not rows:
        return T[lang]["no_servers"]
    items = [T[lang]["srv_row"].format(flag=r["flag"], name=r["name"],
                                       status=T[lang]["status_soon"])
             for r in rows]
    return T[lang]["servers"].format(list="\n".join(items))


for key, fn in (
    ("profile", lambda m: _profile_text(m)),
):
    pass


@dp.message(Command("profile"))
async def cmd_profile(msg: Message):
    core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(_profile_text(msg), reply_markup=webapp_kb(L(core.get_user(msg.from_user.id))))


@dp.message(Command("buy"))
async def cmd_buy(msg: Message):
    await send_buy_menu(msg)


@dp.message(Command("servers"))
async def cmd_servers(msg: Message):
    core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(await _servers_text(msg))


@dp.message(Command("group"))
async def cmd_group(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(T[L(user)]["group"])


@dp.message(Command("referral"))
async def cmd_referral(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    lang = L(user)
    await msg.answer(T[lang]["referral"].format(
        bot=BOT_USERNAME, id=msg.from_user.id, n=core.invited_count(msg.from_user.id)))


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(T[L(user)]["help"])


@dp.message(Command("support"))
async def cmd_support(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(T[L(user)]["support"])


@dp.message(Command("terms"))
async def cmd_terms(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(T[L(user)]["terms"])


@dp.message(Command("paysupport"))
async def cmd_paysupport(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(T[L(user)]["paysupport"])


@dp.message(Command("language"))
async def cmd_language(msg: Message):
    core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(T["ru"]["choose_lang"], reply_markup=lang_kb())


@dp.callback_query(F.data.startswith("lang:"))
async def cb_lang(c: CallbackQuery):
    code = c.data.split(":", 1)[1]
    if code in T:
        db.ex("UPDATE users SET lang=? WHERE tg_id=?", (code, c.from_user.id))
        await c.answer(T[code]["lang_saved"])
        await c.message.answer(T[code]["lang_saved"], reply_markup=main_kb(code))
    else:
        await c.answer()


# ---------------- адмін-команди ----------------
def _is_admin(tg_id: int) -> bool:
    u = core.get_user(tg_id)
    return bool(u and u["is_admin"])


@dp.message(Command("refund"))
async def cmd_refund(msg: Message):
    user = core.get_user(msg.from_user.id)
    if not (user and user["is_admin"]):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.answer("Використання: /refund [tg_id] [charge_id]")
        return
    ok = await payments.refund_stars(bot, int(parts[1]), parts[2])
    await msg.answer("✅ Повернено" if ok else "❌ Не вдалося — деталі в адмін-панелі")


@dp.message(Command("grant"))
async def cmd_grant(msg: Message):
    """Ручна зміна тарифу адміном: /grant <tg_id> <plan> [days]"""
    user = core.get_user(msg.from_user.id)
    if not (user and user["is_admin"]):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.answer("Використання: /grant [tg_id] [basic|premium|ultimate] [days]")
        return
    tg_id, plan = int(parts[1]), parts[2].lower()
    days = int(parts[3]) if len(parts) > 3 else 30
    if plan not in core.PLANS or plan == "free":
        await msg.answer("Невідомий тариф")
        return
    if not core.get_user(tg_id):
        await msg.answer("Користувача не знайдено")
        return
    sub = core.activate_subscription(tg_id, plan, "admin", days=days)
    await msg.answer(f"✅ {core.PLANS[plan]['name']} до {fmt_date(sub['expires_at'])}")


def _panel_url() -> str:
    """Актуальна адреса адмін-панелі: спершу файл keeper-а (тунель ротує URL),
    потім BASE_URL."""
    if settings.PANEL_URL_FILE:
        try:
            with open(settings.PANEL_URL_FILE) as f:
                base = f.read().strip()
            if base.startswith("https"):
                return f"{base}/admin?token={settings.ADMIN_TOKEN}"
        except Exception:
            pass
    if settings.BASE_URL:
        return f"{settings.BASE_URL}/admin?token={settings.ADMIN_TOKEN}"
    return "(адреса панелі не налаштована)"


@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    user = core.get_user(msg.from_user.id)
    if not (user and user["is_admin"]):
        await msg.answer("⛔ Ця команда тільки для адміністраторів.")
        return
    core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    panel = _panel_url()
    await msg.answer(
        "👑 <b>Панель адміністратора</b>\n\n"
        f"🔗 Адмін-панель: {panel}\n\n"
        "<b>Команди:</b>\n"
        "/grant [tg_id] [basic|premium|ultimate] [днів] — видати підписку\n"
        "/refund [tg_id] [charge_id] — повернути Stars\n"
        "/users — кількість користувачів")


@dp.message(Command("users"))
async def cmd_users(msg: Message):
    user = core.get_user(msg.from_user.id)
    if not (user and user["is_admin"]):
        return
    n = core.q1("SELECT COUNT(*) AS c FROM users")["c"]
    paid = core.q1("SELECT COUNT(DISTINCT tg_id) AS c FROM payments WHERE status='paid'")["c"]
    await msg.answer(f"👥 Користувачів: <b>{n}</b>\n💳 Хоча б одна оплата: <b>{paid}</b>")


# ---------------- кнопки меню ----------------
async def miniapp_btn_handler(msg: Message):
    user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    lang = L(user)
    kb = webapp_kb(lang)
    await msg.answer(T[lang]["welcome"].format(name=msg.from_user.first_name), reply_markup=kb)


for lang_code in T:
    m = T[lang_code]["menu"]
    for i, cmd in enumerate(("buy", "profile", "servers", "group", "referral", "support")):
        async def _h(msg, _cmd=cmd):
            if _cmd == "profile":
                core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
                await msg.answer(_profile_text(msg))
            elif _cmd == "buy":
                await send_buy_menu(msg)
            elif _cmd == "servers":
                core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
                await msg.answer(await _servers_text(msg))
            elif _cmd == "group":
                user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
                await msg.answer(T[L(user)]["group"])
            elif _cmd == "referral":
                user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
                await msg.answer(T[L(user)]["referral"].format(
                    bot=BOT_USERNAME, id=msg.from_user.id, n=core.invited_count(msg.from_user.id)))
            else:
                user = core.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
                await msg.answer(T[L(user)]["support"])
        dp.message.register(_h, F.text == m[i])
    dp.message.register(miniapp_btn_handler, F.text == m[6])


# ---------------- захист від збоїв ----------------
@dp.errors()
async def on_error(event, exception):
    """Помилка в обробнику не «зависає» запит — логується і бот живе далі."""
    import traceback
    print(f"[bot] помилка обробника: {exception}\n{traceback.format_exc()[-400:]}")
    try:
        if event and getattr(event, "message", None):
            await event.message.answer("⚠️ Виникла помилка. Спробуйте ще раз.")
    except Exception:
        pass
    return True


# ---------------- setup ----------------
async def setup_bot():
    cmds = [BotCommand(command=c, description=d) for c, d in [
        ("start", "Главное меню"), ("profile", "Мой кабинет"), ("buy", "Купить VPN"),
        ("servers", "Серверы"), ("group", "Моя группа"), ("referral", "Пригласить друга"),
        ("language", "Язык"), ("help", "Помощь"), ("support", "Поддержка"),
        ("terms", "Условия"), ("paysupport", "Вопросы оплаты"), ("trial", "Пробный PREMIUM")]]
    await bot.set_my_commands(cmds, BotCommandScopeDefault())
    if settings.MINIAPP_URL:
        url = settings.MINIAPP_URL
        if settings.BASE_URL:  # LIVE-режим: мініапка одразу з живим API
            url += ("&" if "?" in url else "?") + f"api={settings.BASE_URL}"
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="ЧУПИК VPN", web_app=WebAppInfo(url=url)))
    me = await bot.get_me()
    print(f"[bot] запущен: @{me.username}")
    return dp
