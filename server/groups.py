"""CHUPIK GROUP — керування групами з балансом.

Правила (щоб група не була «безкінечною безкоштовною віпкою»):
- Групу може створити лише власник АКТИВНОЇ платної підписки (BASIC+).
- Слоти: BASIC — 1, PREMIUM — 3, ULTIMATE — 5 друзів.
- Учасник: окремий акаунт, власний VPN-доступ, ЛИШЕ 1 пристрій (ліміт FREE),
  без керування групою і без фінансів власника.
- Запрошення — одноразові коди з терміном дії (7 днів).
- Коли підписка власника завершується — група деактивується,
  а VPN-пристрої учасників відкликаються автоматично.
"""
import secrets
import time

from . import core, db
from .db import ex, q, q1

GROUP_SLOTS = {"basic": 1, "premium": 3, "ultimate": 5}
INVITE_TTL_DAYS = 7


class GroupError(Exception):
    """Помилка групи (текст безпечний для показу клієнту)."""


def _owner_group(tg_id: int):
    return q1("SELECT * FROM groups WHERE owner_tg_id=?", (tg_id,))


def group_info(tg_id: int) -> dict:
    """Власник: статус групи + учасники. Учасник: група, до якої належить."""
    g = _owner_group(tg_id)
    if g:
        owner = core.get_user(tg_id)
        plan, _ = core.effective_plan(owner)
        active = plan in GROUP_SLOTS
        members = [dict(r) for r in q(
            "SELECT tg_id, joined_at, status FROM group_members WHERE group_id=? AND status='active'",
            (g["id"],))]
        for m in members:
            u = core.get_user(m["tg_id"])
            m["username"] = u["username"] if u else "—"
            m["has_vpn"] = bool(q1(
                "SELECT 1 FROM devices WHERE tg_id=? AND status='active'", (m["tg_id"],)))
        invites = [dict(r) for r in q(
            "SELECT code, created_at, expires_at, uses, max_uses FROM group_invites "
            "WHERE group_id=? AND expires_at>? ORDER BY created_at DESC LIMIT 5",
            (g["id"], int(time.time()))) ]
        return {"role": "owner", "exists": True, "active": active,
                "slots": GROUP_SLOTS.get(plan, 0), "used": len(members),
                "members": members, "invites": invites,
                "requires_plan": "BASIC+"}
    m = q1("SELECT * FROM group_members WHERE tg_id=? AND status='active'", (tg_id,))
    if m:
        g = q1("SELECT * FROM groups WHERE id=?", (m["group_id"],))
        owner = core.get_user(g["owner_tg_id"]) if g else None
        plan, _ = core.effective_plan(owner) if owner else ("free", None)
        return {"role": "member", "exists": True, "active": plan in GROUP_SLOTS,
                "owner_name": owner["username"] if owner else "—",
                "device_limit": 1, "note": "member"}
    return {"role": None, "exists": False}


def create_group(tg_id: int, name: str = "") -> dict:
    user = core.get_user(tg_id)
    if user is None:
        raise GroupError("Спочатку завершіть реєстрацію")
    if user["is_banned"]:
        raise GroupError("Акаунт заблоковано")
    plan, _ = core.effective_plan(user)
    if plan not in GROUP_SLOTS:
        raise GroupError("Група доступна з активною підпискою BASIC, PREMIUM або ULTIMATE")
    if _owner_group(tg_id):
        raise GroupError("Група вже створена")
    ex("INSERT INTO groups(owner_tg_id,name,created_at) VALUES(?,?,?)",
       (tg_id, (name or "Моя группа")[:40], int(time.time())))
    db.log_admin("system", "group_created", f"owner={tg_id}")
    return group_info(tg_id)


def create_invite(owner_tg_id: int) -> str:
    g = _owner_group(owner_tg_id)
    if not g:
        raise GroupError("Спочатку створіть групу")
    info = group_info(owner_tg_id)
    if not info.get("active"):
        raise GroupError("Підписка власника неактивна — запрошення недоступні")
    used = q1("SELECT COUNT(*) c FROM group_members WHERE group_id=? AND status='active'",
              (g["id"],))["c"]
    if used >= GROUP_SLOTS.get(_active_plan(owner_tg_id), 0):
        raise GroupError("Усі слоти групи вже зайняті — подовжіть тариф або видаліть учасника")
    code = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")[:8]
    now = int(time.time())
    ex("INSERT INTO group_invites(code,group_id,created_at,expires_at,max_uses,uses) "
       "VALUES(?,?,?,?,1,0)", (code, g["id"], now, now + INVITE_TTL_DAYS * 86400))
    return code


def _active_plan(tg_id: int) -> str:
    user = core.get_user(tg_id)
    plan, _ = core.effective_plan(user)
    return plan


def join_group(code: str, tg_id: int) -> dict:
    """Приєднатися за кодом запрошення (з сайту або з бота)."""
    inv = q1("SELECT * FROM group_invites WHERE code=?", ((code or "").strip(),))
    if not inv:
        raise GroupError("Запрошення не знайдено")
    if inv["expires_at"] < time.time():
        raise GroupError("Термін дії запрошення закінчився")
    if inv["uses"] >= inv["max_uses"]:
        raise GroupError("Це запрошення вже використано")
    g = q1("SELECT * FROM groups WHERE id=?", (inv["group_id"],))
    if not g or g["owner_tg_id"] == tg_id:
        raise GroupError("Не можна приєднатися до власної групи")
    if _active_plan(g["owner_tg_id"]) not in GROUP_SLOTS:
        raise GroupError("Підписка власника групи неактивна")
    if q1("SELECT 1 FROM group_members WHERE group_id=? AND tg_id=?",
          (g["id"], tg_id)):
        raise GroupError("Ви вже учасник цієї групи")
    used = q1("SELECT COUNT(*) c FROM group_members WHERE group_id=? AND status='active'",
              (g["id"],))["c"]
    if used >= GROUP_SLOTS.get(_active_plan(g["owner_tg_id"]), 0):
        raise GroupError("У групі вже немає вільних слотів")
    ex("INSERT INTO group_members(group_id,tg_id,joined_at,status) VALUES(?,?,?,'active')",
       (g["id"], tg_id, int(time.time())))
    ex("UPDATE group_invites SET uses=uses+1 WHERE code=?", (code,))
    core.upsert_user(tg_id) if not core.get_user(tg_id) else None
    db.log_admin("system", "group_joined", f"group={g['id']} tg={tg_id}")
    core._notify(g["owner_tg_id"], "👥 До вашої групи приєднався новий учасник.")
    return {"ok": True, "group_id": g["id"]}


async def remove_member(owner_tg_id: int, member_tg_id: int) -> bool:
    g = _owner_group(owner_tg_id)
    if not g or member_tg_id == owner_tg_id:
        return False
    m = q1("SELECT * FROM group_members WHERE group_id=? AND tg_id=? AND status='active'",
          (g["id"], member_tg_id))
    if not m:
        return False
    ex("UPDATE group_members SET status='removed' WHERE id=?", (m["id"],))
    # відкликаємо VPN-пристрої видаленого учасника
    for d in q("SELECT * FROM devices WHERE tg_id=? AND status='active'", (member_tg_id,)):
        try:
            await core.remove_device(member_tg_id, d["id"])
        except Exception:
            ex("UPDATE devices SET status='removed' WHERE id=?", (d["id"],))
    db.log_admin("system", "group_member_removed", f"owner={owner_tg_id} member={member_tg_id}")
    core._notify(member_tg_id,
                 "⛔ Вас видалили з групи CHUPIK VPN. VPN-доступ групи відкликано.")
    return True


async def leave_group(tg_id: int) -> bool:
    m = q1("SELECT * FROM group_members WHERE tg_id=? AND status='active'", (tg_id,))
    if not m:
        return False
    g = q1("SELECT * FROM groups WHERE id=?", (m["group_id"],))
    ex("UPDATE group_members SET status='removed' WHERE id=?", (m["id"],))
    for d in q("SELECT * FROM devices WHERE tg_id=? AND status='active'", (tg_id,)):
        try:
            await core.remove_device(tg_id, d["id"])
        except Exception:
            ex("UPDATE devices SET status='removed' WHERE id=?", (d["id"],))
    if g:
        core._notify(g["owner_tg_id"], "👋 Учасник покинув вашу групу.")
    return True


async def on_owner_expired(owner_tg_id: int):
    """Викликається з expiry-воркера: підписка власника завершилась — відкликаємо доступ учасників."""
    g = _owner_group(owner_tg_id)
    if not g:
        return
    for m in q("SELECT * FROM group_members WHERE group_id=? AND status='active'", (g["id"],)):
        ex("UPDATE group_members SET status='removed' WHERE id=?", (m["id"],))
        for d in q("SELECT * FROM devices WHERE tg_id=? AND status='active'", (m["tg_id"],)):
            try:
                await core.remove_device(m["tg_id"], d["id"])
            except Exception:
                ex("UPDATE devices SET status='removed' WHERE id=?", (d["id"],))
        core._notify(m["tg_id"],
                     "⏳ Підписка власника групи завершилась — груповий VPN-доступ відкликано.")
    db.log_admin("system", "group_deactivated", f"owner={owner_tg_id}")


def group_invites_cleanup():
    now = int(time.time())
    ex("DELETE FROM group_invites WHERE expires_at<?", (now,))
