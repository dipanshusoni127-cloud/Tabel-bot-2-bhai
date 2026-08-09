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

# Words used to report a win by replying to the confirmed table message
WIN_WORDS = {"win", "won", "jeeta", "jeet", "jit", "w"}

COMMISSION_PERCENT = 5

# Pattern to detect a table-request message: a number (e.g. 2k, 1k, 500, 700)
# followed by the word "full"/"ful" (typo-tolerant) or "ludo", e.g. "2k full", "1k ful", "5k ludo"
TABLE_REQUEST_PATTERN = re.compile(r"\d+\s*k?\s*(fu?ll?|ludo)", re.IGNORECASE)


def is_table_request(text: str) -> bool:
    text = text or ""
    return bool(TABLE_REQUEST_PATTERN.search(text))
# ================================================

# In-memory store: {original_message_id: {"text":..., "poster": ..., "joiner": ..., "chat_id": ...}}
pending_requests = {}

# Tracks confirmed tables so we can detect win reports: {table_message_id: {...}}
active_tables = {}

# Tracks pending win reports awaiting admin confirmation: {report_key: {...}}
pending_win_reports = {}


def is_join_word(text: str) -> bool:
    cleaned = (text or "").strip().lower()
    return cleaned in JOIN_WORDS


def is_win_word(text: str) -> bool:
    cleaned = (text or "").strip().lower()
    return cleaned in WIN_WORDS


def extract_stake_amount(text: str) -> int:
    """Best-effort extraction of the main stake amount from a table request text.
    '5k full' -> 5000, '10k full +500 no iphone' -> 10000, '500 1000 1500 1200' -> 500
    """
    text = text or ""
    k_match = re.search(r"(\d+)\s*k\b", text, re.IGNORECASE)
    if k_match:
        return int(k_match.group(1)) * 1000
    num_match = re.search(r"\d+", text)
    if num_match:
        return int(num_match.group(0))
    return 0


def parse_amount(balance_str: str):
    """Extract the first integer (with optional sign) from a balance string like '+900', '-250', '5000+4750'."""
    if not balance_str or balance_str == "Not found":
        return None
    match = re.search(r"[-+]?\d+", balance_str)
    if match:
        return int(match.group(0))
    return None


def parse_balances(pinned_text: str) -> dict:
    """Parse the pinned message into {name_lowercase: balance_string}.
    Handles lines like:
      ❤️ Aman Raj = -250
      ❤️ Bhaisab  =  850
      ❤️ @Soni_fx  = 9000
      ❤️ sanju  = +6650
      ❤️ Prince 🔥 = 7000 +1425
    Any line containing '=' is treated as a balance line; everything before
    '=' (minus leading emoji/symbols) is the name, everything after is the balance.
    """
    balances = {}
    for raw_line in (pinned_text or "").splitlines():
        if "=" not in raw_line:
            continue
        left, right = raw_line.split("=", 1)
        # Strip leading emoji/symbols but keep letters, digits, @, and apostrophes
        name = re.sub(r"^[^A-Za-z@]+", "", left).strip()
        name = re.sub(r"\s+", " ", name)
        balance = right.strip()
        if not name or not balance:
            continue
        balances[name.lower()] = balance
        if name.startswith("@"):
            balances[name[1:].lower()] = balance
    logger.info(f"Parsed {len(balances)} balance entries from pinned message: {list(balances.items())[:5]}")
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
            logger.info(f"Pinned message raw text (first 300 chars): {pinned.text[:300]}")
            return parse_balances(pinned.text)
        else:
            logger.warning("No pinned message found, or pinned message has no text.")
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
        return

    # Case 3: someone replying to a CONFIRMED table message to report a win
    if msg.reply_to_message and is_win_word(msg.text):
        table_id = msg.reply_to_message.message_id
        table = active_tables.get(table_id)
        if not table:
            return  # not a tracked confirmed table

        reporter_id = msg.from_user.id
        if reporter_id == table["poster_id"]:
            winner, loser = "poster", "joiner"
        elif reporter_id == table["joiner_id"]:
            winner, loser = "joiner", "poster"
        else:
            return  # someone unrelated replied "win", ignore

        winner_id = table[f"{winner}_id"]
        winner_display = table[f"{winner}_display"]
        winner_name = table[f"{winner}_name"]
        winner_username = table[f"{winner}_username"]

        loser_display = table[f"{loser}_display"]
        loser_name = table[f"{loser}_name"]
        loser_username = table[f"{loser}_username"]

        stake = extract_stake_amount(table["stake_text"])
        commission = round(stake * COMMISSION_PERCENT / 100)
        winner_gain = stake - commission

        balances = await get_pinned_balances(context, chat_id)
        winner_bal_str = lookup_balance(balances, winner_name, winner_username)
        loser_bal_str = lookup_balance(balances, loser_name, loser_username)
        winner_bal = parse_amount(winner_bal_str)
        loser_bal = parse_amount(loser_bal_str)

        winner_new = (winner_bal + winner_gain) if winner_bal is not None else None
        loser_new = (loser_bal - stake) if loser_bal is not None else None

        report_key = f"{table_id}:{msg.message_id}"
        pending_win_reports[report_key] = {
            "table_id": table_id,
            "winner_display": winner_display,
            "loser_display": loser_display,
        }

        winner_line = f"{winner_bal_str} → {winner_new}" if winner_new is not None else f"{winner_bal_str} (manual check needed)"
        loser_line = f"{loser_bal_str} → {loser_new}" if loser_new is not None else f"{loser_bal_str} (manual check needed)"

        text_for_admin = (
            "🏆 Win Reported\n\n"
            f"Table: {table['stake_text']}\n"
            f"Winner: {winner_display}\n"
            f"Loser: {loser_display}\n"
            f"Stake: {stake} | Commission ({COMMISSION_PERCENT}%): {commission}\n\n"
            f"Winner balance: {winner_line}\n"
            f"Loser balance: {loser_line}\n\n"
            "Confirm karoge toh ye amounts chart mein manually update kar dena."
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"winconfirm:{report_key}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"winreject:{report_key}"),
            ]
        ])
        await context.bot.send_message(chat_id=ADMIN_ID, text=text_for_admin, reply_markup=keyboard)
        return


async def handle_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("Sirf admin confirm kar sakta hai.", show_alert=True)
        return

    action, callback_key = query.data.split(":", 1)

    if action in ("winconfirm", "winreject"):
        report = pending_win_reports.get(callback_key)
        if not report:
            await query.edit_message_text("Ye win report ab valid nahi hai (expired ya already handled).")
            return
        if action == "winreject":
            await query.edit_message_text(f"❌ Win report rejected:\n{report['winner_display']} vs {report['loser_display']}")
        else:
            await query.edit_message_text(
                f"✅ Confirmed. Ab chart mein manually update kar do:\n"
                f"{report['winner_display']} vs {report['loser_display']}"
            )
        pending_win_reports.pop(callback_key, None)
        return

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
        f"{request['text']}\n"
        "\n"
        f"{request['poster_display']}\n"
        "🆚\n"
        f"{request['joiner_display']}"
    )
    sent_msg = await context.bot.send_message(chat_id=request["chat_id"], text=table_text)

    active_tables[sent_msg.message_id] = {
        "stake_text": request["text"],
        "chat_id": request["chat_id"],
        "poster_id": request["poster_id"],
        "poster_name": request["poster_name"],
        "poster_username": request["poster_username"],
        "poster_display": request["poster_display"],
        "joiner_id": request["joiner_id"],
        "joiner_name": request["joiner_name"],
        "joiner_username": request["joiner_username"],
        "joiner_display": request["joiner_display"],
    }

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
