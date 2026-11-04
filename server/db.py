"""База даних (SQLite) — єдина для бота, Mini App, сайту та адмін-панелі."""
import sqlite3
import threading
import time

from .config import settings

_local = threading.local()
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  tg_id INTEGER PRIMARY KEY,
  username TEXT, first_name TEXT,
  lang TEXT DEFAULT 'ru',
  plan TEXT DEFAULT 'free',                 -- базовий оплачений тариф
  plan_expires_at INTEGER,                  -- unix ts, NULL = безкінечно неактивна
  bonus_plan TEXT, bonus_until INTEGER,     -- VIP trial / реферальні бонуси
  referrer_id INTEGER,
  is_banned INTEGER DEFAULT 0,
  is_admin INTEGER DEFAULT 0,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id INTEGER, plan TEXT,
  starts_at INTEGER, expires_at INTEGER,
  status TEXT DEFAULT 'active',             -- active|expired|refunded
  source TEXT,                              -- stars|card|admin|referral
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id INTEGER, server_id INTEGER, name TEXT,
  wg_public_key TEXT, wg_private_key TEXT,  -- приватний ключ зашифрований (Fernet)
  client_ip TEXT,
  status TEXT DEFAULT 'active',             -- active|removed
  last_handshake_at INTEGER, rx_bytes INTEGER DEFAULT 0, tx_bytes INTEGER DEFAULT 0,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS servers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE, name TEXT, country TEXT, flag TEXT,
  endpoint_host TEXT, endpoint_port INTEGER DEFAULT 51820,
  wg_interface TEXT DEFAULT 'wg0',
  server_public_key TEXT,
  subnet TEXT DEFAULT '10.7.0.0/24',
  driver TEXT DEFAULT 'ssh',                -- ssh|local
  lon REAL, lat REAL,
  is_active INTEGER DEFAULT 1,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT,                            -- stars|card
  provider_ref TEXT UNIQUE,                 -- ID транзакції (захист від повторної обробки)
  tg_id INTEGER, plan TEXT,
  amount REAL, currency TEXT,
  status TEXT DEFAULT 'pending',            -- pending|succeeded|failed|refunded
  created_at INTEGER, completed_at INTEGER,
  raw TEXT
);
CREATE TABLE IF NOT EXISTS referral_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  referrer_id INTEGER, invited_id INTEGER, kind TEXT, created_at INTEGER,
  UNIQUE(referrer_id, invited_id, kind)
);
CREATE TABLE IF NOT EXISTS admin_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, actor TEXT, action TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS web_users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  tg_id INTEGER UNIQUE NOT NULL,               -- синтеэтичний ID (негативний), спільна логіка з Telegram
  tg_link INTEGER,                             -- реальний Telegram ID (відновлення пароля, сповіщення)
  link_code TEXT,                              -- одноразовий код прив'язки Telegram
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS web_sessions(
  token TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  created_at INTEGER,
  expires_at INTEGER
);
CREATE TABLE IF NOT EXISTS web_reset_tokens(
  token TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  created_at INTEGER,
  expires_at INTEGER
);
CREATE TABLE IF NOT EXISTS web_logins(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT, tg_id INTEGER,
  ip TEXT, ua TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS groups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_tg_id INTEGER UNIQUE NOT NULL,
  name TEXT,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS group_invites(
  code TEXT PRIMARY KEY,
  group_id INTEGER NOT NULL,
  created_at INTEGER,
  expires_at INTEGER,
  max_uses INTEGER DEFAULT 1,
  uses INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS group_members(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL,
  tg_id INTEGER NOT NULL,
  joined_at INTEGER,
  status TEXT DEFAULT 'active',
  UNIQUE(group_id, tg_id)
);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id INTEGER NOT NULL,
  subject TEXT, category TEXT,
  status TEXT DEFAULT 'open',
  created_at INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS ticket_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  sender TEXT,
  message TEXT,
  created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_devices_tg ON devices(tg_id);
CREATE INDEX IF NOT EXISTS idx_payments_tg ON payments(tg_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_tg ON subscriptions(tg_id);
"""


def conn() -> sqlite3.Connection:
    if getattr(_local, "c", None) is None:
        _local.c = sqlite3.connect(settings.DB_PATH, timeout=15)
        _local.c.row_factory = sqlite3.Row
        _local.c.execute("PRAGMA journal_mode=WAL")
    return _local.c


def _migrate(c):
    """Додавання колонок до вже існуючих баз (без втрати даних)."""
    dcols = [r["name"] for r in c.execute("PRAGMA table_info(devices)").fetchall()]
    if "type" not in dcols:
        c.execute("ALTER TABLE devices ADD COLUMN type TEXT DEFAULT 'other'")
    ucols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "notify_prefs" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN notify_prefs TEXT DEFAULT '{}'")
    # старі таблиці тікетів (from_role/text) → нові назви колонок
    tm = [r["name"] for r in c.execute("PRAGMA table_info(ticket_messages)").fetchall()]
    if tm and "sender" not in tm and "from_role" in tm:
        c.execute("ALTER TABLE ticket_messages RENAME COLUMN from_role TO sender")
        c.execute("ALTER TABLE ticket_messages RENAME COLUMN text TO message")


def init_db():
    with _lock:
        c = conn()
        c.executescript(SCHEMA)
        _migrate(c)
        # значення за замовчуванням (адмін може змінити через панель)
        defaults = {
            "stars_basic": "160",     # ~€1.99
            "stars_premium": "410",   # ~€4.99
            "stars_ultimate": "650",  # ~€7.99
            "referral_enabled": "1",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        c.commit()


def q(sql: str, args=()) -> list:
    with _lock:
        return conn().execute(sql, args).fetchall()


def q1(sql: str, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args=()):
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def get_setting(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, value))


def log_admin(actor: str, action: str, details: str = ""):
    ex("INSERT INTO admin_log(ts,actor,action,details) VALUES(?,?,?,?)",
       (int(time.time()), actor, action, details))
