"""Наскрізні тести CHUPIK VPN: оплата (Stars/картка) → підписка → пристрої → завершення.

Запуск:  python3 -m tests.test_e2e
Фейковий wg імітує реальний сервер WireGuard (команди логуються).
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PATH"] = os.path.join(ROOT, "tests", "bin") + ":" + os.environ["PATH"]
os.environ["SECRET_KEY"] = "test-secret-key-32bytes-abcdefgh"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["BOT_TOKEN"] = "9227498907:TEST_FAKE_TOKEN"   # формат валідний, але не справжній
os.environ["DB_PATH"] = "/tmp/chupik_test.db"
os.environ["BASE_URL"] = "http://localhost:9999"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_123"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
os.environ["WG_FAKE_LOG"] = "/tmp/wg_fake.log"
os.environ["WG_FAKE_PEERS"] = "/tmp/wg_fake_peers.txt"

for f in (os.environ["DB_PATH"], os.environ["WG_FAKE_LOG"], os.environ["WG_FAKE_PEERS"]):
    if os.path.exists(f):
        os.remove(f)

from fastapi.testclient import TestClient  # noqa: E402

from server import core, db, payments  # noqa: E402
from server.api import app  # noqa: E402

core.BOT = None  # у тестах не шлемо реальні повідомлення

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name} {extra if not cond else ''}")


def init_data_for(tg_id: int, token: str) -> str:
    """Правильний підпис Telegram initData (як робить клієнт)."""
    user = json.dumps({"id": tg_id, "first_name": "Тест", "username": "test_user"})
    data = {"auth_date": str(int(time.time())), "query_id": "AAA", "user": user}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{k}={v}" for k, v in data.items())


async def main():
    db.init_db()

    # --- 0. seed VPN-сервера (фейковий local-драйвер) ---
    db.ex("INSERT INTO servers(code,name,country,flag,endpoint_host,endpoint_port,wg_interface,"
          "subnet,server_public_key,driver,lon,lat,created_at) "
          "VALUES('de-1','Германия · Франкфурт','Германия','🇩🇪','127.0.0.1',51820,'wg0',"
          "'10.7.0.0/24','SERVERPUB123','local',8.68,50.11,?)", (int(time.time()),))
    srv = db.q1("SELECT * FROM servers WHERE code='de-1'")
    check("сервер додано в БД", srv is not None)

    # --- 1. користувач + рефералка ---
    core.upsert_user(111, "inviter", "Inviter")
    core.upsert_user(222, "friend", "Friend", referrer_id=111)
    check("рефералка записана", core.register_referral(111, 222))
    check("самозапрошення заблоковане", not core.register_referral(222, 222))

    # --- 2. оплата Stars → активація ---
    r = payments.activate_after_payment("stars", "charge_XXX_1", 222, "premium", 410, "XTR")
    check("оплата Stars активувала PREMIUM", r["ok"] and not r["duplicate"])
    u = core.get_user(222)
    check("тариф premium", u["plan"] == "premium")
    exp = u["plan_expires_at"]
    check("дата завершення +30д", exp and exp > time.time() + 29 * 86400)

    # повторна обробка ТОГО Ж платежу — не подвоює підписку
    r2 = payments.activate_after_payment("stars", "charge_XXX_1", 222, "premium", 410, "XTR")
    check("повторний webhook ігнорується", r2["ok"] and r2["duplicate"])
    check("дата не подвоїлась", core.get_user(222)["plan_expires_at"] == exp)

    # реферальна нагорода після першої оплати запрошеного
    inv = core.invited_count(111)
    paid_events = db.q("SELECT * FROM referral_events WHERE referrer_id=111 AND kind='first_paid'")
    check("реферальна нагорода нарахована", len(paid_events) == 1)

    # --- 3. ліміти пристроїв ---
    d1 = await core.add_device(222, srv["id"], "Телефон")
    check("пристрій 1 створено", d1["status"] == "active")
    d2 = await core.add_device(222, srv["id"], "Ноут")
    check("пристрій 2 створено (PREMIUM=4)", d2["status"] == "active")
    ok3 = True
    try:
        for i in range(5):
            await core.add_device(222, srv["id"], f"extra{i}")
    except PermissionError:
        ok3 = False
    check("ліміт 4 пристрої PREMIUM дотримано", not ok3)

    # peer-и реально додані на "сервері"
    log = open(os.environ["WG_FAKE_LOG"]).read()
    check("wg set peer викликано", "peer" in log and "allowed-ips" in log)
    check("IP у конфігу в межах підмережі", d1["client_ip"].startswith("10.7.0."))

    # --- 4. конфіг + QR ---
    conf = core.get_device_config(222, d1["id"])
    check("конфіг згенеровано", "[Peer]" in conf["config"] and "Endpoint = 127.0.0.1:51820" in conf["config"])
    qr = core.device_qr_b64(conf["config"])
    check("QR-код згенеровано", len(base64.b64decode(qr)) > 500)

    # --- 5. видалення пристрою ---
    removed = await core.remove_device(222, d2["id"])
    log = open(os.environ["WG_FAKE_LOG"]).read()
    check("пристрій видалено + wg remove", removed and "remove" in log)

    # --- 6. завершення підписки ---
    db.ex("UPDATE users SET plan_expires_at=? WHERE tg_id=?", (int(time.time()) - 10, 222))
    await core.expire_check()
    u = core.get_user(222)
    check("тариф скинуто у free", u["plan"] == "free")
    active = db.q("SELECT * FROM devices WHERE tg_id=222 AND status='active'")
    check("усі peer-и відкликані", len(active) == 0)
    log = open(os.environ["WG_FAKE_LOG"]).read()
    check("wg remove для peer-ів виконано", log.count("remove") >= 2)
    sub = db.q1("SELECT status FROM subscriptions WHERE tg_id=222 ORDER BY id DESC LIMIT 1")
    check("підписка позначена expired", sub["status"] == "expired")

    # --- 7. HTTP API (TestClient) ---
    with TestClient(app) as c:
        # автентифікація
        bad = c.get("/api/me", headers={"X-Init-Data": "hash=abc"})
        check("невалідний підпис відкинуто", bad.status_code == 401)
        init = init_data_for(222, os.environ["BOT_TOKEN"])
        me = c.get("/api/me", headers={"X-Init-Data": init}).json()
        check("api/me повертає профіль", me["ok"] and me["tg_id"] == 222 and me["plan_name"] == "FREE")

        servers = c.get("/api/servers").json()
        s0 = servers["servers"][0]
        check("api/servers з реальним статусом", servers["ok"] and s0["online"] is True and s0["peers"] >= 0)

        # додавання пристроя через API (free → 1 пристрій)
        dev = c.post("/api/devices", json={"name": "Тест", "server_id": srv["id"]},
                     headers={"X-Init-Data": init})
        check("пристрій через API", dev.status_code == 200)
        dev2 = c.post("/api/devices", json={"name": "Другий", "server_id": srv["id"]},
                      headers={"X-Init-Data": init})
        check("ліміт FREE=1 через API", dev2.status_code == 403)

        cfg = c.get(f"/api/devices/{dev.json()['device']['id']}/config",
                    headers={"X-Init-Data": init}).json()
        check("конфіг+QR через API", "[Interface]" in cfg["config"] and len(cfg["qr_png_base64"]) > 100)

        # Stars invoice — бот не справжній, тому 400/503, але не фейк
        inv2 = c.post("/api/stars/invoice", json={"plan": "premium"},
                      headers={"X-Init-Data": init})
        check("stars invoice без живого бота не фейкається", inv2.status_code in (400, 502, 503, 500))

        # --- 8. Stripe webhook ---
        event = {
            "id": "evt_test_1", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_1", "payment_status": "paid", "currency": "eur",
                "amount_total": 499,
                "metadata": {"tg_id": "222", "plan": "premium"}}}}
        payload = json.dumps(event)
        ts = str(int(time.time()))
        sig = hmac.new(b"whsec_test", f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
        hdr = f"t={ts},v1={sig}"
        r1 = c.post("/webhook/stripe", content=payload.encode(),
                    headers={"stripe-signature": hdr, "Content-Type": "application/json"})
        check("webhook активував підписку", r1.json().get("ok") is True)
        u = core.get_user(222)
        check("картка теж активує tariff", u["plan"] == "premium")
        exp1 = u["plan_expires_at"]

        # той самий івент ще раз — ігнорується
        r2 = c.post("/webhook/stripe", content=payload.encode(),
                    headers={"stripe-signature": hdr})
        check("повторний Stripe-івент ігнорується", r2.json().get("duplicate") is True)
        check("підписка не подвоїлась", core.get_user(222)["plan_expires_at"] == exp1)

        # невірний підпис
        r3 = c.post("/webhook/stripe", content=payload.encode(),
                    headers={"stripe-signature": "t=1,v1=bad"})
        check("невірний підпис Stripe відкинуто", r3.status_code == 400)

        # --- 9. адмін-панель ---
        noauth = c.get("/admin/api/dashboard")
        check("адмін без токена заблокований", noauth.status_code == 401)
        dash = c.get("/admin/api/dashboard", headers={"X-Admin-Token": "test-admin-token"}).json()
        d = dash.get("data", dash)
        check("дашборд з метриками", d["users"] >= 2 and d["revenue_total"] > 0)

        # рефонд Stars (через адмін API немає — перевіряємо функцію з мок-ботом)
        class MockBot:
            async def refund_star_payment(self, *a, **k):
                pass
        from server import bot_app
        bot_app.bot = MockBot()
        ok = await payments.refund_stars(bot_app.bot, 222, "charge_XXX_1")
        check("повернення Stars записано", ok)
        p = db.q1("SELECT status FROM payments WHERE provider_ref='charge_XXX_1'")
        check("платіж позначено refunded", p["status"] == "refunded")

    print(f"\n{'='*50}\n✅ пройдено: {len(PASS)}  ❌ провалено: {len(FAIL)}")
    if FAIL:
        print("Провалено:", *FAIL, sep="\n  - ")
        sys.exit(1)


asyncio.run(main())
