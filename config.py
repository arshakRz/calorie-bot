import os

# ── Telegram ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
GROUP_CHAT_ID: int = int(os.environ["GROUP_CHAT_ID"])

# ── OpenAI ──────────────────────────────────────────────────────────────────────
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]

# ── User mapping ────────────────────────────────────────────────────────────────
USER_NAMES: dict[int, str] = {
    int(os.environ["USER1_ID"]): os.environ["USER1_NAME"],
    int(os.environ["USER2_ID"]): os.environ["USER2_NAME"],
}

# ── Database ────────────────────────────────────────────────────────────────────
DB_PATH: str = os.environ.get("DB_PATH", "calories.db")
