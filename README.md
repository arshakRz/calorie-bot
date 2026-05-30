# 🥗 Calorie Bot

A Telegram bot that silently watches your group chat, logs all food messages and photos throughout the day, and sends a GPT-4o powered calorie + protein summary every night at **23:59 (Europe/Berlin)**.

---

## Setup

### 1. Create a Telegram Bot
1. Open Telegram → search for **@BotFather**
2. Send `/newbot` and follow the steps
3. Copy the **bot token** you receive
4. Send `/setprivacy` → select your bot → choose **Disable**  
   *(this lets the bot read all group messages, not just commands)*

### 2. Find your Telegram IDs
- Add **@userinfobot** to your group → it will print the **group chat ID** (negative number)
- Message **@userinfobot** privately → get your personal numeric **user ID**
- Do the same for your GF's account

### 3. Get an OpenAI API key
- Go to [platform.openai.com](https://platform.openai.com) → API keys → Create new key

### 4. Set environment variables
On **Railway**: go to your service → **Variables** tab and add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `GROUP_CHAT_ID` | negative number, e.g. `-1001234567890` |
| `OPENAI_API_KEY` | from OpenAI |
| `USER1_ID` | your Telegram user ID |
| `USER1_NAME` | your name (e.g. `Max`) |
| `USER2_ID` | your GF's Telegram user ID |
| `USER2_NAME` | her name (e.g. `Anna`) |

For **local testing**, copy `.env.example` to `.env` and fill it in, then run:
```bash
pip install -r requirements.txt
python bot.py
```

### 5. Deploy on Railway
1. Push this repo to GitHub (keep it **private**)
2. On Railway → New Project → **GitHub Repository** → select this repo
3. Add all variables under the **Variables** tab
4. Railway auto-deploys – the bot starts immediately ✅

---

## How it works

| Time | What happens |
|---|---|
| All day | Bot silently saves every text/photo message from the two configured users |
| 23:59 | Bot sends all messages + images to GPT-4o |
| 23:59 | GPT-4o estimates calories & protein per person |
| 23:59 | Summary is posted to the group in German |

---

## Persistent storage on Railway (optional but recommended)

By default, SQLite data is lost on redeploy. To persist it:
1. Railway → your service → **Volumes** → Add Volume → mount at `/data`
2. Set env var `DB_PATH=/data/calories.db`

---

## Cost estimate

- Railway: free tier or ~$5/mo
- OpenAI GPT-4o: ~$0.01–0.05 per daily summary (tiny)
