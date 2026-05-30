import logging
import sqlite3
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import google.generativeai as genai
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Gemini client ──────────────────────────────────────────────────────────────
genai.configure(api_key=config.GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.0-flash")

# ── Timezone ───────────────────────────────────────────────────────────────────
TZ = ZoneInfo("Europe/Berlin")

# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            username    TEXT NOT NULL,
            text        TEXT,
            image_b64   TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def save_message(user_id: int, username: str, text: str | None, image_b64: str | None):
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO messages (date, user_id, username, text, image_b64, created_at) VALUES (?,?,?,?,?,?)",
        (date_str, user_id, username, text, image_b64, now_utc),
    )
    conn.commit()
    conn.close()


def get_messages_since_last_summary() -> list[dict]:
    """
    Returns all messages sent since yesterday 23:59 (Berlin time).
    This is what /calculate uses — covers everything from the last
    automatic summary up until right now.
    """
    now = datetime.now(TZ)

    # "since" = yesterday at 23:59 local time, converted to UTC for DB comparison
    from datetime import timedelta
    since_local = now.replace(hour=23, minute=59, second=0, microsecond=0) - timedelta(days=1)
    since_utc = since_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT user_id, username, text, image_b64 FROM messages WHERE created_at >= ?",
        (since_utc,),
    ).fetchall()
    conn.close()
    return [
        {"user_id": r[0], "username": r[1], "text": r[2], "image_b64": r[3]}
        for r in rows
    ]


def get_today_messages() -> list[dict]:
    """Returns all messages for today (used by the automatic 23:59 job)."""
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT user_id, username, text, image_b64 FROM messages WHERE date = ?",
        (date_str,),
    ).fetchall()
    conn.close()
    return [
        {"user_id": r[0], "username": r[1], "text": r[2], "image_b64": r[3]}
        for r in rows
    ]

# ── Display name helper ────────────────────────────────────────────────────────
def display_name(user_id: int, username: str) -> str:
    return config.USER_NAMES.get(user_id, username or f"User {user_id}")

# ── Message handlers ───────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return
    if update.effective_chat.id != config.GROUP_CHAT_ID:
        return

    user = msg.from_user
    if user.id not in config.USER_NAMES:
        return

    logger.info("Text from %s: %s", display_name(user.id, user.username), msg.text)
    save_message(user.id, user.username or "", msg.text, None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return
    if update.effective_chat.id != config.GROUP_CHAT_ID:
        return

    user = msg.from_user
    if user.id not in config.USER_NAMES:
        return

    photo = msg.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    image_b64 = base64.b64encode(image_bytes).decode()

    caption = msg.caption or ""
    logger.info("Photo from %s (caption: %s)", display_name(user.id, user.username), caption)
    save_message(user.id, user.username or "", caption or None, image_b64)

# ── Gemini summariser ──────────────────────────────────────────────────────────
def build_prompt(date_str: str, since_label: str) -> str:
    return (
        f"You are a precise nutrition assistant. "
        f"Below are all the messages and food photos two people shared about their meals {since_label} ({date_str}). "
        f"Estimate the total calories and total protein (in grams) for each person separately. "
        f"Reply exclusively in English. "
        f"Format the summary exactly like this:\n\n"
        f"📊 Calorie Summary – {date_str}\n\n"
        f"👤 {{Name1}}\n"
        f"• Calories: ~{{X}} kcal\n"
        f"• Protein: ~{{Y}} g\n\n"
        f"👤 {{Name2}}\n"
        f"• Calories: ~{{X}} kcal\n"
        f"• Protein: ~{{Y}} g\n\n"
        f"💬 Brief feedback (1–2 sentences per person)\n\n"
        f"If a person hasn't sent any messages, mention that.\n\n"
        f"Here are the entries:\n"
    )


def summarise_with_gemini(rows: list[dict], since_label: str = "today") -> str:
    date_str = datetime.now(TZ).strftime("%d.%m.%Y")
    parts = [build_prompt(date_str, since_label)]

    if not rows:
        parts.append("No messages were logged.")
    else:
        for row in rows:
            name = config.USER_NAMES.get(row["user_id"], row["username"] or f"User {row['user_id']}")
            if row["text"]:
                parts.append(f"[{name}]: {row['text']}")
            if row["image_b64"]:
                if not row["text"]:
                    parts.append(f"[{name}] sent a photo:")
                parts.append({
                    "mime_type": "image/jpeg",
                    "data": base64.b64decode(row["image_b64"]),
                })

    response = gemini.generate_content(parts)
    return response.text.strip()

# ── /calculate command ─────────────────────────────────────────────────────────
async def cmd_calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != config.GROUP_CHAT_ID:
        return

    await update.message.reply_text("⏳ Calculating your intake since last night 23:59... hang on!")

    rows = get_messages_since_last_summary()
    try:
        summary = summarise_with_gemini(rows, since_label="since yesterday 23:59")
    except Exception as e:
        logger.error("Gemini error: %s", e)
        summary = f"⚠️ Error generating summary: {e}"

    await update.message.reply_text(summary)
    logger.info("/calculate summary sent.")

# ── Daily job ──────────────────────────────────────────────────────────────────
async def daily_summary(app: Application):
    logger.info("Running daily calorie summary job…")
    rows = get_today_messages()
    try:
        summary = summarise_with_gemini(rows, since_label="today")
    except Exception as e:
        logger.error("Gemini error: %s", e)
        summary = f"⚠️ Error generating summary: {e}"

    await app.bot.send_message(chat_id=config.GROUP_CHAT_ID, text=summary)
    logger.info("Daily summary sent to group.")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    init_db()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("calculate", cmd_calculate))

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        daily_summary,
        trigger="cron",
        hour=23,
        minute=59,
        args=[app],
    )
    scheduler.start()
    logger.info("Scheduler started – daily summary at 23:59 Europe/Berlin")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()