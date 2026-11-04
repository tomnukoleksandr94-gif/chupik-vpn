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
import urllib.parse

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
os.environ["LIQPAY_PUBLIC_KEY"] = "i_test_pub"
os.environ["LIQPAY_PRIVATE_KEY"] = "test_priv_key_12345"
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

        # --- 10. веб-акаунти сайту (реєстрація / вхід / Bearer) ---
        reg_bad = c.post("/api/web/auth/register", json={"email": "bad-email", "password": "123456"})
        check("реєстрація з кривим email відкинута", reg_bad.status_code == 400)
        reg_dup = c.post("/api/web/auth/register", json={"email": "user@site.ua", "password": "pass123"})
        reg_dup2 = c.post("/api/web/auth/register", json={"email": "user@site.ua", "password": "pass123"})
        check("реєстрація веб-акаунта працює", reg_dup.status_code == 200 and reg_dup2.status_code == 400)
        token = reg_dup.json().get("token", "")
        web_tg_id = reg_dup.json().get("tg_id", 0)
        check("веб-акаунт отримав синтетичний ID", web_tg_id < 0)

        login_bad = c.post("/api/web/auth/login", json={"email": "user@site.ua", "password": "wrong!"})
        check("невірний пароль відкидається", login_bad.status_code == 400)
        login = c.post("/api/web/auth/login", json={"email": "user@site.ua", "password": "pass123"})
        check("вхід веб-акаунта працює", login.status_code == 200 and login.json().get("token"))

        me_web = c.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        check("Bearer-сесія дає доступ до /api/me", me_web.status_code == 200 and me_web.json()["tg_id"] == web_tg_id)
        me_bad = c.get("/api/me", headers={"Authorization": "Bearer not-a-token"})
        check("кривий Bearer відкидається", me_bad.status_code == 401)

        # реферальний код у реєстрації
        reg_ref = c.post("/api/web/auth/register", json={"email": "friend@site.ua", "password": "pass123", "ref": web_tg_id})
        check("реєстрація за реферальним кодом", reg_ref.status_code == 200)
        check("рефералка веб-акаунта записана", core.register_referral(web_tg_id, reg_ref.json()["tg_id"]) is False)  # дублікат

        # --- 11. LiqPay: інвойс + webhook ---
        check("провайдер карток = liqpay (має пріоритет)", payments.card_provider() == "liqpay")
        url = payments.create_liqpay_checkout(222, "premium")
        check("інвойс LiqPay створено", url.startswith("https://www.liqpay.ua/api/3/checkout/"))
        import re as _re
        m = _re.search(r"checkout/([^/]+)/([^/?#]+)", url)
        check("інвойс має data+signature", m and len(m.group(1)) > 20 and len(m.group(2)) > 20)
        order_id = db.q1("SELECT provider_ref FROM payments WHERE provider_ref LIKE 'chupik_222_premium_%' ORDER BY id DESC")
        check("платіж pending записано", order_id is not None)

        # справжній webhook: правильний підпис → активація
        import base64 as _b64
        lp_payload = {"status": "success", "order_id": order_id["provider_ref"], "amount": 4.99, "currency": "EUR"}
        lp_data = _b64.b64encode(json.dumps(lp_payload).encode()).decode()
        lp_sig = base64.b64encode(hashlib.sha1(
            (os.environ["LIQPAY_PRIVATE_KEY"] + lp_data + os.environ["LIQPAY_PRIVATE_KEY"]).encode()).digest()).decode()
        lp_ok = c.post("/webhook/liqpay",
                       content=urllib.parse.urlencode({"data": lp_data, "signature": lp_sig}).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        check("webhook LiqPay активував підписку", lp_ok.status_code == 200 and lp_ok.json().get("ok") is True)
        check("тариф premium після LiqPay", core.get_user(222)["plan"] == "premium")

        # повторний той самий webhook — ігнорується (ідемпотентність)
        lp_dup = c.post("/webhook/liqpay",
                        content=urllib.parse.urlencode({"data": lp_data, "signature": lp_sig}).encode())
        check("повторний LiqPay-webhook ігнорується", lp_dup.json().get("duplicate") is True)

        # невірний підпис
        lp_bad = c.post("/webhook/liqpay",
                        content=urllib.parse.urlencode({"data": lp_data, "signature": "AAAA"}).encode())
        check("невірний підпис LiqPay відкинуто", lp_bad.status_code == 400)

        # невдалий платіж позначається failed
        url2 = payments.create_liqpay_checkout(222, "basic")
        o2 = db.q1("SELECT provider_ref FROM payments WHERE provider_ref LIKE 'chupik_222_basic_%' ORDER BY id DESC")
        lp_fail = {"status": "failure", "order_id": o2["provider_ref"]}
        d2 = _b64.b64encode(json.dumps(lp_fail).encode()).decode()
        s2 = base64.b64encode(hashlib.sha1((os.environ["LIQPAY_PRIVATE_KEY"] + d2 + os.environ["LIQPAY_PRIVATE_KEY"]).encode()).digest()).decode()
        c.post("/webhook/liqpay",
               content=urllib.parse.urlencode({"data": d2, "signature": s2}).encode())
        check("невдалий LiqPay-платіж позначено failed",
              db.q1("SELECT status FROM payments WHERE provider_ref=?", (o2["provider_ref"],))["status"] == "failed")

        # оплата карткою з Bearer-сесії (як робить кабінет сайту)
        co = c.post("/api/card/checkout", json={"plan": "basic"}, headers={"Authorization": f"Bearer {token}"})
        check("checkout карткою з сесії сайту", co.status_code == 200 and co.json().get("provider") == "liqpay")

        # --- 12. тип пристрою ---
        d3 = await core.add_device(222, srv["id"], "Android-фон", "android")
        check("тип пристрою збережено", d3.get("type") == "android")

        # --- 13. групи: баланс слотів за тарифом ---
        from server import groups as _groups
        from server import webauth
        core.upsert_user(333, "m1", "M1")
        core.upsert_user(444, "m2", "M2")
        core.upsert_user(555, "m3", "M3")
        ginfo = _groups.create_group(222, "Друзі")
        check("групу створено для premium", ginfo["role"] == "owner" and ginfo["slots"] == 3)
        inv1 = _groups.create_invite(222)
        check("запрошення створено", bool(inv1))
        jr = _groups.join_group(inv1, 333)
        check("учасник приєднався", jr.get("ok"))
        dup_ok = True
        try:
            _groups.join_group(inv1, 444)
        except _groups.GroupError:
            dup_ok = False
        check("запрошення одноразове", not dup_ok)
        inv2 = _groups.create_invite(222)
        _groups.join_group(inv2, 444)
        inv3 = _groups.create_invite(222)
        _groups.join_group(inv3, 555)
        full_err = None
        try:
            _groups.create_invite(222)
        except _groups.GroupError as e:
            full_err = str(e)
        check("слоти premium=3 вичерпано", full_err is not None)
        dm = await core.add_device(333, srv["id"], "m1 phone", "android")
        check("учасник групи має VPN-пристрій", dm["status"] == "active")
        ok = await _groups.remove_member(222, 333)
        check("учасника видалено", ok)
        check("VPN учасника відкликано",
              db.q1("SELECT COUNT(*) c FROM devices WHERE tg_id=333 AND status='active'")["c"] == 0)
        # закінчення підписки власника → група деактивована
        db.ex("UPDATE users SET plan_expires_at=? WHERE tg_id=?", (int(time.time()) - 10, 222))
        await core.expire_check()
        gi = _groups.group_info(222)
        check("групу деактивовано після закінчення тарифу", gi["active"] is False and gi["used"] == 0)

        # --- 14. тікети підтримки ---
        auth = {"Authorization": f"Bearer {token}"}
        tk = c.post("/api/tickets", json={"subject": "Не працює VPN", "message": "Допоможіть"},
                    headers=auth)
        check("тікет створено", tk.status_code == 200 and tk.json().get("ticket_id"))
        tk_id = tk.json()["ticket_id"]
        rep = c.post(f"/admin/api/tickets/{tk_id}/reply", json={"message": "Вирішено"},
                     headers={"X-Admin-Token": "test-admin-token"})
        check("адмін відповів на тікет", rep.status_code == 200)
        lst = c.get("/api/tickets", headers=auth).json()
        check("відповідь адміна у тікеті",
              any(t["id"] == tk_id and any(m["sender"] == "admin" for m in t["messages"])
                  for t in lst["tickets"]))
        # API-групи веб-акаунтом без тарифу → 400
        gr = c.post("/api/group", json={"name": "x"}, headers=auth)
        check("група без активного тарифу заборонена", gr.status_code == 400)

        # --- 15. профіль: мова, сповіщення, пароль, прив'язка Telegram ---
        lg = c.post("/api/lang", json={"lang": "de"}, headers=auth)
        check("мову збережено", lg.status_code == 200 and core.get_user(web_tg_id)["lang"] == "de")
        c.post("/api/notify-prefs", json={"prefs": {"promos": True, "tickets": False}}, headers=auth)
        check("сповіщення збережено", core.get_notify_prefs(web_tg_id)["promos"] is True)
        pr = c.get("/api/profile", headers=auth).json()
        check("профіль з історією входів",
              len(pr.get("login_history", [])) >= 1 and pr.get("tg_linked") is False)
        cp = c.post("/api/web/auth/change-password",
                    json={"old_password": "pass123", "new_password": "newpass1"}, headers=auth)
        check("пароль змінено", cp.status_code == 200)
        check("стара сесія відкликана", c.get("/api/me", headers=auth).status_code == 401)
        login2 = c.post("/api/web/auth/login", json={"email": "user@site.ua", "password": "newpass1"})
        check("вхід з новим паролем", login2.status_code == 200)
        token = login2.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        # скидання пароля
        rr = webauth.request_reset("user@site.ua")
        rst = c.post("/api/web/auth/reset", json={"token": rr["token"], "new_password": "resetpass1"})
        check("пароль скинуто", rst.status_code == 200)
        login3 = c.post("/api/web/auth/login", json={"email": "user@site.ua", "password": "resetpass1"})
        check("вхід після скидання", login3.status_code == 200)
        token = login3.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        # прив'язка Telegram
        lc = c.post("/api/web/auth/link", headers=auth).json()
        check("код прив'язки видано", bool(lc.get("code")))
        email = webauth.confirm_tg_link(lc["code"], 777)
        check("Telegram прив'язано", email == "user@site.ua")
        fg = c.post("/api/web/auth/forgot", json={"email": "user@site.ua"})
        check("forgot доставляється через Telegram", fg.json().get("delivered") == "telegram")
        # видалення акаунта
        reg_del = c.post("/api/web/auth/register", json={"email": "todelete@site.ua", "password": "pass123"})
        dl = c.delete("/api/web/auth/delete",
                      headers={"Authorization": f"Bearer {reg_del.json()['token']}"})
        check("акаунт видалено", dl.status_code == 200)
        check("вхід видаленого акаунта неможливий",
              c.post("/api/web/auth/login", json={"email": "todelete@site.ua",
                                                  "password": "pass123"}).status_code == 400)

        # вихід з веб-сесії
        out = c.post("/api/web/auth/logout", headers={"Authorization": f"Bearer {token}"})
        check("вихід з сесії працює", out.status_code == 200)
        me_after = c.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        check("сесію після виходу відкликано", me_after.status_code == 401)

    print(f"\n{'='*50}\n✅ пройдено: {len(PASS)}  ❌ провалено: {len(FAIL)}")
    if FAIL:
        print("Провалено:", *FAIL, sep="\n  - ")
        sys.exit(1)


asyncio.run(main())
