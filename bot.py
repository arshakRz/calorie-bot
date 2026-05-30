import logging
import sqlite3
import base64
import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import OpenAI

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
            date        TEXT NOT NULL,          -- YYYY-MM-DD in Berlin time
            user_id     INTEGER NOT NULL,
            username    TEXT NOT NULL,
            text        TEXT,
            image_b64   TEXT                    -- base64-encoded JPEG, or NULL
        )
    """)
    conn.commit()
    conn.close()


def save_message(user_id: int, username: str, text: str | None, image_b64: str | None):
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO messages (date, user_id, username, text, image_b64) VALUES (?,?,?,?,?)",
        (date_str, user_id, username, text, image_b64),
    )
    conn.commit()
    conn.close()


def get_today_messages() -> list[dict]:
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
    """Return a friendly name for a user."""
    return config.USER_NAMES.get(user_id, username or f"User {user_id}")

# ── Message handlers ───────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return
    if update.effective_chat.id != config.GROUP_CHAT_ID:
        return  # ignore messages from other chats

    user = msg.from_user
    if user.id not in config.USER_NAMES:
        return  # only track configured users

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

    # Download the largest available photo
    photo = msg.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    image_b64 = base64.b64encode(image_bytes).decode()

    caption = msg.caption or ""
    logger.info("Photo from %s (caption: %s)", display_name(user.id, user.username), caption)
    save_message(user.id, user.username or "", caption or None, image_b64)

# ── GPT-4o summariser ──────────────────────────────────────────────────────────
def build_openai_messages(rows: list[dict]) -> list:
    """
    Build the messages list for the OpenAI API call.
    Each row becomes a labelled entry; images are embedded as base64.
    """
    content: list = []

    content.append({
        "type": "text",
        "text": (
            "Du bist ein präziser Ernährungsberater. "
            "Ich zeige dir alle Nachrichten und Fotos, die zwei Personen heute über ihre Mahlzeiten geteilt haben. "
            "Schätze für jede Person separat die Gesamtkalorien und die Gesamtmenge an Protein (in Gramm). "
            "Antworte ausschließlich auf Deutsch. "
            "Formatiere die Zusammenfassung genau so:\n\n"
            "📊 Tagesauswertung – {datum}\n\n"
            "👤 {Name1}\n"
            "• Kalorien: ~{X} kcal\n"
            "• Protein: ~{Y} g\n\n"
            "👤 {Name2}\n"
            "• Kalorien: ~{X} kcal\n"
            "• Protein: ~{Y} g\n\n"
            "💬 Kurze Einschätzung (1–2 Sätze pro Person)\n\n"
            "Falls eine Person heute keine Nachrichten geschickt hat, weise darauf hin."
        ),
    })

    if not rows:
        content.append({"type": "text", "text": "Heute wurden keine Nachrichten geloggt."})
        return [{"role": "user", "content": content}]

    for row in rows:
        name = config.USER_NAMES.get(row["user_id"], row["username"] or f"User {row['user_id']}")
        label = f"[{name}]"

        if row["text"]:
            content.append({"type": "text", "text": f"{label}: {row['text']}"})

        if row["image_b64"]:
            if row["text"]:
                pass  # caption already added above
            else:
                content.append({"type": "text", "text": f"{label} hat ein Foto geschickt:"})
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{row['image_b64']}",
                    "detail": "low",   # saves tokens; switch to "high" for better accuracy
                },
            })

    return [{"role": "user", "content": content}]


def summarise_with_gpt(rows: list[dict]) -> str:
    date_str = datetime.now(TZ).strftime("%d.%m.%Y")
    messages = build_openai_messages(rows)
    # Inject today's date into the system prompt
    messages[0]["content"][0]["text"] = messages[0]["content"][0]["text"].replace(
        "{datum}", date_str
    )

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=800,
    )
    return response.choices[0].message.content.strip()

# ── Daily job ──────────────────────────────────────────────────────────────────
async def daily_summary(app: Application):
    logger.info("Running daily calorie summary job…")
    rows = get_today_messages()
    try:
        summary = summarise_with_gpt(rows)
    except Exception as e:
        logger.error("OpenAI error: %s", e)
        summary = f"⚠️ Fehler beim Erstellen der Auswertung: {e}"

    await app.bot.send_message(chat_id=config.GROUP_CHAT_ID, text=summary)
    logger.info("Summary sent to group.")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    init_db()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Register handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Scheduler – fires every day at 23:59 Berlin time
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
