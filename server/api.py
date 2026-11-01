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

from . import bot_app, core, db, payments, wg
from .config import settings

app = FastAPI(title="CHUPIK VPN API", docs_url=None, redoc_url=None)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

ADMIN_DIR = Path(__file__).parent / "admin"


# ================================================================
# Автентифікація Mini App (перевірка підпису Telegram)
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
    init = request.headers.get("X-Init-Data", "") or request.query_params.get("init_data", "")
    user = validate_init_data(init)
    if not user or not user.get("id"):
        raise HTTPException(401, "invalid telegram signature")
    row = core.get_user(user["id"])
    if row and row["is_banned"]:
        raise HTTPException(403, "banned")
    return user


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
            "link": f"https://t.me/{bot_app.BOT_USERNAME}?start=ref_{tg_id}",
            "invited": core.invited_count(tg_id),
            "tiers": core.REF_TIERS,
        },
        "payments": [dict(p) for p in db.q(
            "SELECT provider,plan,amount,currency,status,created_at FROM payments "
            "WHERE tg_id=? ORDER BY id DESC LIMIT 20", (tg_id,))],
    }


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


@app.post("/api/devices")
async def api_add_device(body: DeviceIn, user=Depends(tg_user)):
    try:
        d = await core.add_device(user["id"], body.server_id, body.name)
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
    if not payments.stripe_configured():
        raise HTTPException(503, "card payments not configured")
    if body.plan not in core.PLANS or body.plan == "free":
        raise HTTPException(400, "unknown plan")
    tg_id = body.tg_id or user["id"]
    try:
        url = payments.create_card_checkout(tg_id, body.plan)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "url": url}


# ================================================================
# Сайт: оплата карткою (Stripe) — зовнішня сторінка
# ================================================================
@app.get("/pay", response_class=HTMLResponse)
async def pay_page(plan: str = "premium"):
    if not payments.stripe_configured():
        return HTMLResponse(_pay_disabled_page(), status_code=503)
    if plan not in core.PLANS or plan == "free":
        plan = "premium"
    return HTMLResponse(_pay_page_html(plan))


class PayForm(BaseModel):
    plan: str
    tg_id: int


@app.post("/pay/checkout")
async def pay_checkout(body: PayForm):
    if not payments.stripe_configured():
        raise HTTPException(503, "card payments not configured")
    if body.plan not in core.PLANS or body.plan == "free":
        raise HTTPException(400, "unknown plan")
    if not core.get_user(body.tg_id):
        raise HTTPException(404, "Telegram ID не найден — сначала откройте бота и нажмите /start")
    try:
        url = payments.create_card_checkout(body.tg_id, body.plan)
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
# Stripe webhook
# ================================================================
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
