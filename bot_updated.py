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
ADMIN_ID     = 5004684815       # একমাত্র bot admin — broadcast করতে পারবেন

MSG_LIMIT               = 15     # একজন সদস্যের ফ্রি মেসেজ লিমিট
WARN_DELETE_AFTER       = 200    # নোটিশ কতো সেকেন্ড পরে মুছবে
WELCOME_DELETE_AFTER    = 300    # welcome মেসেজ কতো সেকেন্ড পরে মুছবে (৫ মিনিট)
SPAM_LIMIT              = 10     # একই মেসেজ সর্বোচ্চ কতোবার/ঘন্টা
SPAM_WINDOW             = 3600   # স্প্যাম উইন্ডো (১ ঘন্টা)
DB_PATH                 = "police_bot.db"
FORWARD_URL_WARN_LIMIT  = 3      # এতোবার পর্যন্ত শুধু warning, এরপর mute
MAX_MUTE_COUNT          = 24     # মোট ২৪ বার mute হলে permanent

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
#  মেমোরি স্টেট
# ══════════════════════════════════════════════════════════

spam_tracker: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
last_warning: dict  = {}
last_welcome: dict  = {}  # chat_id → Message (শেষ welcome মেসেজ)
lang_cache: dict    = {}  # chat_id → "bn" | "en"

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
        # Broadcast: সব গ্রুপ ও private user ট্র্যাক করো
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
        con.commit()
    logger.info("DB ready.")


def db_save_chat(chat_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)", (chat_id,)
        )
        con.commit()


def db_save_user(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO known_users (user_id) VALUES (?)", (user_id,)
        )
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


def db_get(user_id: int) -> tuple:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT msg_count, limit_reached FROM members WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    return row if row else (0, 0)


def db_increment(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO members (user_id, msg_count) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET msg_count = msg_count + 1",
            (user_id,)
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
            "ON CONFLICT(user_id) DO UPDATE SET limit_reached = 1",
            (user_id, MSG_LIMIT)
        )
        con.commit()


def db_reset_limit(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE members SET limit_reached = 0, msg_count = 0 WHERE user_id = ?",
            (user_id,)
        )
        con.commit()


def db_get_mute(chat_id: int, user_id: int) -> tuple:
    """(violation_count, mute_count, is_permanent)"""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT violation_count, mute_count, is_permanent FROM mute_tracker "
            "WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
    return row if row else (0, 0, 0)


def db_increment_violation(chat_id: int, user_id: int) -> tuple:
    """violation_count বাড়াও → (violation_count, mute_count, is_permanent)"""
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO mute_tracker (chat_id, user_id, violation_count) VALUES (?,?,1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET violation_count = violation_count + 1",
            (chat_id, user_id)
        )
        row = con.execute(
            "SELECT violation_count, mute_count, is_permanent FROM mute_tracker "
            "WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
        con.commit()
    return row


def db_increment_mute(chat_id: int, user_id: int) -> tuple:
    """mute_count বাড়াও → (violation_count, mute_count, is_permanent)"""
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO mute_tracker (chat_id, user_id, mute_count) VALUES (?,?,1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET mute_count = mute_count + 1",
            (chat_id, user_id)
        )
        row = con.execute(
            "SELECT violation_count, mute_count, is_permanent FROM mute_tracker "
            "WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
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

# ══════════════════════════════════════════════════════════
#  ভাষা নির্ধারণ
# ══════════════════════════════════════════════════════════

def is_bengali_text(text: str) -> bool:
    """বাংলা Unicode character আছে কিনা চেক করো (U+0980–U+09FF)।"""
    return any('\u0980' <= ch <= '\u09FF' for ch in text)


async def get_chat_lang(bot, chat_id: int) -> str:
    """গ্রুপের নাম দেখে ভাষা নির্ধারণ করো — cache করা থাকে।"""
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
    """গ্রুপের নাম ফেরত দাও।"""
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
        "bn": "📵 {m}, **ফরোয়ার্ড করা মেসেজ** এই গ্রুপে নিষিদ্ধ।\n সতর্কতা: **{v}/{limit}** — এরপর মিউট করা হবে।",
        "en": "📵 {m}, **forwarded messages are not allowed** in this group.\nWarning **{v}/{limit}** — next will result in a mute.",
    },
    "url_warn": {
        "bn": "🔗 {m}, **লিংক পাঠানো** এই গ্রুপে নিষিদ্ধ।\n সতর্কতা: **{v}/{limit}** — এরপর মিউট করা হবে।",
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
    # Welcome messages
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
}


def t(key: str, lang: str, **kwargs) -> str:
    """ভাষা অনুযায়ী মেসেজ টেমপ্লেট পূরণ করো।"""
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
    """পুরনো নোটিশ মুছে নতুন পাঠাও, WARN_DELETE_AFTER সেকেন্ড পরে মুছবে।"""
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
        r'(https?://|www\.|t\.me/|bit\.ly|tinyurl\.com)',
        text, re.IGNORECASE
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
#  Mute (escalating)
# ══════════════════════════════════════════════════════════

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


async def apply_mute(bot, chat_id: int, user_id: int, mute_count: int) -> tuple:
    """
    mute_count অনুযায়ী mute করো।
    Returns: (is_permanent: bool, hours: int | None)
    """
    if mute_count >= MAX_MUTE_COUNT:
        db_set_permanent_mute(chat_id, user_id)
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=MUTED_PERMISSIONS,
            )
            logger.info(f"Permanent mute: user {user_id} in {chat_id}")
        except Exception as e:
            logger.error(f"Permanent mute failed: {e}")
        return True, None
    else:
        hours = mute_count   # ১ম mute=1h, ২য়=2h, ... ২৪তম=24h
        until = datetime.now() + timedelta(hours=hours)
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=MUTED_PERMISSIONS,
                until_date=until,
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
        [InlineKeyboardButton(
            f"📢 {CHANNEL_NAME} — জয়েন করুন",
            url=CHANNEL_LINK
        )],
        [InlineKeyboardButton(
            "✅ জয়েন করেছি — চেক করুন",
            callback_data=f"chk_{user_id}"
        )],
    ])

# ══════════════════════════════════════════════════════════
#  Violation handler (forward + URL, with mute escalation)
# ══════════════════════════════════════════════════════════

async def handle_violation(bot, msg, user, chat_id: int, lang: str, vtype: str) -> bool:
    """
    Forward বা URL violation:
    - ১–৩ বার: warning শুধু
    - ৪+ বার: mute (১h → ২h → ... → permanent at ২৪)
    Returns True (মেসেজ মুছে দেওয়া হয়েছে)।
    """
    await safe_delete(msg)
    m = mention_of(user, lang)

    violation_count, mute_count, is_permanent = db_get_mute(chat_id, user.id)

    if is_permanent:
        return True

    violation_count, mute_count, is_permanent = db_increment_violation(chat_id, user.id)

    if violation_count <= FORWARD_URL_WARN_LIMIT:
        key = "forward_warn" if vtype == "forward" else "url_warn"
        text = t(key, lang, m=m, v=violation_count, limit=FORWARD_URL_WARN_LIMIT)
        await send_warning(bot, chat_id, user.id, text)
    else:
        _, new_mute_count, _ = db_increment_mute(chat_id, user.id)
        is_perm, hours = await apply_mute(bot, chat_id, user.id, new_mute_count)
        if is_perm:
            text = t("muted_permanent", lang, m=m)
        else:
            text = t("muted_timed", lang, m=m, h=hours)
        await send_warning(bot, chat_id, user.id, text)

    return True

# ══════════════════════════════════════════════════════════
#  মডারেশন
# ══════════════════════════════════════════════════════════

async def run_moderation(bot, msg, user, chat_id: int) -> bool:
    lang = await get_chat_lang(bot, chat_id)
    m    = mention_of(user, lang)

    # Forward → mute escalation
    if is_forwarded(msg):
        return await handle_violation(bot, msg, user, chat_id, lang, "forward")

    # Inline buttons
    if has_inline_buttons(msg):
        await safe_delete(msg)
        await send_warning(bot, chat_id, user.id, t("button_deleted", lang, m=m))
        return True

    # URL / hidden hyperlink → mute escalation
    if has_url(msg):
        return await handle_violation(bot, msg, user, chat_id, lang, "url")

    # @mention
    if has_mention(msg):
        await safe_delete(msg)
        await send_warning(bot, chat_id, user.id, t("mention_deleted", lang, m=m))
        return True

    # Spam
    text = msg.text or msg.caption or ""
    if is_spam(chat_id, user.id, text):
        await safe_delete(msg)
        await send_warning(bot, chat_id, user.id,
            t("spam_detected", lang, m=m, limit=SPAM_LIMIT))
        return True

    return False

# ══════════════════════════════════════════════════════════
#  Main message handler
# ══════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    user = update.effective_user
    if not msg or not user:
        return

    chat_id = msg.chat.id

    # গ্রুপ ট্র্যাক করো (broadcast এর জন্য)
    db_save_chat(chat_id)

    # বট মেসেজ: শুধু URL / button চেক করো (non-admin bot হলেও)
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

    # Admin → সব কিছু ছাড়
    if await is_chat_admin(ctx.bot, chat_id, user.id):
        return

    # মডারেশন (সব গ্রুপে)
    deleted = await run_moderation(ctx.bot, msg, user, chat_id)
    if deleted:
        return

    # ── GROUP_ID: মেসেজ লিমিট সিস্টেম ──
    if chat_id == GROUP_ID:
        lang      = await get_chat_lang(ctx.bot, chat_id)
        is_member = await is_channel_member(ctx.bot, user.id)
        m         = mention_of(user, lang)

        if is_member:
            msg_count, limit_reached = db_get(user.id)
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
#  Welcome — নতুন সদস্য (Rose bot style)
# ══════════════════════════════════════════════════════════

async def handle_new_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    নতুন সদস্য যোগ হলে welcome পাঠাও।
    নতুন কেউ আসলে আগের welcome সাথে সাথে মুছে নতুনটা পাঠাবে।
    WELCOME_DELETE_AFTER সেকেন্ড পরে এটিও auto-delete হবে।
    """
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    chat_id     = msg.chat.id
    lang        = await get_chat_lang(ctx.bot, chat_id)
    group_title = await get_chat_title(ctx.bot, chat_id)

    # System "X joined" মেসেজটা মুছো
    await safe_delete(msg)

    # আগের welcome মেসেজ থাকলে এখনই মুছো
    if chat_id in last_welcome:
        await safe_delete(last_welcome[chat_id])
        del last_welcome[chat_id]

    # বট হলে welcome নয়
    real_users = [u for u in msg.new_chat_members if not u.is_bot]
    if not real_users:
        return

    # একাধিক সদস্য একসাথে join করলে সবাইকে একটাই মেসেজে উল্লেখ করো
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
#  Rule 3 — Unauthorized bot removal (all groups)
# ══════════════════════════════════════════════════════════

async def handle_chat_member_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return

    chat_id    = result.chat.id
    new_member = result.new_chat_member

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
#  Rule 7 — Auto-approve join requests
# ══════════════════════════════════════════════════════════

async def handle_join_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return
    try:
        await ctx.bot.approve_chat_join_request(
            chat_id=req.chat.id,
            user_id=req.from_user.id
        )
        logger.info(f"Auto-approved: user {req.from_user.id} → chat {req.chat.id}")
    except Exception as e:
        logger.error(f"Join approve failed: {e}")

# ══════════════════════════════════════════════════════════
#  Rule 8 — Delete leave service messages (join handled above)
# ══════════════════════════════════════════════════════════

async def handle_leave_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
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
        await query.answer(
            MSGS["not_your_button"]["bn"], show_alert=True
        )
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
                t("join_confirmed", lang, m=m),
                parse_mode=ParseMode.MARKDOWN
            )
            await query.answer(MSGS["join_confirmed_answer"][lang])
            await asyncio.sleep(5)
            await sent.delete()
        except Exception:
            pass
    else:
        lang = await get_chat_lang(ctx.bot, query.message.chat.id)
        await query.answer(
            MSGS["join_not_yet"][lang],
            show_alert=True
        )

# ══════════════════════════════════════════════════════════
#  /start command
# ══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat    = update.effective_chat
    is_private = chat.type == "private"

    # Private user ট্র্যাক করো (broadcast এর জন্য)
    if is_private:
        db_save_user(user.id)

    # গ্রুপে /start দিলে শুধু ছোট নোটিশ
    if not is_private:
        await update.message.reply_text(
            "👮‍♂️ *Police Bot সক্রিয় আছে!*\n"
            "_সব নিয়মকানুন জানতে বটে private মেসেজ করুন।_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Bot username নাও (Add to Group লিংকের জন্য)
    bot_username = ctx.bot.username or "this_bot"

    await update.message.reply_text(
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
        "🗑️ *Join/Leave* সার্ভিস মেসেজ মুছে দিই\n\n"
        "🔇 *Mute escalation:*\n"
        "  ৩ বার সতর্কতার পর → ১ম=১ঘন্টা, ২য়=২ঘন্টা…\n"
        "  ২৪ বার পর → *স্থায়ী mute*\n\n"
        "🌐 গ্রুপের নাম বাংলায় হলে বাংলায়, English হলে English এ কাজ করি\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 *নিচের বাটনে ক্লিক করে আপনার গ্রুপে আমাকে Add করুন*\n"
        "_(Add করার পর আমাকে Admin বানাতে ভুলবেন না!)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "➕ আপনার গ্রুপে Add করুন",
                url=f"https://t.me/{bot_username}?startgroup=true&admin=delete_messages+restrict_members+ban_users"
            )],
            [InlineKeyboardButton(
                f"📢 {CHANNEL_NAME}",
                url=CHANNEL_LINK
            )],
        ])
    )

# ══════════════════════════════════════════════════════════
#  Admin Broadcast
# ══════════════════════════════════════════════════════════

async def handle_admin_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Admin (ADMIN_ID) private এ যা পাঠাবেন তা সব গ্রুপ ও
    সব private user এর কাছে broadcast হবে।
    শুধু Admin জানবেন, অন্য কেউ জানবে না।
    """
    msg  = update.message
    user = update.effective_user

    if not msg or not user or user.id != ADMIN_ID:
        return

    all_chats = db_get_all_chats()
    all_users = db_get_all_users()

    success = 0
    failed  = 0

    # সব গ্রুপে পাঠাও
    for chat_id in all_chats:
        try:
            await msg.copy(chat_id=chat_id)
            success += 1
        except (BadRequest, Forbidden):
            db_remove_chat(chat_id)   # বট সরানো হয়েছে — রেকর্ড মুছো
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)   # Rate limit এড়াতে

    # সব private user এ পাঠাও
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

    # Admin কে রিপোর্ট দাও (শুধু Admin দেখবে)
    total = len(all_chats) + len(all_users) - 1
    try:
        await ctx.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📡 *Broadcast সম্পন্ন*\n\n"
                 f"✅ সফল: {success}\n"
                 f"❌ ব্যর্থ: {failed}\n"
                 f"📊 মোট: {total}",
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

    # /start
    app.add_handler(CommandHandler("start", cmd_start))

    # Admin broadcast — private এ admin যা পাঠাবেন broadcast হবে
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.User(ADMIN_ID) & ~filters.COMMAND,
        handle_admin_broadcast
    ))

    # Welcome — নতুন সদস্য join (group 0 — সর্বোচ্চ priority)
    app.add_handler(MessageHandler(
        filters.ChatType.SUPERGROUP & filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_new_member
    ), group=0)

    # Main moderation + message limit (group 1)
    # NEW_CHAT_MEMBERS বাদ দিলাম — welcome handler (group 0) আলাদা সামলাবে
    app.add_handler(MessageHandler(
        filters.ChatType.SUPERGROUP
        & ~filters.COMMAND
        & ~filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_message
    ), group=1)

    # Leave service messages delete (group 2)
    app.add_handler(MessageHandler(
        filters.ChatType.SUPERGROUP & filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_leave_message
    ), group=2)

    # Bot removal via ChatMember updates
    app.add_handler(ChatMemberHandler(
        handle_chat_member_update,
        ChatMemberHandler.CHAT_MEMBER
    ))

    # Auto-approve join requests
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Check button
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
