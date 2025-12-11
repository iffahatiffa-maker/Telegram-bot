# bot.py
import os
import time
import logging
import asyncio
import json
from datetime import datetime, timedelta

from dotenv import load_dotenv
from redis.asyncio import from_url as redis_from_url
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

load_dotenv()

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")  # numeric -100... or @groupusername
REDIS_URL = os.getenv("REDIS_URL")  # redis://...
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")  # e.g. https://<app>.up.railway.app
PORT = int(os.getenv("PORT", "8080"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@privep2p")
WEEK_SECONDS = 7 * 24 * 3600

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable required")

if not GROUP_ID:
    raise RuntimeError("GROUP_ID environment variable required")

if not REDIS_URL:
    raise RuntimeError("REDIS_URL environment variable required")

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Redis client (async) ---
redis = redis_from_url(REDIS_URL, decode_responses=True)

# keys usage:
# inv_count:{user_id} -> integer (weekly counter, TTL ~7 days)
# invite_meta:{invite_link} -> json with user_id, created_at, expire_ts

# --- Helpers ---
async def can_send_invite(user_id: int) -> (bool, str):
    """
    Check if user can request an invite.
    Returns (allowed, reason_message_if_not_allowed)
    """
    key = f"inv_count:{user_id}"
    count = await redis.get(key)
    count = int(count) if count else 0
    if count >= 2:
        return False, (
            f"Limit reached — tumne is week me already {count} invites liye.\n"
            f"Try next week, ya contact admin {ADMIN_USERNAME}."
        )
    return True, ""

async def record_invite(user_id: int, invite_link: str, expire_ts: int):
    key = f"inv_count:{user_id}"
    # INCR and set expiry to 7 days if new
    new = await redis.incr(key)
    ttl = await redis.ttl(key)
    if ttl == -1:
        await redis.expire(key, WEEK_SECONDS)
    # store invite metadata
    meta = {"user_id": user_id, "invite_link": invite_link, "created_at": int(time.time()), "expire_ts": expire_ts}
    await redis.set(f"invite_meta:{invite_link}", json.dumps(meta), ex=3600 + 60)  # keep a bit longer than 1 hour

# --- Handlers ---
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.effective_chat.type
    # We want people to request invite if they are not members of the group
    # Try to check membership using get_chat_member - might raise if bot not in group/permissions
    is_member = False
    try:
        # If GROUP_ID is like "@mygroup" it's okay; else numeric
        member = await context.bot.get_chat_member(GROUP_ID, user.id)
        status = member.status  # 'member', 'creator', 'administrator', 'left', 'kicked'
        if status in ("member", "creator", "administrator"):
            is_member = True
    except Exception as e:
        # If bot can't check, assume not member
        logger.debug("Failed to fetch chat member: %s", e)
        is_member = False

    if is_member:
        await update.message.reply_text(
            f"Hello {user.first_name}! You're already a member of the group. Use the bot features from inside the group."
        )
        return

    # Not a member -> send invite request button
    text = (
        "You are not a member of the group.\n"
        f"Contact admin {ADMIN_USERNAME} if urgent.\n\n"
        "Click below to request a one-time invite link (1 hour expiry, single use)."
    )
    kb = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton("Request Invite", callback_data="request_invite")
    )
    await update.message.reply_text(text, reply_markup=kb)

async def request_invite_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # acknowledge callback
    user = update.effective_user
    user_id = user.id

    allowed, reason = await can_send_invite(user_id)
    if not allowed:
        await query.edit_message_text(reason)
        return

    # Try to create invite link
    now = int(time.time())
    expire_ts = now + 3600  # 1 hour
    try:
        invite = await context.bot.create_chat_invite_link(chat_id=GROUP_ID, expire_date=expire_ts, member_limit=1)
        link = invite.invite_link
    except Exception as e:
        logger.exception("Failed creating invite: %s", e)
        await query.edit_message_text(
            f"Sorry, couldn't create invite link. Contact admin {ADMIN_USERNAME}.\nError: {e}"
        )
        return

    # record in redis
    await record_invite(user_id, link, expire_ts)

    # Send invite with join button
    kb = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton("Join Group", url=link)
    )
    sent_text = (
        "Here's your invite link — single use, expires in 1 hour.\n\n"
        "Click **Join Group** to join now."
    )
    await query.edit_message_text(sent_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def health_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is alive.")

# --- Main boot ---
def build_app():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("health", health_handler))
    app.add_handler(CallbackQueryHandler(request_invite_cb, pattern="^request_invite$"))
    return app

async def main():
    application = build_app()
    # set webhook if requested
    if USE_WEBHOOK:
        if not WEBHOOK_URL:
            raise RuntimeError("WEBHOOK_URL required when USE_WEBHOOK=1")
        webhook_path = f"/webhook/{BOT_TOKEN}"
        webhook_full = f"{WEBHOOK_URL}{webhook_path}"
        logger.info("Setting webhook to %s", webhook_full)
        # set webhook via bot
        await application.bot.set_webhook(webhook_full)
        # run webhook (this blocks)
        # Use run_webhook helper (it creates aiohttp app)
        await application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            path=webhook_path,
            webhook_url=webhook_full,
        )
    else:
        # polling mode (not recommended for production), but fallback
        logger.info("Starting polling (USE_WEBHOOK=0)")
        await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down")
