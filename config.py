"""
config.py – edit this file with your actual values before deploying.

All sensitive values are read from environment variables so you never
hardcode secrets in the source code.
"""
import os

# ── Telegram ────────────────────────────────────────────────────────────────────
# Get this from @BotFather on Telegram
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

# The numeric chat ID of your group.
# Easy way to find it:
#   1. Add @userinfobot to the group
#   2. It will print the group's chat ID (a negative number like -1001234567890)
GROUP_CHAT_ID: int = int(os.environ["GROUP_CHAT_ID"])

# ── OpenAI ──────────────────────────────────────────────────────────────────────
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]

# ── User mapping  ───────────────────────────────────────────────────────────────
# Map each person's Telegram numeric user ID → display name used in the summary.
# How to find your Telegram user ID:
#   Send any message to @userinfobot – it replies with your numeric ID.
#
# Example:
#   USER_NAMES = {
#       123456789: "Max",
#       987654321: "Anna",
#   }
USER_NAMES: dict[int, str] = {
    int(os.environ["USER1_ID"]): os.environ["USER1_NAME"],
    int(os.environ["USER2_ID"]): os.environ["USER2_NAME"],
}

# ── Database ────────────────────────────────────────────────────────────────────
# SQLite file path – Railway persists /data if you mount a volume there,
# otherwise the current directory is fine for testing.
DB_PATH: str = os.environ.get("DB_PATH", "calories.db")
