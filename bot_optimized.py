import asyncio
import sqlite3
import logging
import re
import os
import psutil
import gc
import aiohttp
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from telegram import Update, Chat, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ChatMemberHandler, CallbackQueryHandler
)
from telegram.error import TelegramError, BadRequest
from telethon import TelegramClient
from telethon.sessions import StringSession

# ============================================================================
# CONFIGURATION
# ============================================================================

OWNER_IDS = [
    7977094135,
    "7892703589",
]

CHECKER_BOT_TOKEN = "8517198376:AAGiT2FwUTtWFEWTA5Qj4dGtspH1_HrNZ6M"
CHECKER_BOT_ID = int(CHECKER_BOT_TOKEN.split(":")[0]) if CHECKER_BOT_TOKEN and ":" in CHECKER_BOT_TOKEN else None

BOT_TOKENS = [
    "8631319823:AAH8xfhOHKKe6kOrLX4KOGYlucpIAYPXLWY",
    "8544480677:AAFnHCJwkHgBAa76LvASDMV55slVs7UsyfY",
    "8745542349:AAHfjamNWBeX9kubK8PONlphy2xc32dX4FU",
]

REQUIRED_CHANNEL = "https://t.me/+YRWG1jUOhetkYWZl"
CHANNEL_URL = REQUIRED_CHANNEL
REQUIRED_CHANNEL_ID = -1003361310400

API_ID = 35539512
API_HASH = "83fce2b4b5b6e2a9073465014daf0f2d"
SESSION_STRING = "1BVtsOIUBu4skQ5MsjD9OWhVrveb5HH5NCNAj_lEVTeYvw9yFL2CymqiXNLk3gc9PwOGLb3Msy8Vlu1TayyLIPTgowTWYEs7HX32EQ6Hv1cP--REyRc-4UEKknIUUQEOBWmfHlx7wWqa7RoZevxfQmC9_zBALna9NVbbkwwcxbIGqbcGhHC4lYcQne7rYE4GFu1pPYjotFIgYN-_A8cHPZasy2t7_6gEunP7jNOcwLCR81Z3BQRqdFnaZsdQg4iXtzQcmqko0BTH1U6bwzROL_lXBk3MpPSH6D87ftZxo4-HXc5oouYVpf-E__DCtRc8EA9--8Asd4zheSTBbtRvQc3dAyg905hA="

MASTER_CONFIG_MESSAGE_TITLE = "[GLOBAL_BOT_CONFIG]"

URL_BACKUP_CHANNEL_ID = -1003971432392
URL_BATCH_MAX_CHARS = 3500

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# WEB SERVER
# ============================================================================

app = Flask("")
_start_time = datetime.now()

@app.route("/")
def home():
    return "Bot System is Live - Group URLs Only!"

@app.route("/health")
def health():
    try:
        mem = psutil.virtual_memory()
        uptime = str(datetime.now() - _start_time).split(".")[0]
        return {
            "status": "ok",
            "uptime": uptime,
            "memory_percent": mem.percent,
            "memory_used_mb": round(mem.used / 1024 / 1024, 1),
            "memory_total_mb": round(mem.total / 1024 / 1024, 1),
        }, 200
    except Exception as e:
        return {"status": "error", "detail": str(e)}, 500

def run_web():
    port = int(os.environ.get("PORT", 10000))
    while True:
        try:
            try:
                from waitress import serve
                serve(app, host="0.0.0.0", port=port, threads=4)
            except ImportError:
                app.run(host="0.0.0.0", port=port)
        except Exception as e:
            logger.error(f"Web server crashed: {e} — restarting in 5s")
            import time
            time.sleep(5)

# ============================================================================
# SHARED AIOHTTP SESSION  (RAM সাশ্রয় — প্রতিবার নতুন session তৈরি হয় না)
# ============================================================================

_http_session: aiohttp.ClientSession | None = None

async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        timeout = aiohttp.ClientTimeout(total=15)
        connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
        _http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _http_session

async def close_http_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None

# ============================================================================
# URL HELPERS
# ============================================================================

def is_telegram_private_url(url):
    patterns = [
        r'https?://t\.me/\+[a-zA-Z0-9_-]+',
        r'https?://telegram\.me/\+[a-zA-Z0-9_-]+',
        r'https?://www\.t\.me/\+[a-zA-Z0-9_-]+',
    ]
    for pattern in patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False

async def check_if_group_url(url):
    """
    URL ভিজিট করে চেক করা — গ্রুপ নাকি চ্যানেল।

    RAM সাশ্রয়:
    - Shared session ব্যবহার (প্রতিবার নতুন session খোলে না)
    - মাত্র প্রথম 50KB পড়ে — পুরো HTML load হয় না
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        session = await get_http_session()
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            # মাত্র ৫০KB পড়ো — পুরো পেজ RAM-এ লোড না করে
            raw = await response.content.read(50 * 1024)
            html = raw.decode("utf-8", errors="ignore")
            html_lower = html.lower()

            if 'channel' in html_lower and 'subscribers' in html_lower:
                return False
            if 'members' in html_lower or 'join group' in html_lower:
                return True
            if 'channel' in html_lower:
                return False
            if 'group' in html_lower:
                return True
            return True  # default: assume group
    except asyncio.TimeoutError:
        logger.error(f"Timeout checking URL: {url}")
        return False
    except Exception as e:
        logger.error(f"Error checking URL {url}: {e}")
        return False

# ============================================================================
# MASTER CONFIG MANAGER
# ============================================================================

class MasterConfigManager:
    def __init__(self):
        self.config_message_id = None
        self.config_message = None
        self.client = None
        self.initialized = False

    async def initialize(self):
        try:
            self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
            await self.client.start()
            db_id = db.get_setting("master_config_msg_id")
            if db_id:
                try:
                    msg = await self.client.get_messages('me', ids=int(db_id))
                    if msg and msg.text and MASTER_CONFIG_MESSAGE_TITLE in msg.text:
                        self.config_message_id = msg.id
                        self.config_message = msg
                        await self.load_config_to_db()
                        self.initialized = True
                        return
                except Exception:
                    pass
            saved_messages = await self.client.get_messages('me', limit=100)
            for msg in saved_messages:
                if msg.text and MASTER_CONFIG_MESSAGE_TITLE in msg.text:
                    self.config_message_id = msg.id
                    self.config_message = msg
                    db.set_setting("master_config_msg_id", str(msg.id))
                    await self.load_config_to_db()
                    self.initialized = True
                    return
            new_msg = await self.client.send_message(
                'me',
                f"**{MASTER_CONFIG_MESSAGE_TITLE}**\n\n# BOT_ID|INTERVAL|MSG_ID"
            )
            self.config_message_id = new_msg.id
            self.config_message = new_msg
            db.set_setting("master_config_msg_id", str(new_msg.id))
            self.initialized = True
        except Exception as e:
            logger.error(f"Error initializing Master Config: {e}")

    async def load_config_to_db(self):
        if not self.config_message or not self.config_message.text:
            return
        for line in self.config_message.text.split('\n'):
            if line.strip() and not line.startswith('#') and MASTER_CONFIG_MESSAGE_TITLE not in line:
                parts = line.split('|')
                if len(parts) == 3:
                    try:
                        b_id, interval, m_id = int(parts[0]), int(parts[1]), int(parts[2])
                        db.set_auto_msg(b_id, interval, m_id)
                    except Exception:
                        pass

    async def update_bot_config(self, bot_id, interval, msg_id):
        if not self.initialized:
            return
        current_text = self.config_message.text or ""
        lines = current_text.split('\n')
        new_lines = []
        found = False
        for line in lines:
            if line.startswith(f"{bot_id}|"):
                new_lines.append(f"{bot_id}|{interval}|{msg_id}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{bot_id}|{interval}|{msg_id}")
        new_text = "\n".join(new_lines)
        try:
            await self.client.edit_message('me', self.config_message_id, new_text)
            self.config_message.text = new_text
        except Exception as e:
            logger.error(f"Error updating master config: {e}")

    async def remove_bot_config(self, bot_id):
        if not self.initialized:
            return
        current_text = self.config_message.text or ""
        new_lines = [l for l in current_text.split('\n') if not l.startswith(f"{bot_id}|")]
        new_text = "\n".join(new_lines)
        try:
            await self.client.edit_message('me', self.config_message_id, new_text)
            self.config_message.text = new_text
        except Exception as e:
            logger.error(f"Error removing bot config: {e}")

    async def restore_all_auto_messages_from_config(self):
        if not self.initialized or not self.config_message:
            return False
        try:
            restored_count = 0
            for line in (self.config_message.text or "").split('\n'):
                if line.strip() and not line.startswith('#') and MASTER_CONFIG_MESSAGE_TITLE not in line:
                    parts = line.split('|')
                    if len(parts) == 3:
                        try:
                            bot_id, interval, msg_id = int(parts[0]), int(parts[1]), int(parts[2])
                            if bot_id == CHECKER_BOT_ID:
                                continue
                            db.set_auto_msg(bot_id, interval, msg_id)
                            restored_count += 1
                        except Exception as e:
                            logger.error(f"Error restoring config for line '{line}': {e}")
            return restored_count > 0
        except Exception as e:
            logger.error(f"Error restoring auto messages: {e}")
            return False

# ============================================================================
# DATABASE
# ============================================================================

class Database:
    def __init__(self):
        self.db_path = "bot_master_db.sqlite"
        self.setup()

    def get_conn(self):
        conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        # WAL mode: concurrent read/write, কম লক, কম crash
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # RAM cache সীমিত করো (default 2MB → 512KB)
        conn.execute("PRAGMA cache_size=-512")
        return conn

    def setup(self):
        with self.get_conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS groups
                         (bot_id INTEGER, chat_id INTEGER, last_active TIMESTAMP,
                          PRIMARY KEY (bot_id, chat_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS urls
                         (url TEXT PRIMARY KEY, discovered_at TIMESTAMP,
                          sent_to_bot2 INTEGER DEFAULT 0,
                          global_counter INTEGER, is_group INTEGER DEFAULT 0)""")
            # ★ নতুন: URL type SQLite-এ cache (HTTP request বারবার হবে না)
            conn.execute("""CREATE TABLE IF NOT EXISTS url_type_cache
                         (url TEXT PRIMARY KEY, is_group INTEGER, checked_at TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS auto_messages
                         (bot_id INTEGER PRIMARY KEY, interval_min INTEGER,
                          last_sent TIMESTAMP, message_data_id INTEGER)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS reply_map
                         (admin_msg_id INTEGER PRIMARY KEY, original_chat_id INTEGER,
                          original_msg_id INTEGER, bot_id INTEGER)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS settings
                         (key TEXT PRIMARY KEY, value TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS group_replies
                         (user_id INTEGER PRIMARY KEY, last_reply_time TIMESTAMP)""")
            conn.commit()
            if self.get_setting("global_url_counter") is None:
                self.set_setting("global_url_counter", "0")

    def get_url_type_from_cache(self, url):
        """SQLite cache থেকে URL type পড়ো — HTTP call লাগে না"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT is_group FROM url_type_cache WHERE url = ?", (url,)
            )
            row = cursor.fetchone()
            return row[0] if row else None  # None মানে cache-এ নেই

    def save_url_type_cache(self, url, is_group):
        """URL type SQLite-এ save করো"""
        with self.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO url_type_cache (url, is_group, checked_at) VALUES (?, ?, ?)",
                (url, 1 if is_group else 0, datetime.now())
            )
            conn.commit()

    def add_group(self, bot_id, chat_id):
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO groups (bot_id, chat_id, last_active) VALUES (?, ?, ?) "
                "ON CONFLICT(bot_id, chat_id) DO UPDATE SET last_active = excluded.last_active",
                (bot_id, chat_id, datetime.now())
            )
            conn.commit()

    def get_groups(self, bot_id):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id, last_active FROM groups WHERE bot_id = ?", (bot_id,))
            return cursor.fetchall()

    def url_exists(self, url):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM urls WHERE url = ? LIMIT 1", (url,))
            return cursor.fetchone() is not None

    async def save_url(self, url):
        try:
            if not is_telegram_private_url(url):
                return False
            if self.url_exists(url):
                return False

            # ★ আগে SQLite cache চেক করো — HTTP request এড়াও
            cached_type = self.get_url_type_from_cache(url)
            if cached_type is not None:
                is_group = bool(cached_type)
            else:
                # Cache-এ নেই, HTTP check করো
                is_group = await check_if_group_url(url)
                self.save_url_type_cache(url, is_group)

            if not is_group:
                logger.info(f"Skipping channel URL: {url}")
                return False

            with self.get_conn() as conn:
                current_counter = int(self.get_setting("global_url_counter") or "0")
                next_counter = current_counter + 1
                now = datetime.now()
                try:
                    conn.execute(
                        "INSERT INTO urls (url, discovered_at, global_counter, is_group) "
                        "VALUES (?, ?, ?, ?)",
                        (url, now, next_counter, 1)
                    )
                    conn.commit()
                    self.set_setting("global_url_counter", str(next_counter))
                    logger.info(f"New GROUP URL saved: {url}")
                    try:
                        await url_backup.backup_url(url, next_counter)
                    except Exception as be:
                        logger.error(f"URL backup error: {be}")
                    return True
                except sqlite3.IntegrityError:
                    return False
        except Exception as e:
            logger.error(f"Error saving URL {url}: {e}")
            return False

    def get_unsent_urls_for_sending(self, limit=10):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT url, global_counter FROM urls
                WHERE sent_to_bot2 = 0 AND is_group = 1
                ORDER BY global_counter ASC
                LIMIT ?
            """, (limit,))
            return cursor.fetchall()

    def mark_urls_as_sent(self, urls):
        if not urls:
            return
        with self.get_conn() as conn:
            placeholders = ",".join(["?"] * len(urls))
            conn.execute(
                f"UPDATE urls SET sent_to_bot2 = 1 WHERE url IN ({placeholders})",
                urls
            )
            conn.commit()

    def set_auto_msg(self, bot_id, interval, msg_id):
        with self.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO auto_messages (bot_id, interval_min, last_sent, message_data_id) "
                "VALUES (?, ?, ?, ?)",
                (bot_id, interval, datetime.now() - timedelta(minutes=interval), msg_id)
            )
            conn.commit()

    def get_auto_msgs(self):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT bot_id, interval_min, last_sent, message_data_id FROM auto_messages"
            )
            return cursor.fetchall()

    def update_last_sent(self, bot_id):
        with self.get_conn() as conn:
            conn.execute(
                "UPDATE auto_messages SET last_sent = ? WHERE bot_id = ?",
                (datetime.now(), bot_id)
            )
            conn.commit()

    def delete_auto_msg(self, bot_id):
        with self.get_conn() as conn:
            conn.execute("DELETE FROM auto_messages WHERE bot_id = ?", (bot_id,))
            conn.commit()

    def save_reply_map(self, admin_msg_id, chat_id, msg_id, bot_id):
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO reply_map VALUES (?, ?, ?, ?)",
                (admin_msg_id, chat_id, msg_id, bot_id)
            )
            conn.commit()

    def get_reply_info(self, admin_msg_id):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT original_chat_id, original_msg_id, bot_id FROM reply_map WHERE admin_msg_id = ?",
                (admin_msg_id,)
            )
            return cursor.fetchone()

    def set_setting(self, key, value):
        with self.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
            conn.commit()

    def get_setting(self, key):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None

    def can_reply_in_group(self, user_id):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_reply_time FROM group_replies WHERE user_id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
            if row:
                last_time = (
                    datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S.%f")
                    if isinstance(row[0], str) else row[0]
                )
                if datetime.now() - last_time < timedelta(hours=24):
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO group_replies (user_id, last_reply_time) VALUES (?, ?)",
                (user_id, datetime.now())
            )
            conn.commit()
            return True

# ============================================================================
# URL BACKUP MANAGER — RAM সাশ্রয়ী সংস্করণ
# ============================================================================

class URLBackupManager:
    """
    RAM সাশ্রয়ের জন্য পরিবর্তন:
    - current_msg_text RAM-এ রাখা হয় না
    - Telethon দিয়ে সরাসরি channel থেকে message text পড়া হয়
    - current_msg_len শুধু track করা হয় (full text নয়)
    - Restore: একটা একটা করে process, কখনো সব RAM-এ না
    """

    INDEX_HEADER = "[URL_BACKUP_INDEX]"

    def __init__(self):
        self.channel_id = URL_BACKUP_CHANNEL_ID
        self.bot = None
        self.telethon_client = None          # master_config থেকে share করবো
        self.index_msg_id = None
        self.backup_msg_ids = []
        self.current_msg_id = None
        self.current_msg_len = 0             # ★ text নয়, শুধু length
        self.initialized = False

    async def initialize(self, bot, telethon_client=None):
        self.bot = bot
        self.telethon_client = telethon_client
        try:
            chat = await bot.get_chat(self.channel_id)
            pinned = chat.pinned_message
            if pinned and self.INDEX_HEADER in (pinned.text or ""):
                self.index_msg_id = pinned.message_id
                self._parse_index(pinned.text)
                logger.info(
                    f"URLBackup: found index msg {self.index_msg_id}, "
                    f"{len(self.backup_msg_ids)} batch(es)"
                )
                await self._restore_urls_to_db()
            else:
                logger.info("URLBackup: no index found, creating fresh.")
                await self._create_index()
            self.initialized = True
        except Exception as e:
            logger.error(f"URLBackupManager init error: {e}")

    def _parse_index(self, text):
        self.backup_msg_ids = []
        self.current_msg_id = None
        self.current_msg_len = 0
        for line in (text or "").strip().split("\n"):
            line = line.strip()
            if line.startswith("COUNTER:"):
                try:
                    counter_val = int(line.split(":", 1)[1])
                    db.set_setting("global_url_counter", str(counter_val))
                except Exception:
                    pass
            elif line.startswith("CURRENT:"):
                try:
                    v = int(line.split(":", 1)[1])
                    self.current_msg_id = v if v else None
                except Exception:
                    pass
            elif line.startswith("MSGS:"):
                ids_part = line[5:].strip()
                if ids_part:
                    try:
                        self.backup_msg_ids = [
                            int(x) for x in ids_part.split(",") if x.strip().isdigit()
                        ]
                    except Exception:
                        pass

    async def _build_index_text(self):
        counter = db.get_setting("global_url_counter") or "0"
        current = self.current_msg_id or 0
        msgs_str = ",".join(str(x) for x in self.backup_msg_ids)
        return (
            f"{self.INDEX_HEADER}\n"
            f"COUNTER:{counter}\n"
            f"CURRENT:{current}\n"
            f"MSGS:{msgs_str}"
        )

    async def _create_index(self):
        text = await self._build_index_text()
        msg = await self.bot.send_message(self.channel_id, text)
        self.index_msg_id = msg.message_id
        try:
            await self.bot.pin_chat_message(
                self.channel_id, msg.message_id, disable_notification=True
            )
        except Exception as e:
            logger.warning(f"URLBackup: could not pin index: {e}")

    async def _update_index(self):
        if not self.index_msg_id:
            return
        try:
            text = await self._build_index_text()
            await self.bot.edit_message_text(
                chat_id=self.channel_id,
                message_id=self.index_msg_id,
                text=text,
            )
        except Exception as e:
            logger.error(f"URLBackup: error updating index: {e}")

    async def _get_message_text_via_telethon(self, msg_id):
        """
        ★ Telethon দিয়ে সরাসরি channel message text পড়ো।
        Owner-কে forward করতে হয় না — API call কম, RAM কম।
        """
        if not self.telethon_client:
            return None
        try:
            msg = await self.telethon_client.get_messages(self.channel_id, ids=msg_id)
            return msg.text if msg else None
        except Exception as e:
            logger.error(f"URLBackup: telethon get_messages error: {e}")
            return None

    async def _restore_urls_to_db(self):
        """
        ★ RAM-সাশ্রয়ী restore:
        - একটা একটা message process করে
        - Text কখনো একসাথে জমে না
        - INSERT OR IGNORE দিয়ে সরাসরি SQLite-এ
        """
        if not self.backup_msg_ids:
            logger.info("URLBackup: no batches to restore.")
            return

        restored = 0

        for msg_id in self.backup_msg_ids:
            text = None

            # ★ Telethon দিয়ে সরাসরি পড়ো (forward → owner → delete করতে হয় না)
            if self.telethon_client:
                text = await self._get_message_text_via_telethon(msg_id)

            # Fallback: পুরনো পদ্ধতি (owner-কে forward)
            if not text and OWNER_IDS:
                try:
                    owner_id = OWNER_IDS[0]
                    fwd = await self.bot.forward_message(
                        chat_id=owner_id,
                        from_chat_id=self.channel_id,
                        message_id=msg_id,
                    )
                    text = fwd.text or fwd.caption or ""
                    try:
                        await self.bot.delete_message(chat_id=owner_id, message_id=fwd.message_id)
                    except Exception:
                        pass
                except Exception as e:
                    logger.error(f"URLBackup: forward fallback error for msg {msg_id}: {e}")

            if not text:
                continue

            # current batch-এর length মনে রাখো (full text নয়)
            if msg_id == self.current_msg_id:
                self.current_msg_len = len(text)

            # ★ Line by line process — পুরো text কখনো একসাথে memory-তে বেশিক্ষণ থাকে না
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("📦"):
                    continue
                url = line.split(". ", 1)[1].strip() if ". " in line else line
                if not url.startswith("http"):
                    continue
                with db.get_conn() as conn:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO urls "
                            "(url, discovered_at, sent_to_bot2, global_counter, is_group) "
                            "VALUES (?, ?, 1, 0, 1)",
                            (url, datetime.now()),
                        )
                        conn.commit()
                        restored += 1
                    except Exception:
                        pass

            # ★ text variable explicitly মুছে দাও — GC-কে সাহায্য করো
            del text
            await asyncio.sleep(0.3)

        logger.info(f"URLBackup: restored {restored} URLs from {len(self.backup_msg_ids)} batch(es).")

    async def backup_url(self, url, counter):
        """
        ★ RAM-সাশ্রয়ী backup_url:
        - current_msg_text RAM-এ রাখা হয় না
        - নতুন URL লাগলে Telethon দিয়ে current text fetch → append → edit
        - মাত্র length track করা হয়
        """
        if not self.initialized:
            return

        url_line = f"{counter}. {url}\n"

        try:
            needs_new_batch = (
                not self.current_msg_id or
                self.current_msg_len + len(url_line) > URL_BATCH_MAX_CHARS
            )

            if needs_new_batch:
                header = f"📦 URL Batch\n{url_line}"
                msg = await self.bot.send_message(self.channel_id, header)
                self.current_msg_id = msg.message_id
                self.current_msg_len = len(header)
                self.backup_msg_ids.append(self.current_msg_id)
                await self._update_index()
                logger.info(f"URLBackup: new batch msg {self.current_msg_id}")
            else:
                # ★ Current text Telethon দিয়ে fetch করো (RAM-এ রাখা নেই)
                current_text = await self._get_message_text_via_telethon(self.current_msg_id)

                if current_text is None:
                    # Telethon না থাকলে নতুন batch তৈরি করো
                    header = f"📦 URL Batch\n{url_line}"
                    msg = await self.bot.send_message(self.channel_id, header)
                    self.current_msg_id = msg.message_id
                    self.current_msg_len = len(header)
                    self.backup_msg_ids.append(self.current_msg_id)
                    await self._update_index()
                    return

                new_text = current_text + url_line
                await self.bot.edit_message_text(
                    chat_id=self.channel_id,
                    message_id=self.current_msg_id,
                    text=new_text,
                )
                self.current_msg_len = len(new_text)
                del new_text, current_text  # RAM মুক্ত করো

                if counter % 10 == 0:
                    await self._update_index()

        except Exception as e:
            logger.error(f"URLBackup: error backing up URL: {e}")


# ============================================================================
# GLOBAL SINGLETONS
# ============================================================================

db = Database()
master_config = MasterConfigManager()
url_backup = URLBackupManager()

# ============================================================================
# UTILITIES
# ============================================================================

def check_memory():
    try:
        mem = psutil.virtual_memory()
        if mem.percent > 70:
            gc.collect()
            logger.warning(f"Memory: {mem.percent}% — gc triggered")
        if mem.percent > 85:
            for gen in range(3):
                gc.collect(gen)
            logger.warning(f"Memory critical: {mem.percent}% — full gc done")
    except Exception:
        pass

_TELEGRAM_PUBLIC_RE = re.compile(
    r'^https?://(?:www\.)?(?:t|telegram)\.me/([a-zA-Z][a-zA-Z0-9_]{3,})/?$',
    re.IGNORECASE
)
_TELEGRAM_PRIVATE_RE = re.compile(
    r'^https?://(?:www\.)?(?:t|telegram)\.me/(?:\+|joinchat/)[a-zA-Z0-9_-]+/?$',
    re.IGNORECASE
)

# ★ _join_cache: সর্বোচ্চ ১০০০ entry — অসীম বাড়বে না
_join_cache = {}
_JOIN_CACHE_TTL = 300
_JOIN_CACHE_MAX = 1000


def get_check_bot_token():
    if CHECKER_BOT_TOKEN and ":" in CHECKER_BOT_TOKEN and CHECKER_BOT_TOKEN != "1":
        return CHECKER_BOT_TOKEN
    for token in BOT_TOKENS:
        if token and ":" in token and token != "1":
            return token
    return None


async def resolve_required_chat():
    if REQUIRED_CHANNEL_ID:
        return REQUIRED_CHANNEL_ID
    cached = db.get_setting("required_channel_id")
    if cached:
        try:
            return int(cached)
        except ValueError:
            if cached.startswith("@"):
                return cached
    raw = (REQUIRED_CHANNEL or "").strip()
    if not raw:
        return None
    try:
        cid = int(raw)
        db.set_setting("required_channel_id", str(cid))
        return cid
    except ValueError:
        pass
    username = None
    if raw.startswith("@") and len(raw) > 1:
        username = raw
    elif _TELEGRAM_PUBLIC_RE.match(raw) and not _TELEGRAM_PRIVATE_RE.match(raw):
        m = _TELEGRAM_PUBLIC_RE.match(raw)
        if m:
            username = "@" + m.group(1)
    if username:
        from telegram import Bot
        token = get_check_bot_token()
        if token:
            try:
                checker = Bot(token=token)
                chat_info = await checker.get_chat(username)
                db.set_setting("required_channel_id", str(chat_info.id))
                return chat_info.id
            except Exception as e:
                logger.warning(f"Resolve {username} failed ({e})")
        return username
    return None


async def get_channel_id():
    return await resolve_required_chat()


async def check_force_join(bot, user_id, use_cache=True):
    if user_id in OWNER_IDS:
        return True
    if use_cache:
        ts = _join_cache.get(user_id)
        if ts and (datetime.now() - ts).total_seconds() < _JOIN_CACHE_TTL:
            return True
    chat_to_check = await resolve_required_chat()
    if not chat_to_check:
        return False
    from telegram import Bot
    token = get_check_bot_token()
    if token:
        try:
            checker_bot = Bot(token=token)
            member = await checker_bot.get_chat_member(chat_id=chat_to_check, user_id=user_id)
            if member.status in ["member", "administrator", "creator"]:
                # ★ Cache সীমা মানলে তবেই যোগ করো
                if len(_join_cache) < _JOIN_CACHE_MAX:
                    _join_cache[user_id] = datetime.now()
                return True
            return False
        except Exception as e:
            logger.error(f"Checker bot get_chat_member failed: {e}")
    try:
        member = await bot.get_chat_member(chat_id=chat_to_check, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            if len(_join_cache) < _JOIN_CACHE_MAX:
                _join_cache[user_id] = datetime.now()
            return True
    except Exception as e:
        logger.error(f"Fallback get_chat_member failed: {e}")
    return False


def get_join_keyboard():
    keyboard = [
        [InlineKeyboardButton("Join Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("Joined ✅ (Check)", callback_data="check_join")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ============================================================================
# HANDLERS
# ============================================================================

async def start_command(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id in OWNER_IDS:
        bot_id = c.bot.id
        if bot_id == CHECKER_BOT_ID:
            await u.message.reply_text(
                "👋 Checker Bot চালু আছে।\n\n"
                "🔗 Duplicate URL detect করে admin-দের পাঠানো\n"
                "✅ Force-join check করা\n\n"
                "📊 /stats - পরিসংখ্যান দেখুন"
            )
        else:
            await u.message.reply_text(
                "👋 কন্ট্রোল প্যানেল চালু হয়েছে।\n\n"
                "🚀 /AutoMessage - অটো মেসেজ সেট করুন\n"
                "🛑 /cancelAutomessage - অটো মেসেজ বন্ধ করুন\n"
                "📊 /stats - পরিসংখ্যান দেখুন"
            )
    else:
        if await check_force_join(c.bot, u.effective_user.id):
            await u.message.reply_text("স্বাগতম! আপনি এখন বট ব্যবহার করতে পারেন।")
        else:
            await u.message.reply_text(
                f"বটটি ব্যবহার করতে হলে আপনাকে আমাদের চ্যানেলে জয়েন করতে হবে।\n\n"
                f"চ্যানেল: {REQUIRED_CHANNEL}\n\nজয়েন করার পর নিচের বাটনে ক্লিক করুন।",
                reply_markup=get_join_keyboard()
            )

async def handle_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query
    if query.data != "check_join":
        await query.answer()
        return
    user_id = u.effective_user.id
    if await check_force_join(c.bot, user_id, use_cache=False):
        if len(_join_cache) < _JOIN_CACHE_MAX:
            _join_cache[user_id] = datetime.now()
        await query.answer("✅ ধন্যবাদ!")
        try:
            await query.edit_message_text("🎉 আপনি সফলভাবে জয়েন করেছেন।")
        except Exception:
            pass
        return
    await query.answer("যাচাই করছি...")
    await asyncio.sleep(2)
    if await check_force_join(c.bot, user_id, use_cache=False):
        if len(_join_cache) < _JOIN_CACHE_MAX:
            _join_cache[user_id] = datetime.now()
        try:
            await query.edit_message_text("🎉 আপনি সফলভাবে জয়েন করেছেন।")
        except Exception:
            pass
        return
    await query.message.reply_text(
        "❌ আপনি এখনো জয়েন হননি। চ্যানেলে join করুন, কিছুক্ষণ পর আবার চেক করুন।"
    )

async def config_status_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id in OWNER_IDS:
        if master_config.initialized and master_config.config_message:
            await u.message.reply_text(
                f"📋 **মাস্টার কনফিগারেশন**\n\n"
                f"📌 **মেসেজ আইডি:** `{master_config.config_message_id}`"
            )
        else:
            await u.message.reply_text("❌ মাস্টার কনফিগ পাওয়া যায়নি।")

async def track_chats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    result = u.my_chat_member
    chat = result.chat
    new_status = result.new_chat_member.status
    if new_status in ["member", "administrator"]:
        if chat.type in ["group", "supergroup"]:
            db.add_group(c.bot.id, chat.id)
        elif chat.type == "channel" and new_status == "administrator":
            cached = db.get_setting("required_channel_id")
            if not cached:
                db.set_setting("required_channel_id", str(chat.id))

async def track_channel_post(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.channel_post
    if not msg:
        return
    if msg.chat.type != "channel":
        return
    if db.get_setting("required_channel_id"):
        return
    db.set_setting("required_channel_id", str(msg.chat.id))

async def set_channel_id_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id not in OWNER_IDS:
        return
    parts = (u.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        cur = db.get_setting("required_channel_id") or "সেট করা হয়নি"
        await u.message.reply_text(f"📌 বর্তমান Channel ID: `{cur}`\n\nব্যবহার: `/setchannelid -1001234567890`")
        return
    val = parts[1].strip()
    try:
        cid = int(val)
        db.set_setting("required_channel_id", str(cid))
        _join_cache.clear()
        await u.message.reply_text(f"✅ Channel ID সেট: `{cid}`")
    except ValueError:
        if val.startswith("@"):
            db.set_setting("required_channel_id", val)
            _join_cache.clear()
            await u.message.reply_text(f"✅ Username সেট: `{val}`")
        else:
            await u.message.reply_text("❌ সংখ্যা বা @username দিন।")

async def handle_all_checker(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message:
        return
    chat = u.message.chat
    user = u.effective_user
    bot_id = c.bot.id
    if chat.type in [Chat.GROUP, Chat.SUPERGROUP]:
        db.add_group(bot_id, chat.id)
        urls = re.findall(r'(https?://\S+)', u.message.text or "")
        for url in urls:
            await db.save_url(url)
    elif chat.type == Chat.PRIVATE:
        if user.id not in OWNER_IDS:
            if not await check_force_join(c.bot, user.id):
                await u.message.reply_text(
                    f"বটটি ব্যবহার করতে চ্যানেলে জয়েন করুন।\n\n{REQUIRED_CHANNEL}",
                    reply_markup=get_join_keyboard()
                )

async def handle_all(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message:
        return
    chat = u.message.chat
    user = u.effective_user
    bot_id = c.bot.id
    if chat.type in [Chat.GROUP, Chat.SUPERGROUP]:
        db.add_group(bot_id, chat.id)
        if u.message.reply_to_message and u.message.reply_to_message.from_user.id == bot_id:
            if db.can_reply_in_group(user.id):
                await u.message.reply_text("কাজ করলে inbox করুন")
            return
        urls = re.findall(r'(https?://\S+)', u.message.text or "")
        for url in urls:
            await db.save_url(url)
    elif chat.type == Chat.PRIVATE:
        if user.id not in OWNER_IDS:
            if not await check_force_join(c.bot, user.id):
                await u.message.reply_text(
                    f"বটটি ব্যবহার করতে চ্যানেলে জয়েন করুন।\n\n{REQUIRED_CHANNEL}",
                    reply_markup=get_join_keyboard()
                )
                return
        if user.id in OWNER_IDS:
            if c.user_data.get("awaiting_interval"):
                try:
                    interval = int(u.message.text)
                    c.user_data["interval"] = interval
                    c.user_data["awaiting_interval"] = False
                    c.user_data["awaiting_msg"] = True
                    await u.message.reply_text("✅ সেট হয়েছে। মেসেজটি forward করুন।")
                except Exception:
                    await u.message.reply_text("❌ শুধু সংখ্যা দিন।")
                return
            if c.user_data.get("awaiting_msg"):
                msg_id = u.message.message_id
                interval = c.user_data["interval"]
                db.set_auto_msg(bot_id, interval, msg_id)
                await master_config.update_bot_config(bot_id, interval, msg_id)
                c.user_data["awaiting_msg"] = False
                await u.message.reply_text("🚀 অটো মেসেজ সেট হয়েছে!")
                return
            if u.message.reply_to_message:
                info = db.get_reply_info(u.message.reply_to_message.message_id)
                if info:
                    orig_chat, orig_msg, b_id = info
                    try:
                        await c.bot.copy_message(
                            chat_id=orig_chat,
                            from_chat_id=user.id,
                            message_id=u.message.message_id,
                            reply_to_message_id=orig_msg
                        )
                        await u.message.reply_text("✅ উত্তর পাঠানো হয়েছে।")
                        return
                    except Exception as e:
                        await u.message.reply_text(f"❌ পাঠানো যায়নি: {e}")
                        return
            if not u.message.text or not u.message.text.startswith("/"):
                groups_data = db.get_groups(bot_id)
                success, fail = 0, 0
                for gid, _ in groups_data:
                    try:
                        await u.message.copy(gid)
                        success += 1
                        await asyncio.sleep(0.05)
                    except Exception:
                        fail += 1
                await u.message.reply_text(
                    f"📊 ব্রডকাস্ট:\n✅ সফল: {success}\n❌ ব্যর্থ: {fail}"
                )
        else:
            if OWNER_IDS:
                try:
                    admin_msg = await c.bot.forward_message(
                        OWNER_IDS[0], chat.id, u.message.message_id
                    )
                    db.save_reply_map(
                        admin_msg.message_id, chat.id, u.message.message_id, bot_id
                    )
                except Exception as e:
                    logger.error(f"Forward to owner failed: {e}")

async def auto_msg_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id in OWNER_IDS:
        c.user_data["awaiting_interval"] = True
        await u.message.reply_text("🕒 কত মিনিট পর পর পাঠাতে চান?")

async def cancel_auto_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id in OWNER_IDS:
        db.delete_auto_msg(c.bot.id)
        await master_config.remove_bot_config(c.bot.id)
        await u.message.reply_text("🛑 অটো মেসেজ বন্ধ করা হয়েছে।")

async def stats_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id in OWNER_IDS:
        gids = db.get_groups(c.bot.id)
        total_urls = db.get_setting("global_url_counter")
        with db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM urls WHERE sent_to_bot2 = 1")
            sent_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM urls WHERE sent_to_bot2 = 0 AND is_group = 1")
            pending_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT interval_min FROM auto_messages WHERE bot_id = ?", (c.bot.id,)
            )
            auto_config = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM url_type_cache")
            cached_types = cursor.fetchone()[0]
        bot_id = c.bot.id
        if bot_id == CHECKER_BOT_ID:
            auto_status = "🚫 এই বটে auto-message নেই"
            backup_info = f"\n💾 **URL Backup:** {len(url_backup.backup_msg_ids)} batch(es)"
        else:
            auto_status = (
                "❌ বন্ধ" if not auto_config
                else f"✅ চালু (প্রতি {auto_config[0]} মিনিট)"
            )
            backup_info = ""
        try:
            ram_pct = psutil.virtual_memory().percent
            ram_mb = round(psutil.virtual_memory().used / 1024 / 1024, 1)
        except Exception:
            ram_pct = ram_mb = "?"
        await u.message.reply_text(
            f"📊 **পরিসংখ্যান**\n\n"
            f"👥 **গ্রুপ:** {len(gids)}\n"
            f"🔗 **মোট URL:** {total_urls or 0}\n"
            f"✅ **পাঠানো:** {sent_count}\n"
            f"⏳ **পেন্ডিং:** {pending_count}\n"
            f"📋 **Type Cache (SQLite):** {cached_types}\n"
            f"🤖 **অটো:** {auto_status}\n"
            f"🧠 **RAM:** {ram_pct}% ({ram_mb}MB)"
            f"{backup_info}"
        )

# ============================================================================
# SCHEDULER
# ============================================================================

async def scheduler(apps):
    target_bot_id = CHECKER_BOT_ID
    if not target_bot_id:
        valid_tokens = [t for t in BOT_TOKENS if t and t != "1" and ":" in t]
        if valid_tokens:
            try:
                target_bot_id = int(valid_tokens[0].split(":")[0])
            except Exception:
                target_bot_id = None

    while True:
        check_memory()
        try:
            if target_bot_id and target_bot_id in apps:
                urls_with_counter = db.get_unsent_urls_for_sending(limit=10)
                if urls_with_counter:
                    formatted_urls = [f"{cnt}. {url}" for url, cnt in urls_with_counter]
                    urls_to_mark_sent = [url for url, _ in urls_with_counter]
                    url_text = "\n".join(formatted_urls)
                    for admin_id in OWNER_IDS:
                        try:
                            await apps[target_bot_id].bot.send_message(
                                admin_id,
                                f"🔗 **নতুন গ্রুপ URL (১০টি):**\n\n{url_text}"
                            )
                        except Exception as e:
                            logger.error(f"Failed to send to {admin_id}: {e}")
                    db.mark_urls_as_sent(urls_to_mark_sent)

            tasks = db.get_auto_msgs()
            for bot_id, interval, last_sent, msg_id in tasks:
                try:
                    if bot_id == CHECKER_BOT_ID:
                        continue
                    last_sent_dt = (
                        datetime.strptime(last_sent, "%Y-%m-%d %H:%M:%S.%f")
                        if isinstance(last_sent, str) else last_sent
                    )
                    if (datetime.now() - last_sent_dt).total_seconds() / 60 >= interval:
                        if bot_id in apps:
                            groups_data = db.get_groups(bot_id)
                            sent_count = 0
                            for gid, last_active in groups_data:
                                last_active_dt = (
                                    datetime.strptime(last_active, "%Y-%m-%d %H:%M:%S.%f")
                                    if isinstance(last_active, str) else last_active
                                )
                                if (datetime.now() - last_active_dt).total_seconds() / 60 <= interval:
                                    try:
                                        await apps[bot_id].bot.copy_message(
                                            gid, OWNER_IDS[0], msg_id
                                        )
                                        sent_count += 1
                                        await asyncio.sleep(0.05)
                                    except Exception:
                                        pass
                            db.update_last_sent(bot_id)
                            if sent_count > 0:
                                for admin_id in OWNER_IDS:
                                    try:
                                        await apps[bot_id].bot.send_message(
                                            admin_id,
                                            f"📢 **অটো মেসেজ** (Bot {bot_id}): {sent_count}টি গ্রুপে পাঠানো।"
                                        )
                                    except Exception:
                                        pass
                except Exception as e:
                    logger.error(f"Auto message error: {e}")

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        # ★ _join_cache পরিষ্কার — expired entries মুছো
        try:
            now = datetime.now()
            expired = [uid for uid, ts in list(_join_cache.items())
                       if (now - ts).total_seconds() > _JOIN_CACHE_TTL * 2]
            for uid in expired:
                _join_cache.pop(uid, None)
        except Exception:
            pass

        await asyncio.sleep(30)

# ============================================================================
# BOT RUNNERS
# ============================================================================

async def run_checker_bot(token, apps_dict):
    if not token or token == "1" or ":" not in token:
        return
    try:
        bot_app = ApplicationBuilder().token(token).build()
        bot_id = int(token.split(":")[0])
        apps_dict[bot_id] = bot_app
        bot_app.add_handler(CommandHandler("start", start_command))
        bot_app.add_handler(CommandHandler("stats", stats_cmd))
        bot_app.add_handler(CommandHandler("config", config_status_cmd))
        bot_app.add_handler(CommandHandler("setchannelid", set_channel_id_cmd))
        bot_app.add_handler(CallbackQueryHandler(handle_callback))
        bot_app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, track_channel_post))
        bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_all_checker))
        bot_app.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
        await bot_app.initialize()
        await bot_app.start()
        if bot_app.updater:
            await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info(f"Checker Bot {bot_id} started.")
    except Exception as e:
        logger.error(f"Error starting checker bot: {e}")

async def run_bot(token, apps_dict):
    if not token or token == "1" or ":" not in token:
        return
    try:
        bot_app = ApplicationBuilder().token(token).build()
        bot_id = int(token.split(":")[0])
        apps_dict[bot_id] = bot_app
        bot_app.add_handler(CommandHandler("start", start_command))
        bot_app.add_handler(CommandHandler("AutoMessage", auto_msg_cmd))
        bot_app.add_handler(CommandHandler("cancelAutomessage", cancel_auto_cmd))
        bot_app.add_handler(CommandHandler("stats", stats_cmd))
        bot_app.add_handler(CommandHandler("config", config_status_cmd))
        bot_app.add_handler(CommandHandler("setchannelid", set_channel_id_cmd))
        bot_app.add_handler(CallbackQueryHandler(handle_callback))
        bot_app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, track_channel_post))
        bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_all))
        bot_app.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
        await bot_app.initialize()
        await bot_app.start()
        if bot_app.updater:
            await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info(f"Bot {bot_id} started.")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")

# ============================================================================
# MAIN
# ============================================================================

async def main():
    try:
        web_thread = Thread(target=run_web, daemon=True)
        web_thread.start()

        await master_config.initialize()
        await master_config.restore_all_auto_messages_from_config()

        apps = {}

        if CHECKER_BOT_TOKEN and ":" in CHECKER_BOT_TOKEN and CHECKER_BOT_TOKEN != "1":
            await run_checker_bot(CHECKER_BOT_TOKEN, apps)

        regular_tokens = [t for t in BOT_TOKENS if t and t != "1" and ":" in t]
        if regular_tokens:
            await asyncio.gather(*[run_bot(token, apps) for token in regular_tokens])

        if not apps:
            logger.error("No valid bot tokens found!")
            return

        # ★ Telethon client share করো — আলাদা connection লাগবে না
        if CHECKER_BOT_ID and CHECKER_BOT_ID in apps:
            checker_bot_instance = apps[CHECKER_BOT_ID].bot
            await url_backup.initialize(
                checker_bot_instance,
                telethon_client=master_config.client  # ★ shared Telethon
            )
        else:
            logger.warning("URLBackup: checker bot not found, backup disabled.")

        asyncio.create_task(scheduler(apps))

        while True:
            await asyncio.sleep(300)
            check_memory()

    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        raise
    finally:
        await close_http_session()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
