import logging
import os
import asyncio
import re
import sqlite3
from aiohttp import web as aio_web
from datetime import datetime, timedelta
from collections import defaultdict
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ChatPermissions
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler, ChatJoinRequestHandler,
    filters, ContextTypes
)
from telegram.constants import ParseMode, MessageEntityType
from telegram.error import BadRequest, Forbidden

# ══════════════════════════════════════════════════════════
#  কনফিগারেশন
# ══════════════════════════════════════════════════════════

BOT_TOKEN    = "8935165800:AAFDSLq-sFMUE1tHGHrEpbcuBWhAPX9R8W4"
CHANNEL_ID   = -1003361310400
GROUP_ID     = -1003816790885
CHANNEL_LINK = "https://t.me/+Sf0480mqE4hlYTY1"
CHANNEL_NAME = "Bot's Bangladesh 🇧🇩"
PORT         = int(os.environ.get("PORT", 10000))
ADMIN_ID     = 5004684815

MSG_LIMIT              = 10
WARN_DELETE_AFTER      = 170
WELCOME_DELETE_AFTER   = 240
SPAM_LIMIT             = 6
SPAM_WINDOW            = 3600
DB_PATH                = "police_bot.db"
FORWARD_URL_WARN_LIMIT = 5
MAX_MUTE_COUNT         = 24
BIO_WARN_LIMIT         = 5    # Bio violation: ৫ বার warning, তারপর mute

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
#  মেমোরি স্টেট
# ══════════════════════════════════════════════════════════

spam_tracker: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
last_warning: dict = {}
last_welcome: dict = {}
lang_cache:   dict = {}

# ══════════════════════════════════════════════════════════
#  SQLite — ডাটাবেস
# ══════════════════════════════════════════════════════════

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS members (
                user_id       INTEGER PRIMARY KEY,
                msg_count     INTEGER NOT NULL DEFAULT 0,
                limit_reached INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS mute_tracker (
                chat_id         INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                violation_count INTEGER NOT NULL DEFAULT 0,
                mute_count      INTEGER NOT NULL DEFAULT 0,
                is_permanent    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS known_chats (
                chat_id INTEGER PRIMARY KEY
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS known_users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        # Bio violation tracking: warn_count ও mute_days আলাদা রাখো
        con.execute("""
            CREATE TABLE IF NOT EXISTS bio_violations (
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                warn_count INTEGER NOT NULL DEFAULT 0,
                mute_days  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        # Private user language preference
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_lang (
                user_id INTEGER PRIMARY KEY,
                lang    TEXT NOT NULL DEFAULT 'bn'
            )
        """)
        con.commit()
    logger.info("DB ready.")


# ── Known chats / users ──────────────────────────────────

def db_save_chat(chat_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)", (chat_id,))
        con.commit()

def db_save_user(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO known_users (user_id) VALUES (?)", (user_id,))
        con.commit()

def db_get_all_chats() -> list:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT chat_id FROM known_chats").fetchall()
    return [r[0] for r in rows]

def db_get_all_users() -> list:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT user_id FROM known_users").fetchall()
    return [r[0] for r in rows]

def db_remove_chat(chat_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM known_chats WHERE chat_id=?", (chat_id,))
        con.commit()

def db_remove_user(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM known_users WHERE user_id=?", (user_id,))
        con.commit()

# ── Members ──────────────────────────────────────────────

def db_get(user_id: int) -> tuple:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT msg_count, limit_reached FROM members WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row if row else (0, 0)

def db_increment(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO members (user_id, msg_count) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET msg_count = msg_count + 1", (user_id,)
        )
        count = con.execute(
            "SELECT msg_count FROM members WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        con.commit()
    return count

def db_set_limit_reached(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO members (user_id, msg_count, limit_reached) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET limit_reached = 1", (user_id, MSG_LIMIT)
        )
        con.commit()

def db_reset_limit(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE members SET limit_reached = 0, msg_count = 0 WHERE user_id = ?", (user_id,)
        )
        con.commit()

# ── Mute tracker (forward/url violations) ────────────────

def db_get_mute(chat_id: int, user_id: int) -> tuple:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT violation_count, mute_count, is_permanent FROM mute_tracker "
            "WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
    return row if row else (0, 0, 0)

def db_increment_violation(chat_id: int, user_id: int) -> tuple:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO mute_tracker (chat_id, user_id, violation_count) VALUES (?,?,1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET violation_count = violation_count + 1",
            (chat_id, user_id)
        )
        row = con.execute(
            "SELECT violation_count, mute_count, is_permanent FROM mute_tracker "
            "WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        con.commit()
    return row

def db_increment_mute(chat_id: int, user_id: int) -> tuple:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO mute_tracker (chat_id, user_id, mute_count) VALUES (?,?,1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET mute_count = mute_count + 1",
            (chat_id, user_id)
        )
        row = con.execute(
            "SELECT violation_count, mute_count, is_permanent FROM mute_tracker "
            "WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        con.commit()
    return row

def db_set_permanent_mute(chat_id: int, user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO mute_tracker (chat_id, user_id, is_permanent) VALUES (?,?,1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET is_permanent=1",
            (chat_id, user_id)
        )
        con.commit()

# ── Bio violation tracker ────────────────────────────────

def db_get_bio(chat_id: int, user_id: int) -> tuple:
    """(warn_count, mute_days)"""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT warn_count, mute_days FROM bio_violations WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
    return row if row else (0, 0)

def db_increment_bio_warn(chat_id: int, user_id: int) -> tuple:
    """warn_count বাড়াও → (warn_count, mute_days)"""
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO bio_violations (chat_id, user_id, warn_count) VALUES (?,?,1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET warn_count = warn_count + 1",
            (chat_id, user_id)
        )
        row = con.execute(
            "SELECT warn_count, mute_days FROM bio_violations WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
        con.commit()
    return row

def db_increment_bio_mute(chat_id: int, user_id: int) -> int:
    """mute_days বাড়াও → নতুন mute_days"""
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO bio_violations (chat_id, user_id, mute_days) VALUES (?,?,1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET mute_days = mute_days + 1",
            (chat_id, user_id)
        )
        days = con.execute(
            "SELECT mute_days FROM bio_violations WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()[0]
        con.commit()
    return days

# ── User language preference ─────────────────────────────

def db_get_user_lang(user_id: int) -> str | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT lang FROM user_lang WHERE user_id=?", (user_id,)
        ).fetchone()
    return row[0] if row else None

def db_set_user_lang(user_id: int, lang: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO user_lang (user_id, lang) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET lang=?",
            (user_id, lang, lang)
        )
        con.commit()

# ══════════════════════════════════════════════════════════
#  ভাষা নির্ধারণ
# ══════════════════════════════════════════════════════════

def is_bengali_text(text: str) -> bool:
    return any('\u0980' <= ch <= '\u09FF' for ch in text)

async def get_chat_lang(bot, chat_id: int) -> str:
    if chat_id in lang_cache:
        return lang_cache[chat_id]
    try:
        chat  = await bot.get_chat(chat_id)
        title = chat.title or ""
        lang  = "bn" if is_bengali_text(title) else "en"
    except Exception:
        lang = "en"
    lang_cache[chat_id] = lang
    return lang

async def get_chat_title(bot, chat_id: int) -> str:
    try:
        chat = await bot.get_chat(chat_id)
        return chat.title or ""
    except Exception:
        return ""

# ══════════════════════════════════════════════════════════
#  বার্তা টেমপ্লেট (BN + EN)
# ══════════════════════════════════════════════════════════

MSGS = {
    "forward_warn": {
        "bn": "📵 {m}, **ফরোয়ার্ড করা মেসেজ** এই গ্রুপে নিষিদ্ধ।\nসতর্কতা: **{v}/{limit}** — এরপর মিউট করা হবে।",
        "en": "📵 {m}, **forwarded messages are not allowed** in this group.\nWarning **{v}/{limit}** — next will result in a mute.",
    },
    "url_warn": {
        "bn": "🔗 {m}, **লিংক পাঠানো** এই গ্রুপে নিষিদ্ধ।\nসতর্কতা: **{v}/{limit}** — এরপর মিউট করা হবে।",
        "en": "🔗 {m}, **links are not allowed** in this group.\nWarning **{v}/{limit}** — next will result in a mute.",
    },
    "muted_timed": {
        "bn": "🔇 {m}, বারবার নিয়ম লঙ্ঘনের কারণে আপনাকে **{h} ঘন্টার** জন্য মিউট করা হয়েছে।",
        "en": "🔇 {m}, you have been **muted for {h} hour(s)** due to repeated violations.",
    },
    "muted_permanent": {
        "bn": "🔇 {m}, বারবার নিয়ম লঙ্ঘনের কারণে আপনাকে এই গ্রুপে **স্থায়ীভাবে মিউট** করা হয়েছে।",
        "en": "🔇 {m}, you have been **permanently muted** in this group due to repeated violations.",
    },
    "button_deleted": {
        "bn": "🔘 {m}, **বাটনযুক্ত মেসেজ** এই গ্রুপে নিষিদ্ধ।",
        "en": "🔘 {m}, messages with **inline buttons are not allowed** in this group.",
    },
    "mention_deleted": {
        "bn": "📛 {m}, এই গ্রুপে **@username উল্লেখ** করা নিষিদ্ধ।",
        "en": "📛 {m}, **@username mentions are not allowed** in this group.",
    },
    "spam_detected": {
        "bn": "⚠️ {m}, **স্প্যাম শনাক্ত হয়েছে!** একই মেসেজ সর্বোচ্চ {limit}× প্রতি ঘন্টায় পাঠানো যাবে।",
        "en": "⚠️ {m}, **spam detected!** The same message is allowed max {limit}× per hour.",
    },
    "bot_removed": {
        "bn": "🤖 {adder}, এই গ্রুপে **বট যোগ করা নিষিদ্ধ।**\n_{bot_name}_ সরিয়ে দেওয়া হয়েছে।",
        "en": "🤖 {adder}, **adding bots is not allowed** in this group.\n_{bot_name}_ has been removed.",
    },
    "url_bot_deleted": {
        "bn": "🔗 একটি বটের লিংকযুক্ত মেসেজ সরিয়ে দেওয়া হয়েছে।",
        "en": "🔗 A bot message containing a link has been removed.",
    },
    "limit_reached": {
        "bn": (
            "🔒 {m}, আপনার **{limit}টি ফ্রি মেসেজের লিমিট** শেষ হয়েছে!\n\n"
            "এই গ্রুপে আর মেসেজ করতে হলে চ্যানেলে **জয়েন করতে হবে।**\n"
            "জয়েন করলে আনলিমিটেড মেসেজ করতে পারবেন। 🔓"
        ),
        "en": (
            "🔒 {m}, your **{limit} free message limit** has been reached!\n\n"
            "To continue messaging, please **join the channel.**\n"
            "After joining you can send unlimited messages. 🔓"
        ),
    },
    "limit_warning": {
        "bn": (
            "⚠️ {m}, আপনার **{limit}টি ফ্রি মেসেজ শেষ** হয়ে গেছে!\n\n"
            "এখন থেকে মেসেজ করতে চ্যানেলে জয়েন করতে হবে।\n"
            "জয়েন করলে **আনলিমিটেড** মেসেজ করতে পারবেন! 🔓"
        ),
        "en": (
            "⚠️ {m}, your **{limit} free messages are used up!**\n\n"
            "To continue messaging, join the channel.\n"
            "After joining you can send **unlimited** messages! 🔓"
        ),
    },
    "join_confirmed": {
        "bn": "✅ {m}, ধন্যবাদ জয়েন করার জন্য!\nএখন থেকে **আনলিমিটেড** মেসেজ করতে পারবেন। 🎉",
        "en": "✅ {m}, thanks for joining!\nYou can now send **unlimited** messages. 🎉",
    },
    "join_confirmed_answer": {
        "bn": "✅ সফল! এখন আনলিমিটেড মেসেজ করতে পারবেন।",
        "en": "✅ Confirmed! You can now send unlimited messages.",
    },
    "join_not_yet": {
        "bn": "❌ আপনি এখনো চ্যানেলে জয়েন করেননি!\nজয়েন করে আবার চেষ্টা করুন।",
        "en": "❌ You haven't joined the channel yet!\nPlease join and try again.",
    },
    "not_your_button": {
        "bn": "❌ এই বাটনটি আপনার জন্য নয়!",
        "en": "❌ This button is not for you!",
    },
    # ── Bio violation ──────────────────────────────────────
    "bio_warn": {
        "bn": (
            "🚫 {m}, আপনার **Bio তে নিষিদ্ধ তথ্য** রয়েছে:\n"
            "{reasons}\n\n"
            "সতর্কতা: **{w}/{limit}** — এরপর মিউট করা হবে।\n"
            "Bio থেকে সব **লিংক, ফোন নম্বর, @username ও চ্যানেল** সরিয়ে নিন।"
        ),
        "en": (
            "🚫 {m}, your **Bio contains prohibited content**:\n"
            "{reasons}\n\n"
            "Warning: **{w}/{limit}** — next will result in a mute.\n"
            "Please remove all **links, phone numbers, @usernames, and channels** from your Bio."
        ),
    },
    "bio_muted": {
        "bn": (
            "🔇 {m}, Bio তে নিষিদ্ধ তথ্য রাখার কারণে\n"
            "আপনাকে **{days} দিনের** জন্য মিউট করা হয়েছে।\n\n"
            "Bio পরিষ্কার করে গ্রুপে আসুন।"
        ),
        "en": (
            "🔇 {m}, due to prohibited content in your Bio,\n"
            "you have been **muted for {days} day(s)**.\n\n"
            "Please clean your Bio and come back."
        ),
    },
    # ── Welcome ───────────────────────────────────────────
    "welcome": {
        "bn": (
            "👋 স্বাগতম, {m}!\n\n"
            "🏠 **{group}**-এ আপনাকে স্বাগত জানাই।\n\n"
            "📌 গ্রুপের নিয়ম মেনে চলুন:\n"
            "• ফরোয়ার্ড মেসেজ নিষিদ্ধ\n"
            "• লিংক / URL নিষিদ্ধ\n"
            "• @mention নিষিদ্ধ\n"
            "• স্প্যাম নিষিদ্ধ\n\n"
            "আশা করি আপনার সাথে ভালো সময় কাটবে! 😊"
        ),
        "en": (
            "👋 Welcome, {m}!\n\n"
            "🏠 You've joined **{group}**.\n\n"
            "📌 Please follow the group rules:\n"
            "• No forwarded messages\n"
            "• No links / URLs\n"
            "• No @mentions\n"
            "• No spam\n\n"
            "Hope you enjoy your time here! 😊"
        ),
    },
    # ── Admin commands ────────────────────────────────────
    "muted_by_admin": {
        "bn": "🔇 {m} কে **{h} ঘন্টার** জন্য মিউট করা হয়েছে।\nকারণ: এডমিন কমান্ড।",
        "en": "🔇 {m} has been **muted for {h} hour(s)**.\nReason: Admin command.",
    },
    "kicked_by_admin": {
        "bn": "👢 {m} কে গ্রুপ থেকে **কিক** করা হয়েছে।",
        "en": "👢 {m} has been **kicked** from the group.",
    },
    "banned_by_admin": {
        "bn": "🚫 {m} কে গ্রুপ থেকে **ব্যান** করা হয়েছে।",
        "en": "🚫 {m} has been **banned** from the group.",
    },
    "cmd_no_reply": {
        "bn": "⚠️ কমান্ড ব্যবহার করতে কোনো মেসেজে **Reply** করুন।",
        "en": "⚠️ Please **reply** to a message to use this command.",
    },
    "cmd_no_permission": {
        "bn": "❌ শুধু গ্রুপ এডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।",
        "en": "❌ Only group admins can use this command.",
    },
    "cmd_failed": {
        "bn": "❌ কমান্ড সম্পন্ন করা যায়নি। বটের পর্যাপ্ত অনুমতি আছে কিনা দেখুন।",
        "en": "❌ Command failed. Please check that the bot has sufficient permissions.",
    },
    "mute_hours_invalid": {
        "bn": "⚠️ সঠিক ঘন্টা দিন। যেমন: `/mute 2` (২ ঘন্টার জন্য মিউট)",
        "en": "⚠️ Please provide valid hours. Example: `/mute 2` (mute for 2 hours)",
    },
}


def t(key: str, lang: str, **kwargs) -> str:
    return MSGS[key][lang].format(**kwargs)

# ══════════════════════════════════════════════════════════
#  ইউটিলিটি
# ══════════════════════════════════════════════════════════

async def is_channel_member(bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

async def is_bot_admin(bot, chat_id: int) -> bool:
    """বট নিজে ঐ গ্রুপে admin কিনা চেক করো।"""
    try:
        m = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

async def safe_delete(msg):
    try:
        await msg.delete()
    except (BadRequest, Forbidden):
        pass

async def _auto_remove(msg, delay: int):
    await asyncio.sleep(delay)
    await safe_delete(msg)

async def send_warning(bot, chat_id: int, user_id: int, text: str,
                       reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    key = (chat_id, user_id)
    if key in last_warning:
        await safe_delete(last_warning[key])
        del last_warning[key]
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        last_warning[key] = sent
        asyncio.create_task(_auto_remove(sent, WARN_DELETE_AFTER))
    except Exception as e:
        logger.error(f"send_warning error: {e}")

def mention_of(user, lang="en") -> str:
    name = (user.first_name or ("বন্ধু" if lang == "bn" else "User"))
    name = name.replace("[", "").replace("]", "")
    return f"[{name}](tg://user?id={user.id})"

# ══════════════════════════════════════════════════════════
#  Detection helpers
# ══════════════════════════════════════════════════════════

def has_url(msg) -> bool:
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for e in entities:
        if e.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK):
            return True
    text = msg.text or msg.caption or ""
    return bool(re.search(
        r'(https?://|www\.|t\.me/|bit\.ly|tinyurl\.com)', text, re.IGNORECASE
    ))

def has_mention(msg) -> bool:
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for e in entities:
        if e.type == MessageEntityType.MENTION:
            return True
    text = msg.text or msg.caption or ""
    return bool(re.search(r'@\w+', text))

def is_forwarded(msg) -> bool:
    return bool(
        getattr(msg, 'forward_origin', None)
        or getattr(msg, 'forward_from', None)
        or getattr(msg, 'forward_from_chat', None)
        or getattr(msg, 'forward_sender_name', None)
    )

def has_inline_buttons(msg) -> bool:
    return isinstance(msg.reply_markup, InlineKeyboardMarkup)

def is_spam(chat_id: int, user_id: int, text: str) -> bool:
    if not text.strip():
        return False
    now    = datetime.now()
    cutoff = now - timedelta(seconds=SPAM_WINDOW)
    key    = text.strip().lower()
    ts     = spam_tracker[chat_id][user_id][key]
    ts[:]  = [t for t in ts if t > cutoff]
    ts.append(now)
    return len(ts) > SPAM_LIMIT

# ══════════════════════════════════════════════════════════
#  Bio চেক — URL / Phone / @username / Channel (escalating)
# ══════════════════════════════════════════════════════════

_BIO_URL_RE      = re.compile(
    r'(https?://|www\.|t\.me/|telegram\.me/|bit\.ly|tinyurl\.com)', re.IGNORECASE
)
_BIO_PHONE_RE    = re.compile(r'(\+?\d[\d\s\-\(\)]{7,}\d)')
_BIO_USERNAME_RE = re.compile(r'@\w{3,}')

MUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

async def check_bio_violation(bot, msg, user, chat_id: int, lang: str) -> bool:
    """
    Bio চেক করো। নিষিদ্ধ তথ্য পেলে:
    ১–৫ বার: Warning
    ৬তম থেকে: ১ দিন mute, ২ দিন, ৩ দিন... (প্রতিবার ১ দিন বাড়বে)
    """
    try:
        profile = await bot.get_chat(user.id)
    except Exception:
        return False

    bio = (profile.bio or "").strip()

    violations_bn = []
    violations_en = []

    if _BIO_URL_RE.search(bio):
        violations_bn.append("🔗 Bio তে লিংক/URL")
        violations_en.append("🔗 Link/URL in Bio")

    if _BIO_PHONE_RE.search(bio):
        violations_bn.append("📞 Bio তে ফোন নম্বর")
        violations_en.append("📞 Phone number in Bio")

    if _BIO_USERNAME_RE.search(bio):
        violations_bn.append("📛 Bio তে @username")
        violations_en.append("📛 @username in Bio")

    personal_chat = getattr(profile, 'personal_chat', None)
    if personal_chat:
        violations_bn.append("📢 Profile-এ Linked Channel")
        violations_en.append("📢 Linked Channel on Profile")

    if not violations_bn:
        return False

    await safe_delete(msg)

    m       = mention_of(user, lang)
    reasons = "\n".join(
        f"  • {v}" for v in (violations_bn if lang == "bn" else violations_en)
    )

    warn_count, mute_days = db_get_bio(chat_id, user.id)
    new_warn, _ = db_increment_bio_warn(chat_id, user.id)

    if new_warn <= BIO_WARN_LIMIT:
        # এখনো warning পর্যায়ে
        await send_warning(
            bot, chat_id, user.id,
            t("bio_warn", lang, m=m, reasons=reasons, w=new_warn, limit=BIO_WARN_LIMIT)
        )
    else:
        # Warning শেষ — mute escalation
        new_days = db_increment_bio_mute(chat_id, user.id)
        until    = datetime.now() + timedelta(days=new_days)
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user.id,
                permissions=MUTED_PERMISSIONS,
                until_date=until,
            )
            logger.info(f"Bio mute {new_days}d: user {user.id} in {chat_id}")
        except Exception as e:
            logger.error(f"Bio mute failed: {e}")

        await send_warning(
            bot, chat_id, user.id,
            t("bio_muted", lang, m=m, days=new_days)
        )

    logger.info(f"Bio violation (warn {new_warn}): user {user.id} in {chat_id} — {violations_en}")
    return True

# ══════════════════════════════════════════════════════════
#  Mute (escalating — forward/url violations)
# ══════════════════════════════════════════════════════════

async def apply_mute(bot, chat_id: int, user_id: int, mute_count: int) -> tuple:
    if mute_count >= MAX_MUTE_COUNT:
        db_set_permanent_mute(chat_id, user_id)
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id, permissions=MUTED_PERMISSIONS
            )
            logger.info(f"Permanent mute: user {user_id} in {chat_id}")
        except Exception as e:
            logger.error(f"Permanent mute failed: {e}")
        return True, None
    else:
        hours = mute_count
        until = datetime.now() + timedelta(hours=hours)
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=MUTED_PERMISSIONS, until_date=until
            )
            logger.info(f"Timed mute {hours}h: user {user_id} in {chat_id}")
        except Exception as e:
            logger.error(f"Timed mute failed: {e}")
        return False, hours

# ══════════════════════════════════════════════════════════
#  বাটন — চ্যানেল জয়েন
# ══════════════════════════════════════════════════════════

def join_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📢 {CHANNEL_NAME} — জয়েন করুন", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ জয়েন করেছি — চেক করুন", callback_data=f"chk_{user_id}")],
    ])

# ══════════════════════════════════════════════════════════
#  Violation handler (forward + URL, with mute escalation)
# ══════════════════════════════════════════════════════════

async def handle_violation(bot, msg, user, chat_id: int, lang: str, vtype: str) -> bool:
    await safe_delete(msg)
    m = mention_of(user, lang)

    violation_count, mute_count, is_permanent = db_get_mute(chat_id, user.id)
    if is_permanent:
        return True

    violation_count, mute_count, is_permanent = db_increment_violation(chat_id, user.id)

    if violation_count <= FORWARD_URL_WARN_LIMIT:
        key  = "forward_warn" if vtype == "forward" else "url_warn"
        text = t(key, lang, m=m, v=violation_count, limit=FORWARD_URL_WARN_LIMIT)
        await send_warning(bot, chat_id, user.id, text)
    else:
        _, new_mute_count, _ = db_increment_mute(chat_id, user.id)
        is_perm, hours = await apply_mute(bot, chat_id, user.id, new_mute_count)
        text = t("muted_permanent", lang, m=m) if is_perm else t("muted_timed", lang, m=m, h=hours)
        await send_warning(bot, chat_id, user.id, text)

    return True

# ══════════════════════════════════════════════════════════
#  মডারেশন
# ══════════════════════════════════════════════════════════

async def run_moderation(bot, msg, user, chat_id: int) -> bool:
    lang = await get_chat_lang(bot, chat_id)
    m    = mention_of(user, lang)

    if await check_bio_violation(bot, msg, user, chat_id, lang):
        return True
    if is_forwarded(msg):
        return await handle_violation(bot, msg, user, chat_id, lang, "forward")
    if has_inline_buttons(msg):
        await safe_delete(msg)
        await send_warning(bot, chat_id, user.id, t("button_deleted", lang, m=m))
        return True
    if has_url(msg):
        return await handle_violation(bot, msg, user, chat_id, lang, "url")
    if has_mention(msg):
        await safe_delete(msg)
        await send_warning(bot, chat_id, user.id, t("mention_deleted", lang, m=m))
        return True
    text = msg.text or msg.caption or ""
    if is_spam(chat_id, user.id, text):
        await safe_delete(msg)
        await send_warning(bot, chat_id, user.id,
            t("spam_detected", lang, m=m, limit=SPAM_LIMIT))
        return True
    return False

# ══════════════════════════════════════════════════════════
#  Admin Commands — /mute /kick /ban (গ্রুপ এডমিনদের জন্য)
# ══════════════════════════════════════════════════════════

async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    user = update.effective_user
    if not msg or not user:
        return

    chat_id = msg.chat.id
    lang    = await get_chat_lang(ctx.bot, chat_id)

    if not await is_chat_admin(ctx.bot, chat_id, user.id):
        await safe_delete(msg)
        sent = await ctx.bot.send_message(chat_id, t("cmd_no_permission", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))
        return

    if not msg.reply_to_message:
        await safe_delete(msg)
        sent = await ctx.bot.send_message(chat_id, t("cmd_no_reply", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))
        return

    # ঘন্টা নির্ধারণ — /mute 3 → ৩ ঘন্টা
    hours = 1
    if ctx.args:
        try:
            hours = int(ctx.args[0])
            if hours < 1:
                raise ValueError
        except ValueError:
            await safe_delete(msg)
            sent = await ctx.bot.send_message(chat_id, t("mute_hours_invalid", lang),
                                              parse_mode=ParseMode.MARKDOWN)
            asyncio.create_task(_auto_remove(sent, 10))
            return

    target      = msg.reply_to_message.from_user
    target_name = mention_of(target, lang)
    until       = datetime.now() + timedelta(hours=hours)

    await safe_delete(msg)
    try:
        await ctx.bot.restrict_chat_member(
            chat_id=chat_id, user_id=target.id,
            permissions=MUTED_PERMISSIONS, until_date=until
        )
        sent = await ctx.bot.send_message(
            chat_id, t("muted_by_admin", lang, m=target_name, h=hours),
            parse_mode=ParseMode.MARKDOWN
        )
        asyncio.create_task(_auto_remove(sent, WARN_DELETE_AFTER))
        logger.info(f"Admin mute {hours}h: target {target.id} by {user.id} in {chat_id}")
    except Exception as e:
        logger.error(f"Admin mute failed: {e}")
        sent = await ctx.bot.send_message(chat_id, t("cmd_failed", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))


async def cmd_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    user = update.effective_user
    if not msg or not user:
        return

    chat_id = msg.chat.id
    lang    = await get_chat_lang(ctx.bot, chat_id)

    if not await is_chat_admin(ctx.bot, chat_id, user.id):
        await safe_delete(msg)
        sent = await ctx.bot.send_message(chat_id, t("cmd_no_permission", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))
        return

    if not msg.reply_to_message:
        await safe_delete(msg)
        sent = await ctx.bot.send_message(chat_id, t("cmd_no_reply", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))
        return

    target      = msg.reply_to_message.from_user
    target_name = mention_of(target, lang)

    await safe_delete(msg)
    try:
        await ctx.bot.ban_chat_member(chat_id=chat_id, user_id=target.id)
        await ctx.bot.unban_chat_member(chat_id=chat_id, user_id=target.id)
        sent = await ctx.bot.send_message(
            chat_id, t("kicked_by_admin", lang, m=target_name),
            parse_mode=ParseMode.MARKDOWN
        )
        asyncio.create_task(_auto_remove(sent, WARN_DELETE_AFTER))
        logger.info(f"Admin kick: target {target.id} by {user.id} in {chat_id}")
    except Exception as e:
        logger.error(f"Admin kick failed: {e}")
        sent = await ctx.bot.send_message(chat_id, t("cmd_failed", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    user = update.effective_user
    if not msg or not user:
        return

    chat_id = msg.chat.id
    lang    = await get_chat_lang(ctx.bot, chat_id)

    if not await is_chat_admin(ctx.bot, chat_id, user.id):
        await safe_delete(msg)
        sent = await ctx.bot.send_message(chat_id, t("cmd_no_permission", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))
        return

    if not msg.reply_to_message:
        await safe_delete(msg)
        sent = await ctx.bot.send_message(chat_id, t("cmd_no_reply", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))
        return

    target      = msg.reply_to_message.from_user
    target_name = mention_of(target, lang)

    await safe_delete(msg)
    try:
        await ctx.bot.ban_chat_member(chat_id=chat_id, user_id=target.id)
        sent = await ctx.bot.send_message(
            chat_id, t("banned_by_admin", lang, m=target_name),
            parse_mode=ParseMode.MARKDOWN
        )
        asyncio.create_task(_auto_remove(sent, WARN_DELETE_AFTER))
        logger.info(f"Admin ban: target {target.id} by {user.id} in {chat_id}")
    except Exception as e:
        logger.error(f"Admin ban failed: {e}")
        sent = await ctx.bot.send_message(chat_id, t("cmd_failed", lang),
                                          parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(_auto_remove(sent, 10))

# ══════════════════════════════════════════════════════════
#  Main message handler
# ══════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    user = update.effective_user
    if not msg or not user:
        return

    chat_id = msg.chat.id

    if not await is_bot_admin(ctx.bot, chat_id):
        return

    db_save_chat(chat_id)

    if user.is_bot:
        if has_url(msg) or has_inline_buttons(msg):
            await safe_delete(msg)
            lang = await get_chat_lang(ctx.bot, chat_id)
            try:
                sent = await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=t("url_bot_deleted", lang),
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
                asyncio.create_task(_auto_remove(sent, 30))
            except Exception:
                pass
        return

    if await is_chat_admin(ctx.bot, chat_id, user.id):
        return

    deleted = await run_moderation(ctx.bot, msg, user, chat_id)
    if deleted:
        return

    if chat_id == GROUP_ID:
        lang      = await get_chat_lang(ctx.bot, chat_id)
        is_member = await is_channel_member(ctx.bot, user.id)
        m         = mention_of(user, lang)

        if is_member:
            _, limit_reached = db_get(user.id)
            if limit_reached:
                db_reset_limit(user.id)
            return

        msg_count, limit_reached = db_get(user.id)

        if limit_reached:
            await safe_delete(msg)
            await send_warning(
                ctx.bot, chat_id, user.id,
                t("limit_reached", lang, m=m, limit=MSG_LIMIT),
                reply_markup=join_keyboard(user.id),
            )
            return

        new_count = db_increment(user.id)
        if new_count >= MSG_LIMIT:
            db_set_limit_reached(user.id)
            await send_warning(
                ctx.bot, chat_id, user.id,
                t("limit_warning", lang, m=m, limit=MSG_LIMIT),
                reply_markup=join_keyboard(user.id),
            )

# ══════════════════════════════════════════════════════════
#  Welcome — নতুন সদস্য
# ══════════════════════════════════════════════════════════

async def handle_new_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    chat_id = msg.chat.id
    if not await is_bot_admin(ctx.bot, chat_id):
        return

    lang        = await get_chat_lang(ctx.bot, chat_id)
    group_title = await get_chat_title(ctx.bot, chat_id)

    await safe_delete(msg)

    if chat_id in last_welcome:
        await safe_delete(last_welcome[chat_id])
        del last_welcome[chat_id]

    real_users = [u for u in msg.new_chat_members if not u.is_bot]
    if not real_users:
        return

    mentions = ", ".join(mention_of(u, lang) for u in real_users)
    try:
        sent = await ctx.bot.send_message(
            chat_id=chat_id,
            text=t("welcome", lang, m=mentions, group=group_title),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        last_welcome[chat_id] = sent
        asyncio.create_task(_auto_remove(sent, WELCOME_DELETE_AFTER))
    except Exception as e:
        logger.error(f"Welcome send failed: {e}")

# ══════════════════════════════════════════════════════════
#  Bot removal
# ══════════════════════════════════════════════════════════

async def handle_chat_member_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return

    chat_id    = result.chat.id
    new_member = result.new_chat_member

    if not await is_bot_admin(ctx.bot, chat_id):
        return
    if not new_member.user.is_bot:
        return
    if new_member.status not in ("member", "administrator"):
        return

    bot_name = (
        f"@{new_member.user.username}" if new_member.user.username
        else new_member.user.first_name
    )
    try:
        await ctx.bot.ban_chat_member(chat_id=chat_id, user_id=new_member.user.id)
        await ctx.bot.unban_chat_member(chat_id=chat_id, user_id=new_member.user.id)
        logger.info(f"Bot removed: {bot_name} from {chat_id}")
    except Exception as e:
        logger.error(f"Bot remove failed: {e}")
        return

    adder = result.from_user
    if adder and not adder.is_bot:
        lang = await get_chat_lang(ctx.bot, chat_id)
        m    = mention_of(adder, lang)
        await send_warning(ctx.bot, chat_id, adder.id,
            t("bot_removed", lang, adder=m, bot_name=bot_name))

# ══════════════════════════════════════════════════════════
#  Auto-approve join requests
# ══════════════════════════════════════════════════════════

async def handle_join_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return
    if not await is_bot_admin(ctx.bot, req.chat.id):
        return
    try:
        await ctx.bot.approve_chat_join_request(
            chat_id=req.chat.id, user_id=req.from_user.id
        )
        logger.info(f"Auto-approved: user {req.from_user.id} → chat {req.chat.id}")
    except Exception as e:
        logger.error(f"Join approve failed: {e}")

# ══════════════════════════════════════════════════════════
#  Leave service messages
# ══════════════════════════════════════════════════════════

async def handle_leave_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if not await is_bot_admin(ctx.bot, msg.chat.id):
        return
    await safe_delete(msg)

# ══════════════════════════════════════════════════════════
#  চেক বাটন হ্যান্ডলার
# ══════════════════════════════════════════════════════════

async def handle_check_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    clicker = update.effective_user

    try:
        target_uid = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer()
        return

    if clicker.id != target_uid:
        await query.answer(MSGS["not_your_button"]["bn"], show_alert=True)
        return

    if await is_channel_member(ctx.bot, clicker.id):
        db_reset_limit(clicker.id)
        chat_id = query.message.chat.id
        lang    = await get_chat_lang(ctx.bot, chat_id)
        try:
            await query.message.delete()
        except Exception:
            pass
        m = mention_of(clicker, lang)
        try:
            sent = await query.message.chat.send_message(
                t("join_confirmed", lang, m=m), parse_mode=ParseMode.MARKDOWN
            )
            await query.answer(MSGS["join_confirmed_answer"][lang])
            await asyncio.sleep(5)
            await sent.delete()
        except Exception:
            pass
    else:
        lang = await get_chat_lang(ctx.bot, query.message.chat.id)
        await query.answer(MSGS["join_not_yet"][lang], show_alert=True)

# ══════════════════════════════════════════════════════════
#  /start — Language selection (private)
# ══════════════════════════════════════════════════════════

def lang_select_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🇧🇩 বাংলা", callback_data="setlang_bn"),
        InlineKeyboardButton("🇬🇧 English", callback_data="setlang_en"),
    ]])


def start_text(lang: str, bot_username: str) -> str:
    if lang == "bn":
        return (
            "👮‍♂️ *আমি একজন Group Police Bot!*\n"
            "আপনার গ্রুপকে স্প্যাম, লিংক ও অযাচিত মেসেজমুক্ত রাখি।\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ *আমি যা যা করি:*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👋 নতুন সদস্যকে স্বয়ংক্রিয় *Welcome* জানাই\n"
            "📵 *Forwarded message* মুছে দিই\n"
            "🔗 *Link / URL* (লুকানো hyperlink সহ) মুছে দিই\n"
            "📛 *@mention* মুছে দিই\n"
            "⚠️ *Spam* শনাক্ত করে মুছে দিই\n"
            "🔘 *Inline button* যুক্ত মেসেজ মুছে দিই\n"
            "🤖 *অননুমোদিত বট* গ্রুপ থেকে সরিয়ে দিই\n"
            "✅ *Join request* স্বয়ংক্রিয় অনুমোদন করি\n"
            "🗑️ *Join/Leave* সার্ভিস মেসেজ মুছে দিই\n"
            "🚫 *Bio তে Link/Phone/@username/Channel* থাকলে মেসেজ মুছে দিই\n\n"
            "🔇 *Mute escalation (Forward/URL):*\n"
            "  ৫ বার সতর্কতার পর → ১ম=১ঘন্টা, ২য়=২ঘন্টা…\n"
            "  ২৪ বার পর → *স্থায়ী mute*\n\n"
            "🚫 *Bio Mute escalation:*\n"
            "  ৫ বার সতর্কতার পর → ১ম=১দিন, ২য়=২দিন…\n\n"
            "🛡️ *এডমিন কমান্ড (গ্রুপ এডমিনদের জন্য):*\n"
            "  Reply করে: `/mute 2` `/kick` `/ban`\n\n"
            "🌐 গ্রুপের নাম বাংলায় হলে বাংলায়, English হলে English এ কাজ করি\n\n"
            "⚠️ *গুরুত্বপূর্ণ:* আমাকে গ্রুপে *Admin* না করলে কোনো কাজ করব না!\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 *নিচের বাটনে ক্লিক করে আপনার গ্রুপে আমাকে Add করুন*"
        )
    else:
        return (
            "👮‍♂️ *I am a Group Police Bot!*\n"
            "I keep your group free from spam, links, and unwanted messages.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ *What I do:*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👋 Auto *Welcome* new members\n"
            "📵 Delete *forwarded messages*\n"
            "🔗 Delete *links / URLs* (including hidden hyperlinks)\n"
            "📛 Delete *@mentions*\n"
            "⚠️ Detect and delete *spam*\n"
            "🔘 Delete messages with *inline buttons*\n"
            "🤖 Remove *unauthorized bots* from the group\n"
            "✅ Auto-approve *join requests*\n"
            "🗑️ Delete *join/leave* service messages\n"
            "🚫 Delete messages from users with *Link/Phone/@username/Channel in Bio*\n\n"
            "🔇 *Mute escalation (Forward/URL):*\n"
            "  After 5 warnings → 1st=1h, 2nd=2h…\n"
            "  After 24 → *permanent mute*\n\n"
            "🚫 *Bio Mute escalation:*\n"
            "  After 5 warnings → 1st=1 day, 2nd=2 days…\n\n"
            "🛡️ *Admin Commands (for group admins):*\n"
            "  Reply to a message: `/mute 2` `/kick` `/ban`\n\n"
            "🌐 Responds in Bangla for Bangla groups, English for English groups\n\n"
            "⚠️ *Important:* I won't work unless I'm made *Admin* in the group!\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 *Click the button below to add me to your group*"
        )


def start_keyboard(lang: str, bot_username: str) -> InlineKeyboardMarkup:
    if lang == "bn":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "➕ আপনার গ্রুপে Add করুন",
                url=f"https://t.me/{bot_username}?startgroup=true&admin=delete_messages+restrict_members+ban_users"
            )],
            [InlineKeyboardButton(f"📢 {CHANNEL_NAME}", url=CHANNEL_LINK)],
            [InlineKeyboardButton("🌐 ভাষা পরিবর্তন করুন", callback_data="changelang")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "➕ Add me to your group",
                url=f"https://t.me/{bot_username}?startgroup=true&admin=delete_messages+restrict_members+ban_users"
            )],
            [InlineKeyboardButton(f"📢 {CHANNEL_NAME}", url=CHANNEL_LINK)],
            [InlineKeyboardButton("🌐 Change Language", callback_data="changelang")],
        ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    is_private = chat.type == "private"

    if is_private:
        db_save_user(user.id)

    if not is_private:
        await update.message.reply_text(
            "👮‍♂️ *Police Bot সক্রিয় আছে!*\n"
            "_সব নিয়মকানুন জানতে বটে private মেসেজ করুন।_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ভাষা আগে সেট করা আছে কিনা দেখো
    saved_lang = db_get_user_lang(user.id)
    if saved_lang:
        # ভাষা সেট আছে — সরাসরি main message দেখাও
        bot_username = ctx.bot.username or "this_bot"
        await update.message.reply_text(
            start_text(saved_lang, bot_username),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=start_keyboard(saved_lang, bot_username),
            disable_web_page_preview=True,
        )
    else:
        # প্রথমবার — ভাষা সিলেক্ট করতে বলো
        await update.message.reply_text(
            "👋 *Welcome! / স্বাগতম!*\n\n"
            "Please select your language:\nআপনার ভাষা বেছে নিন:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=lang_select_keyboard(),
        )


async def handle_lang_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Language selection ও change callback handler।"""
    query = update.callback_query
    user  = update.effective_user

    if query.data == "changelang":
        await query.answer()
        await query.edit_message_text(
            "🌐 *Please select your language:*\nআপনার ভাষা বেছে নিন:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=lang_select_keyboard(),
        )
        return

    lang = "bn" if query.data == "setlang_bn" else "en"
    db_set_user_lang(user.id, lang)

    bot_username = ctx.bot.username or "this_bot"
    await query.answer("✅ Language set!" if lang == "en" else "✅ ভাষা সেট হয়েছে!")
    await query.edit_message_text(
        start_text(lang, bot_username),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=start_keyboard(lang, bot_username),
        disable_web_page_preview=True,
    )

# ══════════════════════════════════════════════════════════
#  /status — Admin only (শুধু ADMIN_ID দেখতে পাবেন)
# ══════════════════════════════════════════════════════════

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return  # নীরবে ignore — অন্য কেউ জানবে না এই command আছে

    all_chats = db_get_all_chats()

    # কোন গ্রুপে বট admin আছে তা গণনা করো
    admin_chats   = []
    unknown_chats = []

    for chat_id in all_chats:
        try:
            if await is_bot_admin(ctx.bot, chat_id):
                chat = await ctx.bot.get_chat(chat_id)
                title = getattr(chat, 'title', None) or str(chat_id)
                admin_chats.append(f"  ✅ {title} (`{chat_id}`)")
            else:
                chat = await ctx.bot.get_chat(chat_id)
                title = getattr(chat, 'title', None) or str(chat_id)
                unknown_chats.append(f"  ❌ {title} (`{chat_id}`)")
        except Exception:
            unknown_chats.append(f"  ⚠️ `{chat_id}` (এক্সেস নেই)")

    all_users  = db_get_all_users()
    total_users = len(all_users)

    text_lines = [
        "📊 *Bot Status Report*\n",
        f"🤖 *Admin আছি এমন গ্রুপ:* {len(admin_chats)} টি",
        f"❌ *Admin নেই এমন গ্রুপ:* {len(unknown_chats)} টি",
        f"👤 *Private ইউজার (broadcast):* {total_users} জন",
        "",
    ]

    if admin_chats:
        text_lines.append("*✅ Admin হিসেবে আছি:*")
        text_lines.extend(admin_chats)
        text_lines.append("")

    if unknown_chats:
        text_lines.append("*❌ Admin নেই / এক্সেস নেই:*")
        text_lines.extend(unknown_chats)

    await update.message.reply_text(
        "\n".join(text_lines),
        parse_mode=ParseMode.MARKDOWN,
    )

# ══════════════════════════════════════════════════════════
#  Admin Broadcast
# ══════════════════════════════════════════════════════════

async def handle_admin_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    user = update.effective_user

    if not msg or not user or user.id != ADMIN_ID:
        return

    all_chats = db_get_all_chats()
    all_users = db_get_all_users()

    success = 0
    failed  = 0

    for chat_id in all_chats:
        try:
            await msg.copy(chat_id=chat_id)
            success += 1
        except (BadRequest, Forbidden):
            db_remove_chat(chat_id)
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    for uid in all_users:
        if uid == ADMIN_ID:
            continue
        try:
            await msg.copy(chat_id=uid)
            success += 1
        except (BadRequest, Forbidden):
            db_remove_user(uid)
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    total = len(all_chats) + len(all_users) - 1
    try:
        await ctx.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"📡 *Broadcast সম্পন্ন*\n\n"
                f"✅ সফল: {success}\n"
                f"❌ ব্যর্থ: {failed}\n"
                f"📊 মোট: {total}"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

# ══════════════════════════════════════════════════════════
#  Health Server
# ══════════════════════════════════════════════════════════

async def run_health_server():
    app = aio_web.Application()
    app.router.add_get("/",       lambda r: aio_web.Response(text="Police Bot running!"))
    app.router.add_get("/health", lambda r: aio_web.Response(text="OK"))
    runner = aio_web.AppRunner(app)
    await runner.setup()
    await aio_web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Health server → port {PORT}")

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

async def main():
    init_db()
    await run_health_server()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start — private
    app.add_handler(CommandHandler("start", cmd_start))

    # /status — শুধু ADMIN_ID (private ও group উভয়ে)
    app.add_handler(CommandHandler("status", cmd_status))

    # Admin group commands
    app.add_handler(CommandHandler("mute", cmd_mute,
        filters=filters.ChatType.SUPERGROUP))
    app.add_handler(CommandHandler("kick", cmd_kick,
        filters=filters.ChatType.SUPERGROUP))
    app.add_handler(CommandHandler("ban",  cmd_ban,
        filters=filters.ChatType.SUPERGROUP))

    # Admin broadcast — private এ ADMIN_ID যা পাঠাবেন broadcast হবে
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.User(ADMIN_ID) & ~filters.COMMAND,
        handle_admin_broadcast
    ))

    # Welcome — নতুন সদস্য
    app.add_handler(MessageHandler(
        filters.ChatType.SUPERGROUP & filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_new_member
    ), group=0)

    # Main moderation
    app.add_handler(MessageHandler(
        filters.ChatType.SUPERGROUP
        & ~filters.COMMAND
        & ~filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_message
    ), group=1)

    # Leave service messages
    app.add_handler(MessageHandler(
        filters.ChatType.SUPERGROUP & filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_leave_message
    ), group=2)

    # Bot removal
    app.add_handler(ChatMemberHandler(
        handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER
    ))

    # Auto-approve join requests
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Language selection callbacks (setlang_bn, setlang_en, changelang)
    app.add_handler(CallbackQueryHandler(
        handle_lang_callback, pattern=r"^(setlang_bn|setlang_en|changelang)$"
    ))

    # Channel join check button
    app.add_handler(CallbackQueryHandler(
        handle_check_button, pattern=r"^chk_\d+$"
    ))

    await app.initialize()
    await app.bot.set_my_commands([
        BotCommand("start", "👮‍♂️ About this bot"),
    ])

    await app.start()
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=[
            "message",
            "callback_query",
            "chat_member",
            "chat_join_request",
        ],
    )
    logger.info("✅ Police Bot started — polling active.")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
