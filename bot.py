# p2p_bot.py
import os
import time
import logging
import aiosqlite
import asyncio
from uuid import uuid4
from datetime import datetime
from typing import Optional

from telegram import (
    __version__ as tgver,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
    ChatInviteLink,
)
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    ChatMemberHandler,
    filters,
)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
P2P_GROUP_ID = int(os.getenv("P2P_GROUP_ID", "0"))
RULES_URL = os.getenv("RULES_URL", "https://t.me/your_rules_post_here")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "privep2p")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

# Limits:
INVITE_EXPIRE_SECONDS = 3600        # 1 hour
INVITE_MEMBER_LIMIT = 1             # single use
WEEKLY_INVITE_LIMIT = 2             # per user
MUTE_SECONDS = 24 * 3600            # mute new joiners 24 hours
# ----------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# ---------- DB helpers ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS invite_counts (
                user_id INTEGER,
                year INTEGER,
                week INTEGER,
                count INTEGER,
                PRIMARY KEY (user_id, year, week)
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS used_invites (
                invite_name TEXT PRIMARY KEY,
                referrer_id INTEGER,
                created_at INTEGER
            );
            """
        )
        await db.commit()


def current_year_week():
    now = datetime.utcnow()
    year, week, _ = now.isocalendar()
    return year, week


async def get_invite_count(user_id: int) -> int:
    year, week = current_year_week()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT count FROM invite_counts WHERE user_id=? AND year=? AND week=?",
            (user_id, year, week),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def increment_invite_count(user_id: int):
    year, week = current_year_week()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO invite_counts(user_id, year, week, count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, year, week) DO UPDATE SET count = count + 1;
            """,
            (user_id, year, week),
        )
        await db.commit()


async def register_invite_name(invite_name: str, referrer_id: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO used_invites(invite_name, referrer_id, created_at) VALUES (?, ?, ?)",
            (invite_name, referrer_id or 0, int(time.time())),
        )
        await db.commit()


async def get_referrer_by_invite(invite_name: str) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT referrer_id FROM used_invites WHERE invite_name=?",
            (invite_name,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return row[0] if row[0] != 0 else None


# ---------- Bot handlers ----------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start in private chat:
    - If user is a member of P2P_GROUP: allow generating an invite link (subject to weekly limit)
    - Else: show not-member message w/ admin contact
    """
    if update.effective_chat.type != "private":
        return  # we only process private /start
    user = update.effective_user
    app = context.application

    try:
        member = await app.bot.get_chat_member(chat_id=P2P_GROUP_ID, user_id=user.id)
        status = member.status
    except Exception as e:
        logger.exception("Failed to get_chat_member")
        await update.message.reply_text(
            "Error checking group membership. Please try later or contact admin."
        )
        return

    if status not in ("member", "creator", "administrator"):
        # NOT a member
        text = (
            "❌ You are not a member of the group.\n\n"
            f"Please contact admin @{ADMIN_USERNAME} to get access."
        )
        await update.message.reply_text(text)
        return

    # Member: check weekly limit
    count = await get_invite_count(user.id)
    if count >= WEEKLY_INVITE_LIMIT:
        await update.message.reply_text(
            f"⚠️ You have reached the weekly invite limit ({WEEKLY_INVITE_LIMIT}). Try next week."
        )
        return

    # Generate invite link: we'll put the ref info in the invite name so we can attribute later:
    token = uuid4().hex[:8]
    invite_name = f"ref:{user.id}:{token}"

    expire_date = int(time.time() + INVITE_EXPIRE_SECONDS)

    try:
        link: ChatInviteLink = await app.bot.create_chat_invite_link(
            chat_id=P2P_GROUP_ID,
            expire_date=expire_date,
            member_limit=INVITE_MEMBER_LIMIT,
            name=invite_name,
        )
    except Exception as e:
        logger.exception("create_chat_invite_link failed")
        await update.message.reply_text(
            "Failed to create invite link. Ensure the bot is admin and has permission to create invite links."
        )
        return

    # register invite in DB
    await register_invite_name(invite_name, user.id)
    await increment_invite_count(user.id)

    # Send DM with button(s)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="Join Group", url=link.invite_link)],
            [InlineKeyboardButton(text="P2P Process & Rules", url=RULES_URL)],
        ]
    )

    msg_text = (
        "🔗 Your Invite Link (1/1):\n"
        f"{link.invite_link}\n\n"
        "⏳ Expires after 1 hour or a single use.\n"
        "🔒 Refer only trusted contacts you know personally."
    )

    await update.message.reply_text(msg_text, reply_markup=keyboard)


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle user joining group via invite link.
    When a new member joins, Update.chat_member or Update.my_chat_member may have invite_link info.
    """
    app = context.application
    # We only care about the group's chat_member updates
    cmu = update.chat_member
    if not cmu:
        return

    # We want transitions where new_chat_member is a "member"
    old = cmu.old_chat_member.status
    new = cmu.new_chat_member.status
    user = cmu.new_chat_member.user

    # Only process joins
    if old in ("left", "kicked") and new in ("member", "administrator", "creator"):
        # Check invite_link if present
        invite_link_obj = cmu.invite_link  # may be None
        referrer_info = None

        if invite_link_obj and invite_link_obj.name:
            # Expecting name like "ref:{referrer_id}:{token}"
            name = invite_link_obj.name
            if name.startswith("ref:"):
                try:
                    parts = name.split(":")
                    referrer_id = int(parts[1])
                    referrer_info = referrer_id
                except Exception:
                    referrer_info = None

        # Mute the new user for 24 hours (requires bot to be admin & can_restrict_members)
        try:
            until = int(time.time() + MUTE_SECONDS)
            await app.bot.restrict_chat_member(
                chat_id=cmu.chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
        except Exception as e:
            logger.warning("Failed to restrict new member (bot may lack rights): %s", e)

        # Build the announcement message
        if referrer_info:
            # try to get username for referrer
            try:
                ref_user = await app.bot.get_chat(chat_id=referrer_info)
                ref_username = getattr(ref_user, "username", None)
                if ref_username:
                    referred_by_text = f"Referred by: @{ref_username} [{referrer_info}]"
                else:
                    referred_by_text = f"Referred by: {referrer_info}"
            except Exception:
                referred_by_text = f"Referred by: {referrer_info}"
        else:
            referred_by_text = "Referred by: Unknown"

        announce = (
            f"👋 Welcome {user.mention_html()}!\n\n"
            f"🔗 {referred_by_text}\n\n"
            "⏳ You can send messages after 24 hours.\n\n"
            f"📘 Read the P2P Process & Rules: {RULES_URL}"
        )

        try:
            await app.bot.send_message(
                chat_id=cmu.chat.id,
                text=announce,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Failed to send welcome message in group")

        # Record referral usage (in case you want to track)
        if invite_link_obj and invite_link_obj.name:
            # mark invite_name used (already registered on creation, but we could note usage)
            # optionally you can increment some used_count etc.
            pass


# ---------- Startup ----------
async def main():
    if not BOT_TOKEN or not P2P_GROUP_ID:
        raise RuntimeError("BOT_TOKEN and P2P_GROUP_ID must be set in environment")

    await init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    # chat member updates (for member joins/leaves)
    application.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    logger.info("Bot starting ...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()  # for railway use polling; alternatively set webhook on deployment
    # keep running
    await application.updater.wait_until_finished()
    await application.stop()
    await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
