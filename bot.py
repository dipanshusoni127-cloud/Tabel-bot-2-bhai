"""
Ludo Table Matchmaking Bot
--------------------------
Flow:
1. Player posts a message like: "10k full +500 no iphone"
2. Another player replies with a join-word (lga, lgao, l, aao, ll, t, aaja...)
3. Bot sends a Confirm/Reject button to the ADMIN (via DM) with both usernames + original text
4. Admin taps Confirm -> Bot posts the formatted table in the group
   Admin taps Reject -> Bot does nothing further (silently discarded)

You (admin) still handle balance checks manually before hitting Confirm.
"""

import os
import re
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============ CONFIG — set these as Railway "Variables", not hardcoded here ============
BOT_TOKEN = os.environ["BOT_TOKEN"]              # from @BotFather
ADMIN_ID = int(os.environ["ADMIN_ID"])           # your numeric Telegram user ID (not username)
GROUP_ID = os.environ.get("GROUP_ID")            # optional: restrict bot to one group's chat id
GROUP_ID = int(GROUP_ID) if GROUP_ID else None

# Words that count as "I want to join this table"
JOIN_WORDS = {"lga", "lgao", "l", "aao", "ll", "t", "aaja", "aja"}

# Pattern to detect a table-request message, e.g. "10k full +500 no iphone", "2k full", "1k full no iphone"
TABLE_PATTERN = re.compile(r"\b\d+k\b.*\bfull\b", re.IGNORECASE)
# ================================================

# In-memory store: {original_message_id: {"text":..., "poster": ..., "joiner": ..., "chat_id": ...}}
pending_requests = {}

# Matches lines like: "Aman Raj = -250", "Bhaisab  =  850", "Devil = 2000"
BALANCE_LINE_PATTERN = re.compile(r"^([A-Za-z][A-Za-z\s]*?)\s*=\s*(-?\d+)\s*$")


def is_table_request(text: str) -> bool:
    return bool(TABLE_PATTERN.search(text or ""))


def is_join_word(text: str) -> bool:
    cleaned = (text or "").strip().lower()
    return cleaned in JOIN_WORDS


def parse_balances(pinned_text: str) -> dict:
    """Parse the pinned message into {name_lowercase: balance}."""
    balances = {}
    for line in (pinned_text or "").splitlines():
        match = BALANCE_LINE_PATTERN.match(line.strip().lstrip("❤️").strip())
        if match:
            name = match.group(1).strip().lower()
            balance = int(match.group(2))
            balances[name] = balance
    return balances


def lookup_balance(balances: dict, first_name: str, username: str) -> str:
    """Try to find a balance entry matching this player's first name or username."""
    candidates = []
    if first_name:
        candidates.append(first_name.strip().lower())
    if username:
        candidates.append(username.strip().lower())

    for candidate in candidates:
        if candidate in balances:
            return str(balances[candidate])

    # fallback: partial match (e.g. "Aman Raj" vs telegram first name "Aman")
    for candidate in candidates:
        for name, bal in balances.items():
            if candidate and (candidate in name or name in candidate):
                return str(bal)

    return "Not found"


async def get_pinned_balances(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict:
    try:
        chat = await context.bot.get_chat(chat_id)
        pinned = chat.pinned_message
        if pinned and pinned.text:
            return parse_balances(pinned.text)
    except Exception as e:
        logger.warning(f"Could not fetch pinned message: {e}")
    return {}


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    chat_id = update.effective_chat.id
    if GROUP_ID is not None and chat_id != GROUP_ID:
        return

    # Case 1: someone posting a table request like "10k full +500 no iphone"
    if is_table_request(msg.text):
        pending_requests[msg.message_id] = {
            "text": msg.text,
            "poster_id": msg.from_user.id,
            "poster_name": msg.from_user.first_name,
            "poster_username": msg.from_user.username,
            "chat_id": chat_id,
        }
        return

    # Case 2: someone replying to a table request with a join word
    if msg.reply_to_message and is_join_word(msg.text):
        orig_id = msg.reply_to_message.message_id
        request = pending_requests.get(orig_id)
        if not request:
            return  # not a tracked table request

        joiner_id = msg.from_user.id
        joiner_name = msg.from_user.first_name
        joiner_username = msg.from_user.username

        poster_display = f"@{request['poster_username']}" if request["poster_username"] else request["poster_name"]
        joiner_display = f"@{joiner_username}" if joiner_username else joiner_name

        # Build confirm/reject buttons, encode ids in callback_data
        callback_key = f"{orig_id}:{msg.message_id}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{callback_key}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{callback_key}"),
            ]
        ])

        balances = await get_pinned_balances(context, chat_id)
        poster_balance = lookup_balance(balances, request["poster_name"], request["poster_username"])
        joiner_balance = lookup_balance(balances, joiner_name, joiner_username)

        text_for_admin = (
            "⚠️ New Match Request\n\n"
            f"Table: {request['text']}\n"
            f"Poster: {poster_display} (Balance: {poster_balance})\n"
            f"Joiner: {joiner_display} (Balance: {joiner_balance})\n\n"
            "Balance check karke Confirm ya Reject dabao."
        )

        # store extra info needed at confirm time
        pending_requests[orig_id]["joiner_id"] = joiner_id
        pending_requests[orig_id]["joiner_name"] = joiner_name
        pending_requests[orig_id]["joiner_username"] = joiner_username
        pending_requests[orig_id]["poster_display"] = poster_display
        pending_requests[orig_id]["joiner_display"] = joiner_display

        await context.bot.send_message(chat_id=ADMIN_ID, text=text_for_admin, reply_markup=keyboard)


async def handle_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("Sirf admin confirm kar sakta hai.", show_alert=True)
        return

    action, callback_key = query.data.split(":", 1)
    orig_id_str, _reply_id_str = callback_key.split(":")
    orig_id = int(orig_id_str)

    request = pending_requests.get(orig_id)
    if not request:
        await query.edit_message_text("Ye request ab valid nahi hai (expired ya already handled).")
        return

    if action == "reject":
        await query.edit_message_text(f"❌ Rejected:\n{request['text']}")
        pending_requests.pop(orig_id, None)
        return

    # action == confirm
    table_text = (
        "✅ TABLE CONFIRMED\n"
        f"{request['poster_display']} 🆚 {request['joiner_display']}\n"
        f"{request['text']}"
    )
    await context.bot.send_message(chat_id=request["chat_id"], text=table_text)
    await query.edit_message_text(f"✅ Confirmed & posted:\n{request['text']}")
    pending_requests.pop(orig_id, None)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))
    app.add_handler(CallbackQueryHandler(handle_admin_button))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
