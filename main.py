import os
import asyncio
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
PORT = int(os.environ.get("PORT", "5000"))

# === Supabase Client ===
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# === Telegram Bot ===
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# === FastAPI App ===
app = FastAPI()


# Webhook model
class UpdateModel(BaseModel):
    update_id: int
    message: dict | None = None


# === Bot Logic ===
@bot.message_handler(commands=["start"])
def start_message(msg):
    bot.reply_to(msg, "Send your confession anonymously. I'll forward it without showing your identity.")


@bot.message_handler(func=lambda m: True)
def handle_message(msg):
    user_id = msg.from_user.id
    text = msg.text

    # Save confession to Supabase
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


# === Webhook Handler ===
@app.post("/")
async def process_update(req: Request):
    data = await req.json()
    update = UpdateModel(**data)
    bot.process_new_updates([telebot.types.Update.de_json(data)])
    return {"ok": True}


# === Root Route ===
@app.get("/")
def home():
    return {"status": "running"}
