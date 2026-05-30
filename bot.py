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
gemini = genai.GenerativeModel("gemini-1.5-flash")

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
            image_b64   TEXT
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
def summarise_with_gemini(rows: list[dict]) -> str:
    date_str = datetime.now(TZ).strftime("%d.%m.%Y")

    prompt = (
        f"Du bist ein präziser Ernährungsberater. "
        f"Ich zeige dir alle Nachrichten und Fotos, die zwei Personen heute ({date_str}) über ihre Mahlzeiten geteilt haben. "
        f"Schätze für jede Person separat die Gesamtkalorien und die Gesamtmenge an Protein (in Gramm). "
        f"Antworte ausschließlich auf Deutsch. "
        f"Formatiere die Zusammenfassung genau so:\n\n"
        f"📊 Tagesauswertung – {date_str}\n\n"
        f"👤 {{Name1}}\n"
        f"• Kalorien: ~{{X}} kcal\n"
        f"• Protein: ~{{Y}} g\n\n"
        f"👤 {{Name2}}\n"
        f"• Kalorien: ~{{X}} kcal\n"
        f"• Protein: ~{{Y}} g\n\n"
        f"💬 Kurze Einschätzung (1–2 Sätze pro Person)\n\n"
        f"Falls eine Person heute keine Nachrichten geschickt hat, weise darauf hin.\n\n"
        f"Hier sind die heutigen Einträge:\n"
    )

    # Build content parts: text labels + inline images
    parts = [prompt]

    if not rows:
        parts.append("Heute wurden keine Nachrichten geloggt.")
    else:
        for row in rows:
            name = config.USER_NAMES.get(row["user_id"], row["username"] or f"User {row['user_id']}")
            if row["text"]:
                parts.append(f"[{name}]: {row['text']}")
            if row["image_b64"]:
                if not row["text"]:
                    parts.append(f"[{name}] hat ein Foto geschickt:")
                # Gemini accepts inline images as blobs
                parts.append({
                    "mime_type": "image/jpeg",
                    "data": base64.b64decode(row["image_b64"]),
                })

    response = gemini.generate_content(parts)
    return response.text.strip()

# ── Daily job ──────────────────────────────────────────────────────────────────
async def daily_summary(app: Application):
    logger.info("Running daily calorie summary job…")
    rows = get_today_messages()
    try:
        summary = summarise_with_gemini(rows)
    except Exception as e:
        logger.error("Gemini error: %s", e)
        summary = f"⚠️ Fehler beim Erstellen der Auswertung: {e}"

    await app.bot.send_message(chat_id=config.GROUP_CHAT_ID, text=summary)
    logger.info("Summary sent to group.")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    init_db()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

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
