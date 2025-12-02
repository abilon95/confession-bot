import os
from fastapi import FastAPI, Request
from pydantic import BaseModel
from supabase import create_client, Client
import telebot

# === Environment Variables ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0"))
TARGET_CHANNEL_ID = int(os.environ.get("TARGET_CHANNEL_ID", "0"))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# === Telegram Bot (threaded=False needed for webhook mode) ===
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)

# === Supabase Client ===
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# === FastAPI App ===
app = FastAPI()


@app.post("/")
async def webhook(request: Request):
    data = await request.json()
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])   # <-- Correct update processing
    return {"ok": True}


# === Bot Handlers ===
@bot.message_handler(commands=["start"])
def start_handler(msg):
    bot.reply_to(msg, "Send your confession anonymously.")


@bot.message_handler(func=lambda m: True)
def handle_confession(msg):
    user_id = msg.from_user.id
    text = msg.text

    # Save to Supabase
    supabase.table("confessions").insert({
        "user_id": str(user_id),
        "message": text
    }).execute()

    # Forward anonymously
    bot.send_message(
        TARGET_CHANNEL_ID,
        f"💬 <b>New Confession:</b>\n\n{text}"
    )

    bot.reply_to(msg, "Your confession has been sent anonymously ✔️")


# === Health check ===
@app.get("/")
def home():
    return {"status": "running"}
