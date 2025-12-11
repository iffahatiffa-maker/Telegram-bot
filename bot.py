#!/usr/bin/env python3
"""
bot.py - Telegram invite bot (python-telegram-bot v20 async)

Features:
- /start -> if not a member, shows "Request Invite" button
- Per-user rate limit: max N invites per 7 days (Redis ZSET window)
- createChatInviteLink with member_limit=1 and expire_date = now + INVITE_EXPIRE_SECONDS
- Stores invite metadata in Redis (and optional sqlite for audit)
- Webhook ready (USE_WEBHOOK=1). Polling fallback (USE_WEBHOOK=0) for local testing.
"""

import os
import time
import logging
import json
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

import redis.asyncio as aioredis
import aiosqlite

load_dotenv()

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
REDIS_URL = os.getenv("REDIS_URL")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")  # e.g. https://app.vercel.app
PORT = int(os.getenv("PORT", "8080"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@privep2p")
MAX_INVITES_PER_WEEK = int(os.getenv("MAX_INVITES_PER_WEEK", "2"))
INVITE_EXPIRE_SECONDS = int(os.getenv("INVITE_EXPIRE_SECONDS", "3600"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "invites.db")
# Redis keys: zset per user: invites:<user_id> with score = unix_ts

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env required")
if not GROUP_ID:
    raise RuntimeError("GROUP_ID env required")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL env required")

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------- redis client ----------------
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# ---------------- sqlite init (optional audit) ----------------
async def init_db(path: str):
    try:
        async with aiosqlite.connect(path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    invite_link TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expire_at INTEGER NOT NULL,
                    used BOOLEAN DEFAULT 0,
                    used_by INTEGER
                );
                """
            )
            await db.commit()
    except Exception:
        logger.exception("SQLite init failed")

# ---------------- helper functions ----------------
def redis_zkey_for_user(uid: int) -> str:
    return f"invites:{uid}"

async def cleanup_and_count_user(uid: int) -> int:
    """
    Remove entries older than 7 days and return current count.
    Using ZSET: member = timestamp string, score = timestamp
    """
    key = redis_zkey_for_user(uid)
    now = int(time.time())
    cutoff = now - 7 * 24 * 3600
    # remove old
    await redis.zremrangebyscore(key, 0, cutoff)
    count = await redis.zcard(key)
    return int(count)

async def add_invite_event(uid: int):
    key = redis_zkey_for_user(uid)
    now = int(time.time())
    # use score=now, member = now:<random> to ensure uniqueness
    member = f"{now}:{os.urandom(6).hex()}"
    await redis.zadd(key, {member: now})
    # ensure TTL so the set auto-expires if idle
    await redis.expire(key, 7 * 24 * 3600 + 60)

async def store_invite_meta(invite_link: str, uid: int, expire_ts: int):
    meta = {"user_id": uid, "invite_link": invite_link, "created_at": int(time.time()), "expire_ts": expire_ts}
    await redis.set(f"invite_meta:{invite_link}", json.dumps(meta), ex=INVITE_EXPIRE_SECONDS + 300)

async def save_invite_sql(uid: int, link: str, created_at: int, expire_at: int):
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "INSERT INTO invites (user_id, invite_link, created_at, expire_at) VALUES (?, ?, ?, ?)",
                (uid, link, created_at, expire_at),
            )
            await db.commit()
    except Exception:
        logger.exception("Failed to write to sqlite")

# ---------------- command handlers ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    uid = user.id
    # Check membership
    is_member = False
    try:
        m = await context.bot.get_chat_member(GROUP_ID, uid)
        status = getattr(m, "status", "")
        if status in ("member", "administrator", "creator"):
            is_member = True
    except TelegramError as e:
        # if bot lacks rights or can't fetch, assume not member
        logger.debug("get_chat_member failed: %s", e)

    if is_member:
        await update.message.reply_text(f"✅ Namaste {user.first_name}! You are already a member of the group.")
        return

    text = (
        "❗ You are not a member of the group.\n"
        f"Contact admin: {ADMIN_USERNAME}\n\n"
        "Click below to request a one-time invite link (1 hour expiry, single use)."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Request Invite 🔗", callback_data="request_invite")]])
    await update.message.reply_text(text, reply_markup=kb)

async def request_invite_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user = update.effective_user
    uid = user.id

    # rate-limit check
    try:
        count = await cleanup_and_count_user(uid)
    except Exception:
        logger.exception("Redis error")
        await query.edit_message_text("Server error (redis). Try again later.")
        return

    if count >= MAX_INVITES_PER_WEEK:
        await query.edit_message_text(
            f"⚠️ Tumne is hafte already {count} invites use kar liye hain. Agla invite next week try karo.\nContact admin: {ADMIN_USERNAME}"
        )
        return

    # create invite link
    now = int(time.time())
    expire_ts = now + INVITE_EXPIRE_SECONDS
    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            expire_date=expire_ts,
            member_limit=1,
            name=f"Invite for {uid}"
        )
        link = invite.invite_link
    except TelegramError as e:
        logger.exception("createChatInviteLink failed")
        await query.edit_message_text(f"❗ Bot cannot create invite (needs admin rights). Contact admin {ADMIN_USERNAME}.")
        return
    except Exception:
        logger.exception("createChatInviteLink unexpected")
        await query.edit_message_text(f"❗ Something went wrong creating invite. Contact admin {ADMIN_USERNAME}.")
        return

    # record
    await add_invite_event(uid)
    await store_invite_meta(link, uid, expire_ts)
    # optional sqlite audit
    try:
        await save_invite_sql(uid, link, now, expire_ts)
    except Exception:
        logger.debug("sqlite save skipped")

    # send invite
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Join Group ✅", url=link)]])
    sent_text = "🔗 Yeh tumhara invite link (single-use). Expire ho jayega 1 hour me.\n\nClick below to join now."
    await query.edit_message_text(sent_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start - request invite\n/status - remaining invites this week")

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    try:
        count = await cleanup_and_count_user(user.id)
        remain = max(0, MAX_INVITES_PER_WEEK - count)
        await update.message.reply_text(f"You have {remain} invites left this 7-day window.")
    except Exception:
        await update.message.reply_text("Error checking status.")

# ---------------- startup ----------------
def build_app():
    return ApplicationBuilder().token(BOT_TOKEN).build()

async def main():
    # init redis and sqlite
    try:
        await redis.ping()
    except Exception:
        logger.exception("Cannot connect to Redis - check REDIS_URL")
        raise

    await init_db(DATABASE_PATH)

    app = build_app()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CallbackQueryHandler(request_invite_cb, pattern="^request_invite$"))

    if USE_WEBHOOK:
        if not WEBHOOK_BASE_URL:
            raise RuntimeError("WEBHOOK_BASE_URL required when USE_WEBHOOK=1")
        webhook_path = f"/webhook/{BOT_TOKEN}"
        webhook_full = f"{WEBHOOK_BASE_URL}{webhook_path}"
        logger.info("Setting webhook to %s", webhook_full)
        # set webhook (bot must be able to reach this URL)
        await app.bot.set_webhook(webhook_full)
        # run webhook server
        await app.run_webhook(listen="0.0.0.0", port=PORT, webhook_path=webhook_path, webhook_url=webhook_full)
    else:
        logger.info("Starting polling (USE_WEBHOOK=0)")
        await app.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down")
