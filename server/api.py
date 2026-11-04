"""CHUPIK VPN Backend API — один процес: FastAPI + aiogram-бот + worker завершення підписок.

- /api/*     → Mini App (автентифікація через підпис Telegram initData)
- /pay/*     → оплата карткою на сайті (Stripe Checkout)
- /webhook/* → підтвердження платежів від провайдера
- /admin/*   → адмін-панель (X-Admin-Token)
"""
import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import bot_app, core, db, groups, payments, wg, webauth
from .config import settings

app = FastAPI(title="CHUPIK VPN API", docs_url=None, redoc_url=None)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

ADMIN_DIR = Path(__file__).parent / "admin"


# ================================================================
# Автентифікація: Mini App (підпис Telegram) АБО сайт (Bearer-токен)
# ================================================================
def validate_init_data(init_data: str) -> dict | None:
    if not init_data or not settings.BOT_TOKEN:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", "")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if auth_date < time.time() - 86400:  # підпис старший за добу
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


async def tg_user(request: Request) -> dict:
    """Два способи входу:
    1. Mini App — підпис Telegram initData (заголовок X-Init-Data);
    2. Сайт — сесія веб-кабінету (Authorization: Bearer <token>).
    Обидва дають доступ до одних і тих самих API-ендпоінтів.
    """
    # --- спершу веб-сесія (сайт) ---
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        session = webauth.resolve_session(auth[7:].strip())
        if not session:
            raise HTTPException(401, "invalid or expired session")
        row = core.get_user(session["tg_id"])
        if row and row["is_banned"]:
            raise HTTPException(403, "banned")
        return {"id": session["tg_id"], "web_email": session["email"]}
    # --- потім підпис Telegram (Mini App) ---
    init = request.headers.get("X-Init-Data", "") or request.query_params.get("init_data", "")
    user = validate_init_data(init)
    if not user or not user.get("id"):
        raise HTTPException(401, "invalid telegram signature")
    row = core.get_user(user["id"])
    if row and row["is_banned"]:
        raise HTTPException(403, "banned")
    return user


# ================================================================
# Веб-акаунти сайту: реєстрація / вхід / вихід
# ================================================================
class RegisterIn(BaseModel):
    email: str
    password: str
    ref: int | None = None


class LoginIn(BaseModel):
    email: str
    password: str


class EmailIn(BaseModel):
    email: str


@app.post("/api/web/auth/register")
async def web_register(body: RegisterIn, request: Request):
    try:
        result = webauth.register(body.email, body.password, body.ref)
    except webauth.AuthError as e:
        raise HTTPException(400, str(e))
    webauth.log_login(result["email"], result["tg_id"],
                      request.headers.get("x-forwarded-for", ""),
                      request.headers.get("user-agent", ""))
    return {"ok": True, **result, "user": _user_payload(result["tg_id"])}


@app.post("/api/web/auth/login")
async def web_login(body: LoginIn, request: Request):
    try:
        result = webauth.login(body.email, body.password)
    except webauth.AuthError as e:
        raise HTTPException(400, str(e))
    webauth.log_login(body.email, result["tg_id"],
                      request.headers.get("x-forwarded-for", ""),
                      request.headers.get("user-agent", ""))
    return {"ok": True, **result, "user": _user_payload(result["tg_id"])}


@app.post("/api/web/auth/logout")
async def web_logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        webauth.logout(auth[7:].strip())
    return {"ok": True}


@app.get("/api/web/config")
async def web_config():
    """Сайт питає: чи доступні реєстрація та оплата картою."""
    return {
        "ok": True,
        "web_auth": True,
        "card_provider": payments.card_provider(),
    }


# ================================================================
# Mini App API
# ================================================================
def _user_payload(tg_id: int) -> dict:
    user = core.get_user(tg_id)
    plan, exp = core.effective_plan(user)
    devices = [dict(d) for d in db.q(
        "SELECT * FROM devices WHERE tg_id=? AND status='active' ORDER BY id", (tg_id,))]
    for d in devices:
        d.pop("wg_private_key", None)  # приватний ключ ніколи не віддаємо списком
    return {
        "tg_id": tg_id,
        "username": user["username"], "first_name": user["first_name"],
        "lang": user["lang"],
        "plan": plan, "plan_name": core.PLANS[plan]["name"],
        "plan_expires_at": exp,
        "bonus_until": user["bonus_until"] if (user["bonus_until"] or 0) > time.time() else None,
        "device_limit": core.device_limit(plan),
        "devices": devices,
        "referral": {
            # для Telegram-користувачів — посилання на бота, для веб-акаунтів — на сайт
            "link": (f"https://t.me/{bot_app.BOT_USERNAME}?start=ref_{tg_id}" if tg_id > 0
                     else f"{settings.WEB_SITE_URL}/cabinet.html?ref={tg_id}"),
            "invited": core.invited_count(tg_id),
            "tiers": core.REF_TIERS,
        },
        "payments": [dict(p) for p in db.q(
            "SELECT provider,plan,amount,currency,status,created_at FROM payments "
            "WHERE tg_id=? ORDER BY id DESC LIMIT 20", (tg_id,))],
    }


@app.get("/api/health")
async def api_health():
    import time as _time
    return {"ok": True, "ts": int(_time.time())}


@app.get("/api/me")
async def api_me(user=Depends(tg_user)):
    return {"ok": True, **_user_payload(user["id"])}


@app.get("/api/servers")
async def api_servers():
    """Реальні статси серверів (або статус недоступності — без вигаданих цифр)."""
    rows = db.q("SELECT * FROM servers WHERE is_active=1")
    out = []
    for s in rows:
        entry = {k: s[k] for k in ("id", "code", "name", "country", "flag", "lon", "lat",
                                   "endpoint_host", "endpoint_port")}
        entry["online"] = False
        entry["peers"] = 0
        entry["load1"] = None
        entry["error"] = None
        try:
            st = await asyncio.wait_for(wg.ServerDriver(s).stats(), timeout=12)
            entry["online"] = True
            entry["peers"] = len(st["peers"])
            entry["load1"] = st["load1"]
        except Exception:
            entry["error"] = "unreachable"
        out.append(entry)
    return {"ok": True, "servers": out}


class DeviceIn(BaseModel):
    name: str = "Моё устройство"
    server_id: int
    type: str = "other"


@app.post("/api/devices")
async def api_add_device(body: DeviceIn, user=Depends(tg_user)):
    try:
        d = await core.add_device(user["id"], body.server_id, body.name, body.type)
        d.pop("wg_private_key", None)
        return {"ok": True, "device": d}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except wg.WgError as e:
        raise HTTPException(502, f"server error: {e}")


@app.get("/api/devices/{device_id}/config")
async def api_device_config(device_id: int, user=Depends(tg_user)):
    data = core.get_device_config(user["id"], device_id)
    if not data:
        raise HTTPException(404, "device not found")
    return {"ok": True, "config": data["config"],
            "qr_png_base64": core.device_qr_b64(data["config"])}


@app.delete("/api/devices/{device_id}")
async def api_remove_device(device_id: int, user=Depends(tg_user)):
    ok = await core.remove_device(user["id"], device_id)
    if not ok:
        raise HTTPException(404, "device not found")
    return {"ok": True}


# ================================================================
# Групи (збалансовані: слоти за тарифом власника)
# ================================================================
class GroupIn(BaseModel):
    name: str = ""


class JoinIn(BaseModel):
    code: str


@app.get("/api/group")
async def api_group_info(user=Depends(tg_user)):
    return {"ok": True, **groups.group_info(user["id"])}


@app.post("/api/group")
async def api_group_create(body: GroupIn, user=Depends(tg_user)):
    try:
        return {"ok": True, **groups.create_group(user["id"], body.name)}
    except groups.GroupError as e:
        raise HTTPException(400, str(e))


@app.post("/api/group/invite")
async def api_group_invite(user=Depends(tg_user)):
    try:
        code = groups.create_invite(user["id"])
    except groups.GroupError as e:
        raise HTTPException(400, str(e))
    invite_link = f"https://t.me/{bot_app.BOT_USERNAME}?start=grp_{code}"
    web_link = f"{settings.WEB_SITE_URL}/cabinet.html?grp={code}"
    return {"ok": True, "code": code, "tg_link": invite_link, "web_link": web_link}


@app.post("/api/group/join")
async def api_group_join(body: JoinIn, user=Depends(tg_user)):
    try:
        return {"ok": True, **groups.join_group(body.code, user["id"])}
    except groups.GroupError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/group/member/{member_tg_id}")
async def api_group_remove_member(member_tg_id: int, user=Depends(tg_user)):
    ok = await groups.remove_member(user["id"], member_tg_id)
    if not ok:
        raise HTTPException(400, "member not found")
    return {"ok": True}


@app.post("/api/group/leave")
async def api_group_leave(user=Depends(tg_user)):
    ok = await groups.leave_group(user["id"])
    if not ok:
        raise HTTPException(400, "not a member")
    return {"ok": True}


# ================================================================
# Підтримка: тікети
# ================================================================
class TicketIn(BaseModel):
    subject: str
    message: str


@app.post("/api/tickets")
async def api_create_ticket(body: TicketIn, user=Depends(tg_user)):
    if not (body.subject or "").strip() or not (body.message or "").strip():
        raise HTTPException(400, "subject and message required")
    cur = db.ex("INSERT INTO tickets(tg_id,subject,status,created_at) VALUES(?,?,'open',?)",
                (user["id"], body.subject.strip()[:120], int(time.time())))
    ticket_id = cur.lastrowid
    db.ex("INSERT INTO ticket_messages(ticket_id,sender,message,created_at) "
          "VALUES(?,'user',?,?)",
          (ticket_id, body.message.strip()[:4000], int(time.time())))
    db.log_admin("system", "ticket_created", f"ticket={ticket_id} tg={user['id']}")
    return {"ok": True, "ticket_id": ticket_id}


@app.get("/api/tickets")
async def api_my_tickets(user=Depends(tg_user)):
    rows = db.q("SELECT * FROM tickets WHERE tg_id=? ORDER BY id DESC LIMIT 20", (user["id"],))
    out = []
    for t in rows:
        msgs = [dict(m) for m in db.q(
            "SELECT sender,message,created_at FROM ticket_messages WHERE ticket_id=? "
            "ORDER BY id", (t["id"],))]
        out.append({"id": t["id"], "subject": t["subject"], "status": t["status"],
                    "created_at": t["created_at"], "messages": msgs})
    return {"ok": True, "tickets": out}


# ================================================================
# Профіль: мова, сповіщення, пароль, Telegram-прив'язка, видалення
# ================================================================
class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


class ResetIn(BaseModel):
    token: str
    new_password: str


class LangIn(BaseModel):
    lang: str


class NotifyIn(BaseModel):
    prefs: dict


@app.post("/api/web/auth/change-password")
async def api_change_password(body: PasswordChangeIn, user=Depends(tg_user)):
    if not user.get("web_email"):
        raise HTTPException(400, "web account only")
    try:
        webauth.change_password(user["web_email"], body.old_password, body.new_password)
    except webauth.AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "message": "password changed, sessions reset"}


@app.post("/api/web/auth/forgot")
async def api_forgot_password(body: EmailIn):
    """Запит на скидання: якщо акаунт прив'язаний до Telegram — бот надішле посилання в чат."""
    result = webauth.request_reset(body.email)
    delivered = result.get("delivered")
    if delivered == "telegram":
        # надсилаємо ботом у прив'язаний чат
        link = f"{settings.WEB_SITE_URL}/cabinet.html#reset={result['token']}"
        try:
            await bot_app.bot.send_message(
                result["tg_link"],
                "🔑 Скидання пароля CHUPIK VPN\n\n" + link + "\n\nЛінк діє 1 годину.")
        except Exception:
            pass
        return {"ok": True, "delivered": "telegram"}
    return {"ok": True, "delivered": "support"}


@app.post("/api/web/auth/reset")
async def api_reset_password(body: ResetIn):
    try:
        ok = webauth.confirm_reset(body.token, body.new_password)
    except webauth.AuthError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(400, "invalid or expired token")
    return {"ok": True}


@app.get("/api/profile")
async def api_profile(user=Depends(tg_user)):
    info = _user_payload(user["id"])
    info["group"] = groups.group_info(user["id"])
    info["tg_linked"] = webauth.get_tg_link(user["id"]) is not None
    info["notify_prefs"] = core.get_notify_prefs(user["id"])
    if user.get("web_email"):
        info["login_history"] = webauth.login_history(user["web_email"])
        info["link_code"] = None
    return info


@app.post("/api/lang")
async def api_set_lang(body: LangIn, user=Depends(tg_user)):
    lang = body.lang if body.lang in ("uk", "ru", "fr", "en", "es", "de", "it", "zh") else "uk"
    core.set_lang(user["id"], lang)
    return {"ok": True, "lang": lang}


@app.post("/api/notify-prefs")
async def api_set_notify(body: NotifyIn, user=Depends(tg_user)):
    core.set_notify_prefs(user["id"], body.prefs or {})
    return {"ok": True}


@app.post("/api/web/auth/link")
async def api_make_link_code(user=Depends(tg_user)):
    """Код для прив'язки Telegram: надіслати боту /link CODE"""
    if not user.get("web_email"):
        raise HTTPException(400, "web account only")
    code = webauth.make_link_code(user["web_email"])
    return {"ok": True, "code": code, "bot": bot_app.BOT_USERNAME}


@app.delete("/api/web/auth/delete")
async def api_delete_account(user=Depends(tg_user)):
    if not user.get("web_email"):
        raise HTTPException(400, "web account only")
    webauth.delete_account(user["web_email"])
    return {"ok": True}


class StarsIn(BaseModel):
    plan: str


@app.post("/api/stars/invoice")
async def api_stars_invoice(body: StarsIn, user=Depends(tg_user)):
    """Для Mini App: tg.openInvoice(link). Оплата карткою всередині Telegram заборонена правилами."""
    if body.plan not in core.PLANS or body.plan == "free":
        raise HTTPException(400, "unknown plan")
    if bot_app.bot is None:
        raise HTTPException(503, "bot not configured")
    try:
        link = await payments.create_stars_invoice_link(bot_app.bot, user["id"], body.plan)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"payment provider unavailable: {type(e).__name__}")
    return {"ok": True, "link": link}


class CardIn(BaseModel):
    plan: str
    tg_id: int | None = None


@app.post("/api/card/checkout")
async def api_card_checkout(body: CardIn, user=Depends(tg_user)):
    if not payments.card_provider():
        raise HTTPException(503, "card payments not configured")
    if body.plan not in core.PLANS or body.plan == "free":
        raise HTTPException(400, "unknown plan")
    tg_id = body.tg_id or user["id"]
    try:
        url = payments.create_card_checkout_url(tg_id, body.plan)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "url": url, "provider": payments.card_provider()}


# ================================================================
# Сайт: оплата карткою (LiqPay / Stripe) — зовнішня сторінка
# ================================================================
@app.get("/pay", response_class=HTMLResponse)
async def pay_page(plan: str = "premium"):
    if not payments.card_provider():
        return HTMLResponse(_pay_disabled_page(), status_code=503)
    if plan not in core.PLANS or plan == "free":
        plan = "premium"
    return HTMLResponse(_pay_page_html(plan))


class PayForm(BaseModel):
    plan: str
    tg_id: int


@app.post("/pay/checkout")
async def pay_checkout(body: PayForm):
    if not payments.card_provider():
        raise HTTPException(503, "card payments not configured")
    if body.plan not in core.PLANS or body.plan == "free":
        raise HTTPException(400, "unknown plan")
    if not core.get_user(body.tg_id):
        raise HTTPException(404, "ID не найден — сначала зарегистрируйтесь на сайте или в боте (/start)")
    try:
        url = payments.create_card_checkout_url(body.tg_id, body.plan)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "url": url}


@app.get("/pay/success", response_class=HTMLResponse)
async def pay_success():
    return HTMLResponse("<h2 style='font-family:sans-serif;color:#0a0;color:#fff;background:#05030d;"
                       "padding:40px;text-align:center'>✅ Оплата получена. Подписка активируется "
                       "автоматически после подтверждения от платёжной системы (обычно до минуты)."
                       "</h2>")


@app.get("/pay/cancel", response_class=HTMLResponse)
async def pay_cancel():
    return HTMLResponse("<h2 style='font-family:sans-serif;color:#fff;background:#05030d;"
                        "padding:40px;text-align:center'>Оплата отменена.</h2>")


# ================================================================
# Webhook-и платіжних провайдерів
# ================================================================
@app.post("/webhook/liqpay")
async def liqpay_webhook(request: Request):
    """LiqPay надсилає form-urlencoded: data=<base64> & signature=<sig>."""
    body = (await request.body()).decode("utf-8", "replace")
    form = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
    result = payments.verify_and_process_liqpay(form.get("data", ""), form.get("signature", ""))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    result = payments.verify_and_process_webhook(payload, sig)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


# ================================================================
# Адмін-панель
# ================================================================
def require_admin(request: Request):
    token = request.headers.get("X-Admin-Token", "") or request.query_params.get("token", "")
    if not token or not hmac.compare_digest(token, settings.ADMIN_TOKEN):
        raise HTTPException(401, "invalid admin token")


@app.get("/admin", response_class=HTMLResponse)
async def admin_index():
    return FileResponse(ADMIN_DIR / "index.html")


@app.get("/admin/api/dashboard")
async def admin_dashboard(_=Depends(require_admin)):
    now = int(time.time())
    day, week, month = now - 86400, now - 7 * 86400, now - 30 * 86400
    paid = "SELECT COUNT(*) c FROM payments WHERE status='succeeded' AND created_at>=?"
    rev = "SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='succeeded' AND created_at>=?"
    return {
        "ok": True,
        "users": db.q1("SELECT COUNT(*) c FROM users")["c"],
        "new_users_day": db.q1("SELECT COUNT(*) c FROM users WHERE created_at>=?", (day,))["c"],
        "active_subs": db.q1("SELECT COUNT(*) c FROM users WHERE plan!='free' AND plan_expires_at>?",
                             (now,))["c"],
        "plans": {p: db.q1("SELECT COUNT(*) c FROM users WHERE plan=? AND plan_expires_at>?",
                           (p, now))["c"] for p in ("basic", "premium", "ultimate")},
        "purchases": db.q1(paid, (0,))["c"],
        "revenue_total": db.q1("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='succeeded'")["s"],
        "revenue_day": db.q1(rev, (day,))["s"],
        "revenue_week": db.q1(rev, (week,))["s"],
        "revenue_month": db.q1(rev, (month,))["s"],
        "servers": db.q("SELECT id,name,country,flag,is_active,driver,endpoint_host FROM servers"),
    }


@app.get("/admin/api/users")
async def admin_users(q_: str = "", _=Depends(require_admin)):
    where, args = "", ()
    if q_.strip().isdigit():
        where, args = "WHERE tg_id=?", (int(q_.strip()),)
    rows = db.q(f"SELECT tg_id,username,first_name,lang,plan,plan_expires_at,bonus_plan,bonus_until,"
               f"is_banned,created_at FROM users {where} ORDER BY created_at DESC LIMIT 100", args)
    return {"ok": True, "users": [dict(r) for r in rows]}


class AdminAction(BaseModel):
    tg_id: int
    action: str
    value: str = ""


@app.post("/admin/api/users/action")
async def admin_user_action(body: AdminAction, _=Depends(require_admin)):
    u = core.get_user(body.tg_id)
    if not u:
        raise HTTPException(404, "user not found")
    if body.action == "ban":
        db.ex("UPDATE users SET is_banned=1 WHERE tg_id=?", (body.tg_id,))
        db.log_admin("admin", "user_banned", f"tg={body.tg_id}")
    elif body.action == "unban":
        db.ex("UPDATE users SET is_banned=0 WHERE tg_id=?", (body.tg_id,))
        db.log_admin("admin", "user_unbanned", f"tg={body.tg_id}")
    elif body.action == "grant_plan":
        if body.value not in core.PLANS or body.value == "free":
            raise HTTPException(400, "bad plan")
        days = 30
        core.activate_subscription(body.tg_id, body.value, "admin", days)
        db.log_admin("admin", "manual_grant", f"tg={body.tg_id} plan={body.value}")
    elif body.action == "bonus_hours":
        core.add_bonus(body.tg_id, "premium", float(body.value or 3), "admin_bonus")
    else:
        raise HTTPException(400, "unknown action")
    return {"ok": True}


@app.get("/admin/api/payments")
async def admin_payments(_=Depends(require_admin)):
    rows = db.q("SELECT * FROM payments ORDER BY id DESC LIMIT 200")
    return {"ok": True, "payments": [dict(r) for r in rows]}


@app.get("/admin/api/log")
async def admin_log(_=Depends(require_admin)):
    rows = db.q("SELECT * FROM admin_log ORDER BY id DESC LIMIT 200")
    return {"ok": True, "log": [dict(r) for r in rows]}


# ---------------- тікети підтримки ----------------
@app.get("/admin/api/tickets")
async def admin_tickets(status: str = "", _=Depends(require_admin)):
    if status in ("open", "answered", "closed"):
        rows = db.q("SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
    else:
        rows = db.q("SELECT * FROM tickets ORDER BY id DESC LIMIT 200")
    out = []
    for t in rows:
        u = core.get_user(t["tg_id"])
        msgs = [dict(m) for m in db.q(
            "SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id", (t["id"],))]
        out.append({"id": t["id"], "tg_id": t["tg_id"],
                    "username": u["username"] if u else "—",
                    "subject": t["subject"], "status": t["status"],
                    "created_at": t["created_at"], "messages": msgs})
    return {"ok": True, "tickets": out}


class TicketReplyIn(BaseModel):
    message: str


@app.post("/admin/api/tickets/{ticket_id}/reply")
async def admin_ticket_reply(ticket_id: int, body: TicketReplyIn, _=Depends(require_admin)):
    t = db.q1("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    if not t:
        raise HTTPException(404, "ticket not found")
    db.ex("INSERT INTO ticket_messages(ticket_id,sender,message,created_at) "
          "VALUES(?,'admin',?,?)",
          (ticket_id, (body.message or "").strip()[:4000], int(time.time())))
    db.ex("UPDATE tickets SET status='answered' WHERE id=?", (ticket_id,))
    # сповіщення клієнту в Telegram (для веб-акаунтів — через прив'язку, інакше тихо)
    try:
        dest = t["tg_id"]
        if dest < 0:
            wu = db.q1("SELECT tg_link FROM web_users WHERE tg_id=?", (dest,))
            dest = wu["tg_link"] if (wu and wu["tg_link"]) else None
        if dest:
            await bot_app.bot.send_message(
                dest,
                f"💬 Відповідь підтримки CHUPIK VPN (тікет #{ticket_id}):\n\n{body.message}")
    except Exception:
        pass
    db.log_admin("admin", "ticket_replied", f"ticket={ticket_id}")
    return {"ok": True}


@app.post("/admin/api/tickets/{ticket_id}/close")
async def admin_ticket_close(ticket_id: int, _=Depends(require_admin)):
    db.ex("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
    db.log_admin("admin", "ticket_closed", f"ticket={ticket_id}")
    return {"ok": True}


# ---------------- групи ----------------
@app.get("/admin/api/groups")
async def admin_groups(_=Depends(require_admin)):
    out = []
    for g in db.q("SELECT * FROM groups ORDER BY id DESC LIMIT 200"):
        owner = core.get_user(g["owner_tg_id"])
        members = [dict(m) for m in db.q(
            "SELECT tg_id, joined_at, status FROM group_members WHERE group_id=?", (g["id"],))]
        for m in members:
            mu = core.get_user(m["tg_id"])
            m["username"] = mu["username"] if mu else "—"
        out.append({"id": g["id"], "owner_tg_id": g["owner_tg_id"],
                    "owner_name": owner["username"] if owner else "—",
                    "created_at": g["created_at"], "members": members})
    return {"ok": True, "groups": out}


@app.get("/admin/api/settings")
async def admin_get_settings(_=Depends(require_admin)):
    rows = db.q("SELECT key,value FROM settings")
    return {"ok": True, "settings": {r["key"]: r["value"] for r in rows}}


@app.post("/admin/api/settings")
async def admin_set_settings(request: Request, _=Depends(require_admin)):
    body = await request.json()
    for k, v in body.items():
        db.set_setting(str(k), str(v))
    db.log_admin("admin", "settings_updated", json.dumps(body)[:300])
    return {"ok": True}


class ServerIn(BaseModel):
    code: str
    name: str
    country: str
    flag: str
    endpoint_host: str
    endpoint_port: int = 51820
    wg_interface: str = "wg0"
    subnet: str = "10.7.0.0/24"
    driver: str = "ssh"
    lon: float = 0.0
    lat: float = 0.0


@app.post("/admin/api/servers")
async def admin_add_server(body: ServerIn, _=Depends(require_admin)):
    db.ex("INSERT INTO servers(code,name,country,flag,endpoint_host,endpoint_port,wg_interface,"
          "subnet,server_public_key,driver,lon,lat,created_at) "
          "VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
          (body.code, body.name, body.country, body.flag, body.endpoint_host,
           body.endpoint_port, body.wg_interface, body.subnet, body.driver,
           body.lon, body.lat, int(time.time())))
    db.log_admin("admin", "server_added", f"{body.code} {body.endpoint_host}")
    # пробуємо одразу отримати публічний ключ сервера
    srv = db.q1("SELECT * FROM servers WHERE code=?", (body.code,))
    try:
        pk = await asyncio.wait_for(wg.ServerDriver(srv).get_server_pubkey(), timeout=15)
        db.ex("UPDATE servers SET server_public_key=? WHERE code=?", (pk, body.code))
    except Exception:
        pass
    return {"ok": True}


@app.post("/admin/api/servers/{server_id}/toggle")
async def admin_toggle_server(server_id: int, _=Depends(require_admin)):
    srv = db.q1("SELECT * FROM servers WHERE id=?", (server_id,))
    if not srv:
        raise HTTPException(404, "server not found")
    db.ex("UPDATE servers SET is_active=1-is_active WHERE id=?", (server_id,))
    db.log_admin("admin", "server_toggled", f"id={server_id}")
    return {"ok": True}


@app.delete("/admin/api/servers/{server_id}")
async def admin_delete_server(server_id: int, _=Depends(require_admin)):
    db.ex("UPDATE servers SET is_active=0 WHERE id=?", (server_id,))
    db.log_admin("admin", "server_disabled", f"id={server_id}")
    return {"ok": True}


# ================================================================
# HTML сторінки оплати
# ================================================================
def _pay_page_html(plan: str) -> str:
    p = core.PLANS[plan]
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Оплата · ЧУПИК VPN</title><style>
body{{font-family:'Manrope',system-ui,sans-serif;background:#05030d;color:#fff;display:grid;
place-items:center;min-height:100vh;margin:0}}
.card{{background:linear-gradient(145deg,#1a0f2b,#0c0716);border:1px solid rgba(192,132,252,.2);
border-radius:24px;padding:40px;max-width:420px;width:92%}}
h1{{font-size:24px;margin:0 0 6px}}.muted{{color:#a9a0bc;font-size:13px}}
.price{{font-size:40px;margin:18px 0;color:#c084fc}}
input{{width:100%;box-sizing:border-box;padding:15px;border-radius:13px;border:1px solid
rgba(192,132,252,.3);background:rgba(121,0,255,.08);color:#fff;font-size:15px;margin-top:8px}}
button{{width:100%;padding:16px;border:0;border-radius:13px;font-weight:800;font-size:14px;
background:linear-gradient(110deg,#6900e8,#9a38ff);color:#fff;margin-top:18px;cursor:pointer}}
.note{{font-size:11px;color:#8e849f;margin-top:14px;line-height:1.6}}</style></head><body>
<div class="card"><h1>ЧУПИК VPN · {p['name']}</h1>
<div class="muted">Подписка на 1 месяц · {p['devices']} устройства</div>
<div class="price">€{p['price_eur']:.2f}</div>
<label class="muted">Ваш Telegram ID (узнать: бот → /profile, или в Mini App)</label>
<input id="tg" type="text" inputmode="numeric" placeholder="например 9227498907">
<button onclick="pay()">Оплатить картой</button>
<div class="note">Оплата карткой — только на сайте (правила Telegram). После подтверждения платежа
подписка активируется автоматически. VPN-конфигурация выдаётся в Mini App.</div></div>
<script>async function pay(){{const tg=document.getElementById('tg').value.trim();
if(!tg){{alert('Укажите Telegram ID');return}}
const r=await fetch('/pay/checkout',{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{plan:'{plan}',tg_id:Number(tg)}})}});const j=await r.json();
if(j.url)location.href=j.url;else alert(j.detail||'Ошибка')}}</script></body></html>"""


def _pay_disabled_page() -> str:
    return "<h2 style='font-family:sans-serif;color:#fff;background:#05030d;padding:40px;" \
           "text-align:center'>Оплата картой ещё не подключена. Следите за новостями.</h2>"


# ================================================================
# Статика адмінки + запуск
# ================================================================
if ADMIN_DIR.exists():
    app.mount("/admin/static", StaticFiles(directory=str(ADMIN_DIR)), name="admin_static")


@app.on_event("startup")
async def startup():
    db.init_db()
    loop = asyncio.get_running_loop()
    # бот (polling) + worker завершення підписок — в одному процесі
    if settings.BOT_TOKEN:
        try:
            dp = await bot_app.setup_bot()
            loop.create_task(dp.start_polling(bot_app.bot, close_bot_session=False))
            print("[api] бот запущен у тому ж процесі")
        except Exception as e:
            print(f"[api] НЕ ВДАЛОСЯ запустити бота (перевір BOT_TOKEN): {e}")
    loop.create_task(core.run_expiry_worker())
