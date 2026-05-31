import logging
import sqlite3
import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openai import OpenAI
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

# ── OpenAI client ──────────────────────────────────────────────────────────────
openai_client = OpenAI(api_key=config.OPENAI_API_KEY)

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
    now = datetime.now(TZ)
    since_local = now.replace(hour=23, minute=59, second=0, microsecond=0) - timedelta(days=1)
    since_utc = since_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT user_id, username, text, image_b64 FROM messages WHERE created_at >= ?",
        (since_utc,),
    ).fetchall()
    conn.close()
    return [{"user_id": r[0], "username": r[1], "text": r[2], "image_b64": r[3]} for r in rows]


def get_today_messages() -> list[dict]:
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT user_id, username, text, image_b64 FROM messages WHERE date = ?",
        (date_str,),
    ).fetchall()
    conn.close()
    return [{"user_id": r[0], "username": r[1], "text": r[2], "image_b64": r[3]} for r in rows]


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

    # Convert to JPEG to ensure compatibility with OpenAI vision
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(bytes(image_bytes))).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    image_b64 = base64.b64encode(buf.getvalue()).decode()

    caption = msg.caption or ""
    logger.info("Photo from %s (caption: %s)", display_name(user.id, user.username), caption)
    save_message(user.id, user.username or "", caption or None, image_b64)


# ── OpenAI summariser ──────────────────────────────────────────────────────────
def build_prompt(date_str: str) -> str:
    lines = [
        "You are a helpful food journal assistant.",
        "Two people are tracking what they eat by sending text messages and photos of their meals.",
        "For each photo, first identify what food or drink you can see in it, then use that to estimate the nutritional content.",
        "For each person, add up all their meals from both text and photos and provide a total calorie and protein estimate.",
        "Use realistic average portion sizes when exact amounts are not specified.",
        "This is for personal health tracking so always provide your best estimate even if approximate.",
        "Reply exclusively in English.",
        "Format the summary exactly like this:",
        "",
        f"📊 Calorie Summary - {date_str}",
        "",
        "👤 {Name1}",
        "• Calories: ~{X} kcal",
        "• Protein: ~{Y} g",
        "",
        "👤 {Name2}",
        "• Calories: ~{X} kcal",
        "• Protein: ~{Y} g",
        "",
        "💬 Brief feedback (1-2 sentences per person)",
        "",
        "If a person has not sent any messages, mention that.",
        "",
        "Here are the entries:",
    ]
    return "\n".join(lines)


def summarise_with_openai(rows: list[dict], since_label: str = "today") -> str:
    date_str = datetime.now(TZ).strftime("%d.%m.%Y")
    content: list = [{"type": "text", "text": build_prompt(date_str)}]

    if not rows:
        content.append({"type": "text", "text": "No messages were logged."})
    else:
        for row in rows:
            name = config.USER_NAMES.get(row["user_id"], row["username"] or f"User {row['user_id']}")
            if row["text"]:
                content.append({"type": "text", "text": f"[{name}]: {row['text']}"})
            if row["image_b64"]:
                if not row["text"]:
                    content.append({"type": "text", "text": f"[{name}] sent a photo:"})
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{row['image_b64']}",
                        "detail": "low",
                    },
                })

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": content}],
        max_tokens=800,
    )
    return response.choices[0].message.content.strip()


# ── /flush_db command ──────────────────────────────────────────────────────────
async def cmd_flush_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != config.GROUP_CHAT_ID:
        return
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    conn = sqlite3.connect(config.DB_PATH)
    deleted = conn.execute("DELETE FROM messages WHERE date = ?", (date_str,)).rowcount
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🗑️ Cleared {deleted} message(s) from today's log.")
    logger.info("/flush_db cleared %d messages for %s", deleted, date_str)


# ── /logs command ──────────────────────────────────────────────────────────────
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != config.GROUP_CHAT_ID:
        return
    rows = get_today_messages()
    if not rows:
        await update.message.reply_text("📭 No messages logged today yet.")
        return
    lines = ["📋 *Today's logged entries:*\n"]
    for i, row in enumerate(rows, 1):
        name = config.USER_NAMES.get(row["user_id"], row["username"] or f"User {row['user_id']}")
        if row["text"] and row["image_b64"]:
            lines.append(f"{i}. 👤 {name} — 📸 Photo + 💬 \"{row['text']}\"")
        elif row["image_b64"]:
            lines.append(f"{i}. 👤 {name} — 📸 Photo only")
        elif row["text"]:
            lines.append(f"{i}. 👤 {name} — 💬 \"{row['text']}\"")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    logger.info("/logs command used.")


# ── /calculate command ─────────────────────────────────────────────────────────
async def cmd_calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != config.GROUP_CHAT_ID:
        return
    await update.message.reply_text("⏳ Calculating your intake since last night 23:59... hang on!")
    rows = get_messages_since_last_summary()
    try:
        summary = summarise_with_openai(rows, since_label="since yesterday 23:59")
    except Exception as e:
        logger.error("OpenAI error: %s", e)
        summary = f"⚠️ Error generating summary: {e}"
    await update.message.reply_text(summary)
    logger.info("/calculate summary sent.")


# ── Daily job ──────────────────────────────────────────────────────────────────
async def daily_summary(app: Application):
    logger.info("Running daily calorie summary job...")
    rows = get_today_messages()
    try:
        summary = summarise_with_openai(rows, since_label="today")
    except Exception as e:
        logger.error("OpenAI error: %s", e)
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
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("flush_db", cmd_flush_db))

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(daily_summary, trigger="cron", hour=23, minute=59, args=[app])
    scheduler.start()
    logger.info("Scheduler started - daily summary at 23:59 Europe/Berlin")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()