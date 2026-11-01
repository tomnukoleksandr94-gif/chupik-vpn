#!/usr/bin/env python3
"""
ЧУПИК VPN — Telegram-бот (Этап 1: регистрация, профиль, рефералы, языки)
Запуск: BOT_TOKEN=... MINIAPP_URL=... python bot.py
"""

import asyncio
import logging
import os
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeDefault, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, MenuButtonWebApp, Message,
    ReplyKeyboardMarkup, WebAppInfo,
)

# ---------- конфигурация (токен ТОЛЬКО через переменные окружения!) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MINIAPP_URL = os.environ.get("MINIAPP_URL", "")
BRAND_PHOTO_URL = os.environ.get("BRAND_PHOTO_URL", "")  # фото для /start
DB_PATH = os.environ.get("DB_PATH", "chupik.db")

BOT_USERNAME = "Chupik_vpnBot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chupik")

# ---------- база данных ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY,
            username TEXT, first_name TEXT,
            lang TEXT DEFAULT 'ru',
            plan TEXT DEFAULT 'free',
            referrer_id INTEGER,
            created_at INTEGER
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS referrals(
            referrer_id INTEGER, invited_id INTEGER, created_at INTEGER,
            UNIQUE(referrer_id, invited_id)
        )""")

def upsert_user(msg: Message) -> sqlite3.Row:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(tg_id, username, first_name, created_at) VALUES(?,?,?,?)",
                  (msg.from_user.id, msg.from_user.username, msg.from_user.first_name, int(time.time())))
        c.execute("UPDATE users SET username=?, first_name=? WHERE tg_id=?",
                  (msg.from_user.username, msg.from_user.first_name, msg.from_user.id))
        return c.execute("SELECT * FROM users WHERE tg_id=?", (msg.from_user.id,)).fetchone()

def set_lang(tg_id: int, lang: str):
    with db() as c:
        c.execute("UPDATE users SET lang=? WHERE tg_id=?", (lang, tg_id))

def get_user(tg_id: int) -> sqlite3.Row:
    with db() as c:
        return c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()

def invited_count(tg_id: int) -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (tg_id,)).fetchone()[0]

def add_referral(referrer_id: int, invited_id: int) -> bool:
    if referrer_id == invited_id:
        return False
    with db() as c:
        cur = c.execute("INSERT OR IGNORE INTO referrals(referrer_id, invited_id, created_at) VALUES(?,?,?)",
                        (referrer_id, invited_id, int(time.time())))
        return cur.rowcount > 0

# ---------- переводы (8 языков, по ТЗ) ----------
T = {
"ru": {
 "welcome": "👋 Привет, {name}!\n\nЭто <b>ЧУПИК VPN</b> — быстрый, безопасный и анонимный VPN.\nУправляй подпиской здесь или в Mini App.",
 "open_miniapp": "🚀 Открыть Mini App",
 "menu": ["Купить VPN", "Мой кабинет", "Серверы", "Моя группа", "Пригласить друга", "Поддержка", "Открыть Mini App"],
 "profile": "🪪 <b>Мой кабинет</b>\n\nID: <code>{id}</code>\nТариф: <b>FREE</b>\nУстройства: 0 из 1\n\nПолная статистика появится в Mini App после запуска серверов.",
 "buy": "💳 <b>Тарифы</b>\n\nFREE — €0 · 1 устройство\nBASIC — €1.99 · 2 устройства\nPREMIUM — €4.99 · 4 устройства\nULTIMATE — €7.99 · 5 устройств\n\nОплата Telegram Stars (XTR) подключается на этапе платежей. Подписка активируется только после подтверждения оплаты.",
 "servers": "🌍 <b>Серверы</b>\n\nПлан запуска: Франкфурт, Амстердам, Париж, Лондон, Варшава, Нью-Йорк, Лос-Анджелес, Торонто, Дубай, Сингапур, Токио, Сан-Паулу.\n\nАктуальную карту смотри в Mini App.",
 "group": "👥 <b>Моя группа</b>\n\nГруппы доступны с тарифа BASIC (1 друг), PREMIUM (3 друга) или ULTIMATE (5 друзей). У каждого друга — свой аккаунт и 1 устройство.",
 "referral": "🎁 <b>Пригласи друга</b>\n\nТвоя ссылка:\n<code>https://t.me/{bot}?start=ref_{id}</code>\n\nНаграды за друзей: 1 — 3 часа PREMIUM, 3 — 12 часов, 5 — 24 часа, 10 — 3 дня, 20 — 7 дней.\n\nПриглашено: <b>{n}</b>",
 "help": "ℹ️ <b>Помощь</b>\n\n/start — главное меню\n/profile — кабинет\n/buy — тарифы\n/servers — серверы\n/group — группа\n/referral — пригласить друга\n/language — язык\n/support — поддержка\n/terms — условия\n/paysupport — вопросы оплаты",
 "support": "🛟 <b>Поддержка</b>\n\nОпиши свой вопрос прямо в этом чате — администратор ответит.",
 "terms": "📜 <b>Условия использования</b>\n\nСервис предоставляется «как есть». VPN используется в рамках законов твоей страны. Возврат средств — через поддержку /paysupport.",
 "paysupport": "💳 <b>Вопросы по оплате</b>\n\nНапиши ID транзакции и описание проблемы прямо в этот чат.",
 "lang_saved": "✅ Язык сохранён",
 "choose_lang": "🌐 Выбери язык / Choose language",
 "new_ref": "🎉 Ты пришёл по приглашению! Бонус будет начислен пригласившему.",
},
"uk": {
 "welcome": "👋 Привіт, {name}!\n\nЦе <b>ЧУПИК VPN</b> — швидкий, безпечний та анонімний VPN.\nКеруй підпискою тут або в Mini App.",
 "open_miniapp": "🚀 Відкрити Mini App",
 "menu": ["Купити VPN", "Мій кабінет", "Сервери", "Моя група", "Запросити друга", "Підтримка", "Відкрити Mini App"],
 "profile": "🪪 <b>Мій кабінет</b>\n\nID: <code>{id}</code>\nТариф: <b>FREE</b>\nПристрої: 0 з 1\n\nПовна статистика з'явиться в Mini App після запуску серверів.",
 "buy": "💳 <b>Тарифи</b>\n\nFREE — €0 · 1 пристрій\nBASIC — €1.99 · 2 пристрої\nPREMIUM — €4.99 · 4 пристрої\nULTIMATE — €7.99 · 5 пристроїв\n\nОплата Telegram Stars (XTR) підключається на етапі платежів. Підписка активується лише після підтвердження оплати.",
 "servers": "🌍 <b>Сервери</b>\n\nПлан запуску: Франкфурт, Амстердам, Париж, Лондон, Варшава, Нью-Йорк, Лос-Анджелес, Торонто, Дубай, Сінгапур, Токіо, Сан-Паулу.\n\nАктуальну карту дивись у Mini App.",
 "group": "👥 <b>Моя група</b>\n\nГрупи доступні з тарифу BASIC (1 друг), PREMIUM (3 друзі) або ULTIMATE (5 друзів). У кожного друга — свій акаунт і 1 пристрій.",
 "referral": "🎁 <b>Запроси друга</b>\n\nТвоє посилання:\n<code>https://t.me/{bot}?start=ref_{id}</code>\n\nНагороди: 1 — 3 години PREMIUM, 3 — 12 годин, 5 — 24 години, 10 — 3 дні, 20 — 7 днів.\n\nЗапрошено: <b>{n}</b>",
 "help": "ℹ️ <b>Допомога</b>\n\n/start — головне меню\n/profile — кабінет\n/buy — тарифи\n/servers — сервери\n/group — група\n/referral — запросити друга\n/language — мова\n/support — підтримка\n/terms — умови\n/paysupport — питання оплати",
 "support": "🛟 <b>Підтримка</b>\n\nОпиши своє запитання прямо в цьому чаті — адміністратор відповість.",
 "terms": "📜 <b>Умови використання</b>\n\nСервіс надається «як є». VPN використовується в межах законів твоєї країни. Повернення коштів — через підтримку /paysupport.",
 "paysupport": "💳 <b>Питання щодо оплати</b>\n\nНапиши ID транзакції та опис проблеми прямо в цей чат.",
 "lang_saved": "✅ Мову збережено",
 "choose_lang": "🌐 Обери мову / Choose language",
 "new_ref": "🎉 Ти прийшов за запрошенням! Бонус буде нараховано тому, хто запросив.",
},
"fr": {
 "welcome": "👋 Salut, {name} !\n\nVoici <b>ЧУПИК VPN</b> — un VPN rapide, sécurisé et anonyme.\nGère ton abonnement ici ou dans la Mini App.",
 "open_miniapp": "🚀 Ouvrir la Mini App",
 "menu": ["Acheter VPN", "Mon compte", "Serveurs", "Mon groupe", "Inviter un ami", "Support", "Ouvrir la Mini App"],
 "profile": "🪪 <b>Mon compte</b>\n\nID : <code>{id}</code>\nTarif : <b>FREE</b>\nAppareils : 0 sur 1",
 "buy": "💳 <b>Tarifs</b>\n\nFREE — €0 · 1 appareil\nBASIC — €1,99 · 2 appareils\nPREMIUM — €4,99 · 4 appareils\nULTIMATE — €7,99 · 5 appareils\n\nLe paiement via Telegram Stars (XTR) arrive bientôt. L'abonnement s'active après confirmation du paiement.",
 "servers": "🌍 <b>Serveurs</b>\n\nLancement prévu : Francfort, Amsterdam, Paris, Londres, Varsovie, New York, Los Angeles, Toronto, Dubaï, Singapour, Tokyo, São Paulo. Carte dans la Mini App.",
 "group": "👥 <b>Mon groupe</b>\n\nGroupes disponibles à partir du tarif BASIC (1 ami), PREMIUM (3 amis) ou ULTIMATE (5 amis).",
 "referral": "🎁 <b>Invite un ami</b>\n\nTon lien :\n<code>https://t.me/{bot}?start=ref_{id}</code>\n\nRécompenses : 1 — 3 h PREMIUM, 3 — 12 h, 5 — 24 h, 10 — 3 jours, 20 — 7 jours.\n\nInvités : <b>{n}</b>",
 "help": "ℹ️ <b>Aide</b> : /start, /profile, /buy, /servers, /group, /referral, /language, /support, /terms, /paysupport",
 "support": "🛟 <b>Support</b>\n\nDécris ton problème directement dans ce chat.",
 "terms": "📜 <b>Conditions d'utilisation</b> — le service est fourni « tel quel », dans le respect des lois de ton pays.",
 "paysupport": "💳 <b>Questions de paiement</b> — envoie l'ID de transaction et la description du problème.",
 "lang_saved": "✅ Langue enregistrée",
 "choose_lang": "🌐 Choisis ta langue",
 "new_ref": "🎉 Tu es arrivé par invitation ! Le bonus ira à ton parrain.",
},
"en": {
 "welcome": "👋 Hi, {name}!\n\nWelcome to <b>CHUPIK VPN</b> — fast, secure and anonymous VPN.\nManage your subscription here or in the Mini App.",
 "open_miniapp": "🚀 Open Mini App",
 "menu": ["Buy VPN", "My account", "Servers", "My group", "Invite a friend", "Support", "Open Mini App"],
 "profile": "🪪 <b>My account</b>\n\nID: <code>{id}</code>\nPlan: <b>FREE</b>\nDevices: 0 of 1",
 "buy": "💳 <b>Plans</b>\n\nFREE — €0 · 1 device\nBASIC — €1.99 · 2 devices\nPREMIUM — €4.99 · 4 devices\nULTIMATE — €7.99 · 5 devices\n\nTelegram Stars (XTR) payments are coming soon. Subscription activates only after payment confirmation.",
 "servers": "🌍 <b>Servers</b>\n\nLaunch plan: Frankfurt, Amsterdam, Paris, London, Warsaw, New York, Los Angeles, Toronto, Dubai, Singapore, Tokyo, São Paulo. See the map in the Mini App.",
 "group": "👥 <b>My group</b>\n\nGroups available from BASIC (1 friend), PREMIUM (3 friends) or ULTIMATE (5 friends). Each friend gets their own account and 1 device.",
 "referral": "🎁 <b>Invite a friend</b>\n\nYour link:\n<code>https://t.me/{bot}?start=ref_{id}</code>\n\nRewards: 1 — 3 h PREMIUM, 3 — 12 h, 5 — 24 h, 10 — 3 days, 20 — 7 days.\n\nInvited: <b>{n}</b>",
 "help": "ℹ️ <b>Help</b>: /start, /profile, /buy, /servers, /group, /referral, /language, /support, /terms, /paysupport",
 "support": "🛟 <b>Support</b>\n\nDescribe your issue right in this chat — an administrator will reply.",
 "terms": "📜 <b>Terms of use</b> — the service is provided «as is», in line with the laws of your country.",
 "paysupport": "💳 <b>Payment questions</b> — send the transaction ID and a description of the issue.",
 "lang_saved": "✅ Language saved",
 "choose_lang": "🌐 Choose your language",
 "new_ref": "🎉 You joined by invitation! The bonus goes to the person who invited you.",
},
"es": {
 "welcome": "👋 ¡Hola, {name}!\n\nBienvenido a <b>CHUPIK VPN</b> — VPN rápida, segura y anónima.\nGestiona tu suscripción aquí o en la Mini App.",
 "open_miniapp": "🚀 Abrir Mini App",
 "menu": ["Comprar VPN", "Mi cuenta", "Servidores", "Mi grupo", "Invitar amigo", "Soporte", "Abrir Mini App"],
 "profile": "🪪 <b>Mi cuenta</b>\n\nID: <code>{id}</code>\nPlan: <b>FREE</b>\nDispositivos: 0 de 1",
 "buy": "💳 <b>Planes</b>\n\nFREE — €0 · 1 dispositivo\nBASIC — €1,99 · 2 dispositivos\nPREMIUM — €4,99 · 4 dispositivos\nULTIMATE — €7,99 · 5 dispositivos\n\nPagos con Telegram Stars (XTR) próximamente. La suscripción se activa tras confirmar el pago.",
 "servers": "🌍 <b>Servidores</b>\n\nLanzamiento previsto: Fráncfort, Ámsterdam, París, Londres, Varsovia, Nueva York, Los Ángeles, Toronto, Dubái, Singapur, Tokio, São Paulo. Mapa en la Mini App.",
 "group": "👥 <b>Mi grupo</b>\n\nGrupos disponibles desde BASIC (1 amigo), PREMIUM (3 amigos) o ULTIMATE (5 amigos).",
 "referral": "🎁 <b>Invita a un amigo</b>\n\nTu enlace:\n<code>https://t.me/{bot}?start=ref_{id}</code>\n\nRecompensas: 1 — 3 h PREMIUM, 3 — 12 h, 5 — 24 h, 10 — 3 días, 20 — 7 días.\n\nInvitados: <b>{n}</b>",
 "help": "ℹ️ <b>Ayuda</b>: /start, /profile, /buy, /servers, /group, /referral, /language, /support, /terms, /paysupport",
 "support": "🛟 <b>Soporte</b>\n\nDescribe tu problema directamente en este chat.",
 "terms": "📜 <b>Términos de uso</b> — el servicio se ofrece «tal cual», conforme a las leyes de tu país.",
 "paysupport": "💳 <b>Problemas de pago</b> — envía el ID de la transacción y una descripción.",
 "lang_saved": "✅ Idioma guardado",
 "choose_lang": "🌐 Elige tu idioma",
 "new_ref": "🎉 ¡Llegaste por invitación! El bono va para quien te invitó.",
},
"de": {
 "welcome": "👋 Hallo, {name}!\n\nWillkommen bei <b>CHUPIK VPN</b> — schnelles, sicheres und anonymes VPN.\nVerwalte dein Abo hier oder in der Mini App.",
 "open_miniapp": "🚀 Mini App öffnen",
 "menu": ["VPN kaufen", "Mein Konto", "Server", "Meine Gruppe", "Freund einladen", "Support", "Mini App öffnen"],
 "profile": "🪪 <b>Mein Konto</b>\n\nID: <code>{id}</code>\nTarif: <b>FREE</b>\nGeräte: 0 von 1",
 "buy": "💳 <b>Tarife</b>\n\nFREE — €0 · 1 Gerät\nBASIC — €1,99 · 2 Geräte\nPREMIUM — €4,99 · 4 Geräte\nULTIMATE — €7,99 · 5 Geräte\n\nZahlung mit Telegram Stars (XTR) folgt. Das Abo wird erst nach Zahlungsbestätigung aktiviert.",
 "servers": "🌍 <b>Server</b>\n\nGeplanter Start: Frankfurt, Amsterdam, Paris, London, Warschau, New York, Los Angeles, Toronto, Dubai, Singapur, Tokio, São Paulo. Karte in der Mini App.",
 "group": "👥 <b>Meine Gruppe</b>\n\nGruppen ab BASIC (1 Freund), PREMIUM (3 Freunde) oder ULTIMATE (5 Freunde).",
 "referral": "🎁 <b>Freund einladen</b>\n\nDein Link:\n<code>https://t.me/{bot}?start=ref_{id}</code>\n\nBelohnungen: 1 — 3 Std. PREMIUM, 3 — 12 Std., 5 — 24 Std., 10 — 3 Tage, 20 — 7 Tage.\n\nEingeladen: <b>{n}</b>",
 "help": "ℹ️ <b>Hilfe</b>: /start, /profile, /buy, /servers, /group, /referral, /language, /support, /terms, /paysupport",
 "support": "🛟 <b>Support</b>\n\nBeschreibe dein Problem direkt in diesem Chat.",
 "terms": "📜 <b>Nutzungsbedingungen</b> — der Dienst wird «wie besehen» bereitgestellt, im Rahmen der Gesetze deines Landes.",
 "paysupport": "💳 <b>Zahlungsfragen</b> — sende die Transaktions-ID und eine Beschreibung.",
 "lang_saved": "✅ Sprache gespeichert",
 "choose_lang": "🌐 Wähle deine Sprache",
 "new_ref": "🎉 Du bist per Einladung gekommen! Der Bonus geht an die Person, die dich eingeladen hat.",
},
"it": {
 "welcome": "👋 Ciao, {name}!\n\nBenvenuto su <b>CHUPIK VPN</b> — VPN veloce, sicura e anonima.\nGestisci l'abbonamento qui o nella Mini App.",
 "open_miniapp": "🚀 Apri la Mini App",
 "menu": ["Compra VPN", "Il mio account", "Server", "Il mio gruppo", "Invita un amico", "Supporto", "Apri Mini App"],
 "profile": "🪪 <b>Il mio account</b>\n\nID: <code>{id}</code>\nTariffa: <b>FREE</b>\nDispositivi: 0 di 1",
 "buy": "💳 <b>Tariffe</b>\n\nFREE — €0 · 1 dispositivo\nBASIC — €1,99 · 2 dispositivi\nPREMIUM — €4,99 · 4 dispositivi\nULTIMATE — €7,99 · 5 dispositivi\n\nPagamenti con Telegram Stars (XTR) in arrivo. L'abbonamento si attiva solo dopo la conferma del pagamento.",
 "servers": "🌍 <b>Server</b>\n\nAvvio previsto: Francoforte, Amsterdam, Parigi, Londra, Varsavia, New York, Los Angeles, Toronto, Dubai, Singapore, Tokyo, San Paolo. Mappa nella Mini App.",
 "group": "👥 <b>Il mio gruppo</b>\n\nGruppi disponibili da BASIC (1 amico), PREMIUM (3 amici) o ULTIMATE (5 amici).",
 "referral": "🎁 <b>Invita un amico</b>\n\nIl tuo link:\n<code>https://t.me/{bot}?start=ref_{id}</code>\n\nPremi: 1 — 3 ore PREMIUM, 3 — 12 ore, 5 — 24 ore, 10 — 3 giorni, 20 — 7 giorni.\n\nInvitati: <b>{n}</b>",
 "help": "ℹ️ <b>Aiuto</b>: /start, /profile, /buy, /servers, /group, /referral, /language, /support, /terms, /paysupport",
 "support": "🛟 <b>Supporto</b>\n\nDescrivi il problema direttamente in questa chat.",
 "terms": "📜 <b>Termini di utilizzo</b> — il servizio è fornito «così com'è», nel rispetto delle leggi del tuo Paese.",
 "paysupport": "💳 <b>Problemi di pagamento</b> — invia l'ID della transazione e una descrizione.",
 "lang_saved": "✅ Lingua salvata",
 "choose_lang": "🌐 Scegli la lingua",
 "new_ref": "🎉 Sei arrivato su invito! Il bonus va a chi ti ha invitato.",
},
"zh": {
 "welcome": "👋 你好，{name}！\n\n欢迎来到 <b>CHUPIK VPN</b> — 快速、安全、匿名的 VPN。\n在这里或 Mini App 中管理你的订阅。",
 "open_miniapp": "🚀 打开 Mini App",
 "menu": ["购买 VPN", "我的账户", "服务器", "我的群组", "邀请好友", "支持", "打开 Mini App"],
 "profile": "🪪 <b>我的账户</b>\n\nID：<code>{id}</code>\n套餐：<b>FREE</b>\n设备：0 / 1",
 "buy": "💳 <b>套餐</b>\n\nFREE — €0 · 1 台设备\nBASIC — €1.99 · 2 台设备\nPREMIUM — €4.99 · 4 台设备\nULTIMATE — €7.99 · 5 台设备\n\nTelegram Stars (XTR) 支付即将上线。付款确认后订阅才会激活。",
 "servers": "🌍 <b>服务器</b>\n\n计划上线：法兰克福、阿姆斯特丹、巴黎、伦敦、华沙、纽约、洛杉矶、多伦多、迪拜、新加坡、东京、圣保罗。地图见 Mini App。",
 "group": "👥 <b>我的群组</b>\n\nBASIC（1 位好友）、PREMIUM（3 位）或 ULTIMATE（5 位）可使用群组。",
 "referral": "🎁 <b>邀请好友</b>\n\n你的链接：\n<code>https://t.me/{bot}?start=ref_{id}</code>\n\n奖励：1 位 — PREMIUM 3 小时，3 位 — 12 小时，5 位 — 24 小时，10 位 — 3 天，20 位 — 7 天。\n\n已邀请：<b>{n}</b>",
 "help": "ℹ️ <b>帮助</b>：/start、/profile、/buy、/servers、/group、/referral、/language、/support、/terms、/paysupport",
 "support": "🛟 <b>支持</b>\n\n请直接在此聊天中描述你的问题。",
 "terms": "📜 <b>使用条款</b> — 服务按「原样」提供，请遵守所在国家法律。",
 "paysupport": "💳 <b>付款问题</b> — 请发送交易 ID 和问题描述。",
 "lang_saved": "✅ 语言已保存",
 "choose_lang": "🌐 选择语言",
 "new_ref": "🎉 你是通过邀请链接进来的！奖励将发放给邀请你的人。",
},
}

LANG_NAMES = {"ru": "🇷🇺 Русский", "uk": "🇺🇦 Українська", "fr": "🇫🇷 Français",
              "en": "🇬🇧 English", "es": "🇪🇸 Español", "de": "🇩🇪 Deutsch",
              "it": "🇮🇹 Italiano", "zh": "🇨🇳 中文"}

# ---------- клавиатуры ----------
def main_kb(lang: str):
    m = T[lang]["menu"]
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=m[0]), KeyboardButton(text=m[1])],
        [KeyboardButton(text=m[2]), KeyboardButton(text=m[3])],
        [KeyboardButton(text=m[4]), KeyboardButton(text=m[5])],
        [KeyboardButton(text=m[6])],
    ], resize_keyboard=True)

def webapp_kb(lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=T[lang]["open_miniapp"],
                             web_app=WebAppInfo(url=MINIAPP_URL) if MINIAPP_URL else None)
    ]]) if MINIAPP_URL else None

def lang_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"lang:{code}")]
        for code, name in LANG_NAMES.items()
    ])

# ---------- бот ----------
bot = Bot(token=BOT_TOKEN, default=ParseMode.HTML)
dp = Dispatcher()

def l(user_row) -> str:
    return user_row["lang"] if user_row and user_row["lang"] in T else "ru"

async def send_welcome(msg: Message, user_row):
    lang = l(user_row)
    text = T[lang]["welcome"].format(name=msg.from_user.first_name)
    kb = webapp_kb(lang)
    if BRAND_PHOTO_URL:
        try:
            await msg.answer_photo(BRAND_PHOTO_URL, caption=text, reply_markup=kb)
            await msg.answer("Меню ниже 👇", reply_markup=main_kb(lang))
            return
        except Exception as e:
            log.warning("photo failed: %s", e)
    await msg.answer(text, reply_markup=kb)
    await msg.answer("Меню ниже 👇", reply_markup=main_kb(lang))

@dp.message(CommandStart())
async def cmd_start(msg: Message, command=None):
    user_row = upsert_user(msg)
    lang = l(user_row)
    payload = command.args if command else ""
    if payload and payload.startswith("ref_"):
        try:
            referrer = int(payload[4:])
            if add_referral(referrer, msg.from_user.id):
                try:
                    await bot.send_message(referrer, "🎉 По твоей ссылке пришёл новый друг!")
                except Exception:
                    pass
            await msg.answer(T[lang]["new_ref"])
        except ValueError:
            pass
    elif payload and payload.startswith("buy_"):
        await send_welcome(msg, user_row)
        await msg.answer(T[lang]["buy"])
        return
    elif payload in ("support", "terms", "group", "paysupport"):
        await send_welcome(msg, user_row)
        await msg.answer(T[lang][payload])
        return
    await send_welcome(msg, user_row)

def menu_handler(key):
    async def h(msg: Message):
        user_row = upsert_user(msg)
        lang = l(user_row)
        text = T[lang][key]
        if key == "profile":
            text = text.format(id=msg.from_user.id)
        elif key == "referral":
            text = text.format(bot=BOT_USERNAME, id=msg.from_user.id, n=invited_count(msg.from_user.id))
        kb = webapp_kb(lang)
        if kb:
            await msg.answer(text, reply_markup=kb)
        else:
            await msg.answer(text)
    return h

for key in ("profile", "buy", "servers", "group", "referral", "support", "terms", "paysupport"):
    dp.message.register(menu_handler(key), Command(key))
    # кнопки меню на русском/украинском и т.д. маппим на тот же текст
for lang_code in T:
    for i, key in enumerate(("buy", "profile", "servers", "group", "referral", "support")):
        dp.message.register(menu_handler(key), F.text == T[lang_code]["menu"][i])
    dp.message.register(miniapp_handler, F.text == T[lang_code]["menu"][6])

async def miniapp_handler(msg: Message):
    user_row = upsert_user(msg)
    lang = l(user_row)
    kb = webapp_kb(lang)
    if kb:
        await msg.answer(T[lang]["welcome"].format(name=msg.from_user.first_name), reply_markup=kb)
    else:
        await msg.answer(T[lang]["open_miniapp"])

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    user_row = upsert_user(msg)
    await msg.answer(T[l(user_row)]["help"])

@dp.message(Command("language"))
async def cmd_language(msg: Message):
    upsert_user(msg)
    await msg.answer(T["ru"]["choose_lang"], reply_markup=lang_kb())

@dp.callback_query(F.data.startswith("lang:"))
async def cb_lang(c: CallbackQuery):
    code = c.data.split(":", 1)[1]
    if code in T:
        set_lang(c.from_user.id, code)
        await c.answer(T[code]["lang_saved"])
        await c.message.answer(T[code]["lang_saved"], reply_markup=main_kb(code))
    else:
        await c.answer()

async def setup_bot():
    cmds = [BotCommand(command=c, description=d) for c, d in [
        ("start", "Главное меню"), ("profile", "Мой кабинет"), ("buy", "Купить VPN"),
        ("servers", "Серверы"), ("group", "Моя группа"), ("referral", "Пригласить друга"),
        ("language", "Язык"), ("help", "Помощь"), ("support", "Поддержка"),
        ("terms", "Условия"), ("paysupport", "Вопросы оплаты")]]
    await bot.set_my_commands(cmds, BotCommandScopeDefault())
    if MINIAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="Открыть приложение", web_app=WebAppInfo(url=MINIAPP_URL)))
    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.username)

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Задай BOT_TOKEN (см. README). Не храни токен в коде!")
    init_db()
    await setup_bot()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
