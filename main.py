#!/usr/bin/env python3
"""
Confession Bot — merged version with new features
Includes:
- Confession/comment flows
- Profiles with gender
- Follow/unfollow
- Request to chat
- User reports + ban/retract
- Comment edit/delete
- Confession edit by admin
"""

# ------------------ Standard library imports ------------------
import os
import traceback
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple

# ------------------ Third-party imports ------------------
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from aiogram.filters import Command
from supabase import create_client
from postgrest.exceptions import APIError

# ------------------ Environment (exact names) ------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0"))
TARGET_CHANNEL_ID = int(os.environ.get("TARGET_CHANNEL_ID", "0"))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
PORT = int(os.environ.get("PORT", "5000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY are required")

# ------------------ Clients ------------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

# ------------------ Startup hooks ------------------
async def set_bot_commands(bot: Bot):
    """
    Sets the bot command list (the ones users see in the '/' menu).
    """
    commands = [
        BotCommand(command="share_confession", description="💬 Share a new confession"),
        BotCommand(command="profile", description="View your profile and history"),
        BotCommand(command="rules", description="View the bot's rules"),
    ]
    await bot.set_my_commands(commands)

@app.on_event("startup")
async def on_startup():
    await set_bot_commands(bot)
    print("Startup: bot commands set, app is ready.")

# ------------------ Small in-memory user state (ephemeral) ------------------
user_state: Dict[int, Dict[str, Any]] = {}  # {user_id: {...}}
user_reply_state: Dict[int, Dict[str, Any]] = {}  # reply flow
profile_flow_state: Dict[int, Dict[str, Any]] = {}  # profile edit flow
accepted_terms: set = set()

# ------------------ UI builders ------------------
def build_channel_markup(bot_username: str, conf_id: int, count: int) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=conf_{conf_id}"
    text = f"💬 Add/View Comment ({count})"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])
    return kb

def hub_keyboard(conf_id: int, total_comments: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Comment", callback_data=f"add_c_{conf_id}")],
        [InlineKeyboardButton(text=f"📂 Browse Comments ({total_comments})", callback_data=f"browse_{conf_id}_1")],
    ])
    return kb

def comment_vote_kb(comment_id: int, likes: int, dislikes: int, conf_id: int, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"👍 {likes}", callback_data=f"vote_{comment_id}_up_{conf_id}_{page}"),
            InlineKeyboardButton(text=f"👎 {dislikes}", callback_data=f"vote_{comment_id}_dw_{conf_id}_{page}"),
            InlineKeyboardButton(text="🚩", callback_data=f"report_{comment_id}_{conf_id}")
        ],
        [InlineKeyboardButton(text="↪️ Reply", callback_data=f"reply_{comment_id}_{conf_id}_{page}")]
    ])
    return kb

def pagination_kb(conf_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="⬅ Prev", callback_data=f"browse_{conf_id}_{page-1}"))
    row.append(InlineKeyboardButton(text=f"Page {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        row.append(InlineKeyboardButton(text="Next ➡", callback_data=f"browse_{conf_id}_{page+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="➕ Add Comment", callback_data=f"add_c_{conf_id}")]
    ])
    return kb

def menu_reply_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Menu")]],
        resize_keyboard=True
    )
    return kb

def menu_commands_inline() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Share Confession", callback_data="share_confession")],
        [InlineKeyboardButton(text="/profile", callback_data="cmd_profile")],
        [InlineKeyboardButton(text="/rules", callback_data="cmd_rules")],
        [InlineKeyboardButton(text="/cancel", callback_data="cmd_cancel")],
    ])
    return kb

#part 2
# ------------------ Supabase DB helper functions ------------------
def _safe_insert(table: str, payload: dict):
    try:
        return supabase.table(table).insert(payload).execute()
    except APIError as e:
        msg = getattr(e, "args", [None])[0]
        if isinstance(msg, dict) and "message" in msg and "Could not find the" in msg["message"]:
            reduced = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool, type(None)))}
            return supabase.table(table).insert(reduced).execute()
        raise

# === Confessions ===
def db_add_confession(user_id: str, text: str) -> int:
    payload = {"user_id": user_id, "text": text, "is_approved": False}
    res = supabase.table("confessions").insert(payload).execute()
    return int(res.data[0]["id"])

def db_get_confession(conf_id: int) -> Optional[dict]:
    r = supabase.table("confessions").select("*").eq("id", conf_id).execute()
    return r.data[0] if r.data else None

def db_set_confession_published(conf_id: int, channel_msg_id: int):
    supabase.table("confessions").update({"is_approved": True, "channel_msg_id": channel_msg_id}).eq("id", conf_id).execute()

def db_set_confession_rejected(conf_id: int):
    supabase.table("confessions").update({"is_approved": False}).eq("id", conf_id).execute()

# === Comments ===
def db_add_comment(confession_id: int, user_id: str, username: str, text: str) -> int:
    payload = {"confession_id": confession_id, "user_id": user_id, "username": username, "text": text}
    res = _safe_insert("comments", payload)
    return int(res.data[0]["id"])

def db_get_comments(confession_id: int) -> List[dict]:
    r = supabase.table("comments").select("*").eq("confession_id", confession_id).order("id", desc=False).execute()
    return r.data or []

def db_get_comment(comment_id: int) -> Optional[dict]:
    r = supabase.table("comments").select("*").eq("id", comment_id).execute()
    return r.data[0] if r.data else None

def db_count_comments(confession_id: int) -> int:
    r = supabase.table("comments").select("id", count="exact").eq("confession_id", confession_id).execute()
    return int(r.count or 0)

def db_delete_comment(comment_id: int):
    supabase.table("comments").delete().eq("id", comment_id).execute()
    supabase.table("votes").delete().eq("comment_id", comment_id).execute()
    supabase.table("reports").update({"reason": "resolved"}).eq("comment_id", comment_id).execute()

# === Votes ===
def db_upsert_vote(user_id: str, comment_id: int, vote_value: int):
    supabase.table("votes").upsert({"user_id": user_id, "comment_id": comment_id, "vote": vote_value}).execute()

def db_delete_vote(user_id: str, comment_id: int):
    supabase.table("votes").delete().eq("user_id", user_id).eq("comment_id", comment_id).execute()

def db_get_vote_counts(comment_id: int) -> Tuple[int, int]:
    l = supabase.table("votes").select("*", count="exact").eq("comment_id", comment_id).eq("vote", 1).execute().count or 0
    d = supabase.table("votes").select("*", count="exact").eq("comment_id", comment_id).eq("vote", -1).execute().count or 0
    return int(l), int(d)

# === Reports (comments) ===
def db_add_report(comment_id: int, reporting_user_id: str, reason: str) -> bool:
    existing = supabase.table("reports").select("*").eq("comment_id", comment_id).eq("user_id", reporting_user_id).execute()
    if existing.data:
        return False
    supabase.table("reports").insert({"comment_id": comment_id, "user_id": reporting_user_id, "reason": reason}).execute()
    return True

# === Profiles ===
def db_get_user_profile(user_id: int) -> dict:
    r = supabase.table("profiles").select("*").eq("user_id", str(user_id)).limit(1).execute()
    if r.data:
        return r.data[0]
    else:
        supabase.table("profiles").insert({"user_id": str(user_id)}).execute()
        return {"user_id": str(user_id)}

def db_set_profile_emoji(user_id: int, emoji: Optional[str]):
    supabase.table("profiles").upsert({"user_id": str(user_id), "emoji": emoji}).execute()

def db_set_profile_bio(user_id: int, bio: Optional[str]):
    supabase.table("profiles").upsert({"user_id": str(user_id), "bio": bio}).execute()

def db_set_profile_nickname(user_id: int, nickname: Optional[str]):
    supabase.table("profiles").upsert({"user_id": str(user_id), "nickname": nickname}).execute()

def db_set_profile_gender(user_id: int, gender: Optional[str]):
    supabase.table("profiles").upsert({"user_id": str(user_id), "gender": gender}).execute()

# === Follow system ===
def db_follow(follower_id: str, followed_id: str):
    supabase.table("follows").upsert({"follower_id": follower_id, "followed_id": followed_id}).execute()

def db_unfollow(follower_id: str, followed_id: str):
    supabase.table("follows").delete().eq("follower_id", follower_id).eq("followed_id", followed_id).execute()

def db_is_following(follower_id: str, followed_id: str) -> bool:
    r = supabase.table("follows").select("*").eq("follower_id", follower_id).eq("followed_id", followed_id).execute()
    return bool(r.data)

# === Chat requests ===
def db_add_chat_request(from_user: str, to_user: str):
    supabase.table("chat_requests").insert({"from_user": from_user, "to_user": to_user}).execute()

# === User reports (profiles) ===
def db_add_user_report(reporter_id: str, reported_user_id: str, reason: str):
    return supabase.table("user_reports").insert({
        "reporter_id": reporter_id,
        "reported_user_id": reported_user_id,
        "reason": reason
    }).execute()

# === Bans ===
def is_user_banned(user_id: str) -> bool:
    res = supabase.table("bans").select("ban_until").eq("user_id", user_id).execute()
    if not res.data:
        return False
    ban_until = res.data[0]["ban_until"]
    if ban_until is None:
        return True
    return datetime.utcnow() < datetime.fromisoformat(ban_until)

#part 3 
# ------------------ Profile UI builders ------------------
PROFILE_EMOJIS = [
    "🗣","👻","🥸","🧐","😇","🤠",
    "😎","😜","🦋","👁","☠️","🐼",
    "🐱","🐶","🦊","🦄","🐢","🤡",
    "🤖","👽","👀","👤","🤵‍♂️","🤵‍♀️",
    "🥷","🧚‍♀️","🙎‍♀️","🙎‍♂️","👩‍🦱","🧑‍🦱"
]

def profile_main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 Edit Profile", callback_data="prof_edit")],
        [InlineKeyboardButton(text="📝 My Confessions", callback_data="prof_my_confessions")],
        [InlineKeyboardButton(text="💬 My Comments", callback_data="prof_my_comments")]
    ])
    return kb

def profile_edit_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 Change Profile Emoji", callback_data="prof_edit_emoji")],
        [InlineKeyboardButton(text="✏️ Change Nickname", callback_data="prof_edit_nick")],
        [InlineKeyboardButton(text="📝 Set/Update Bio", callback_data="prof_edit_bio")],
        [InlineKeyboardButton(text="⚧ Set Gender", callback_data="prof_edit_gender")],
        [InlineKeyboardButton(text="🔙 Back to Profile", callback_data="prof_back_profile")]
    ])
    return kb

def emoji_picker_kb() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, e in enumerate(PROFILE_EMOJIS, start=1):
        row.append(InlineKeyboardButton(text=e, callback_data=f"prof_emoji_{e}"))
        if i % 6 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Back to Edit Profile", callback_data="prof_back_edit")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return kb

def render_profile_text(user_id: int) -> str:
    p = db_get_user_profile(user_id)
    emoji = p.get("emoji") or "🙂"
    nickname = p.get("nickname") or "Anonymous"
    bio = p.get("bio") or "NOT SET"
    gender = p.get("gender") or "unspecified"
    name_line = f"{emoji} {nickname}"
    return f"{name_line}\n⚧ Gender: {gender}\n\n📝 Bio: {bio}"

# ------------------ Profile flows ------------------
@dp.callback_query(lambda c: c.data == "prof_edit")
async def prof_edit(call: types.CallbackQuery):
    p = db_get_user_profile(call.from_user.id)
    emoji = p.get("emoji") or "Not set"
    nickname = p.get("nickname") or "Anonymous"
    bio = p.get("bio") or "NOT SET"
    gender = p.get("gender") or "unspecified"
    txt = f"🎨 Profile Customization\n\nEmoji: {emoji}\nNickname: {nickname}\nBio: {bio}\nGender: {gender}"
    await bot.send_message(call.from_user.id, txt, reply_markup=profile_edit_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_back_profile")
async def prof_back_profile(call: types.CallbackQuery):
    txt = render_profile_text(call.from_user.id)
    await bot.send_message(call.from_user.id, txt, reply_markup=profile_main_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_edit_emoji")
async def prof_edit_emoji(call: types.CallbackQuery):
    await bot.send_message(call.from_user.id, "Choose your new profile emoji.", reply_markup=emoji_picker_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith("prof_emoji_"))
async def prof_choose_emoji(call: types.CallbackQuery):
    emoji = call.data.split("_", 2)[2]
    db_set_profile_emoji(call.from_user.id, emoji)
    await bot.send_message(call.from_user.id, "✅ Emoji updated.", reply_markup=profile_edit_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_back_edit")
async def prof_back_edit(call: types.CallbackQuery):
    p = db_get_user_profile(call.from_user.id)
    emoji = p.get("emoji") or "Not set"
    nickname = p.get("nickname") or "Anonymous"
    bio = p.get("bio") or "NOT SET"
    gender = p.get("gender") or "unspecified"
    txt = f"🎨 Profile Customization\n\nEmoji: {emoji}\nNickname: {nickname}\nBio: {bio}\nGender: {gender}"
    await bot.send_message(call.from_user.id, txt, reply_markup=profile_edit_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_edit_bio")
async def prof_edit_bio(call: types.CallbackQuery):
    profile_flow_state[call.from_user.id] = {"await": "bio"}
    await bot.send_message(call.from_user.id, "Please send your new bio (max 250 characters). Send 'remove' to clear your bio.")
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_edit_nick")
async def prof_edit_nick(call: types.CallbackQuery):
    profile_flow_state[call.from_user.id] = {"await": "nick"}
    await bot.send_message(call.from_user.id, "Please send your new nickname (max 32 characters). Send 'default' to reset to Anonymous.")
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_edit_gender")
async def prof_edit_gender(call: types.CallbackQuery):
    profile_flow_state[call.from_user.id] = {"await": "gender"}
    await bot.send_message(call.from_user.id, "Please send your gender (e.g., Male, Female, Non-binary).")
    await call.answer()

# ------------------ Profile input handler ------------------
@dp.message(lambda m: profile_flow_state.get(m.from_user.id))
async def handle_profile_inputs(message: types.Message):
    uid = message.from_user.id
    st = profile_flow_state.get(uid)
    awaiting = st.get("await")
    txt = (message.text or "").strip()

    if awaiting == "bio":
        if txt.lower() == "remove":
            db_set_profile_bio(uid, None)
            await message.answer("✅ Bio cleared.")
        elif len(txt) > 250:
            await message.answer("❌ Bio too long. Please send up to 250 characters.")
            return
        else:
            db_set_profile_bio(uid, txt)
            await message.answer("✅ Bio updated.")
        profile_flow_state.pop(uid, None)

    elif awaiting == "nick":
        if txt.lower() == "default":
            db_set_profile_nickname(uid, None)
            await message.answer("✅ Nickname reset to Anonymous.")
        else:
            if len(txt) > 32:
                await message.answer("❌ Nickname too long. Max 32 characters.")
                return
            db_set_profile_nickname(uid, txt)
            await message.answer("✅ Nickname updated.")
        profile_flow_state.pop(uid, None)

    elif awaiting == "gender":
        db_set_profile_gender(uid, txt)
        await message.answer(f"✅ Gender set to {txt}.")
        profile_flow_state.pop(uid, None)

    # Return to edit profile page
    p = db_get_user_profile(uid)
    emoji = p.get("emoji") or "Not set"
    nickname = p.get("nickname") or "Anonymous"
    bio = p.get("bio") or "NOT SET"
    gender = p.get("gender") or "unspecified"
    txtp = f"🎨 Profile Customization\n\nEmoji: {emoji}\nNickname: {nickname}\nBio: {bio}\nGender: {gender}"
    await message.answer(txtp, reply_markup=profile_edit_kb())

#part 4 
# ------------------ Helpers ------------------
def _safe_reply_or_send(target_chat_id: int, reply_to_message_id: Optional[int], text: str, **kwargs):
    async def _inner():
        try:
            if reply_to_message_id:
                return await bot.send_message(target_chat_id, text, reply_to_message_id=reply_to_message_id, **kwargs)
            else:
                return await bot.send_message(target_chat_id, text, **kwargs)
        except Exception:
            return await bot.send_message(target_chat_id, text, **kwargs)
    return _inner()

# ------------------ Commands: start/profile/rules/cancel + Menu trigger ------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = message.text or ""
    payload = None
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1]

    if payload and payload.startswith("conf_"):
        try:
            conf_id = int(payload.split("_", 1)[1])
        except Exception:
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "Invalid confession link.", reply_markup=menu_reply_keyboard())
            return

        conf = db_get_confession(conf_id)
        if not conf or not conf.get("is_approved"):
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "Confession not found or not published.", reply_markup=menu_reply_keyboard())
            return

        total = db_count_comments(conf_id)
        hub_text = f"*Confession #{conf_id}*\n\n_{conf.get('text')}_\n\nSelect an option below:"
        kb = hub_keyboard(conf_id, total)
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), hub_text, reply_markup=kb)
        return

    if message.from_user.id not in accepted_terms:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Accept Terms", callback_data="accept_terms")],
            [InlineKeyboardButton(text="❌ Decline", callback_data="decline_terms")]
        ])
        terms_text = (
            "📜 *Terms & Conditions*\n\n"
            "1. Admins will review your message.\n"
            "2. Approved messages are posted anonymously.\n"
            "3. Any Comments containing inappropriate content will be removed.\n\n"
            "Click *Accept* to continue."
        )
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), terms_text, reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
            [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")]
        ])
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "What do you want to share?", reply_markup=kb)

@dp.message(Command("share_confession"))
async def cmd_share_confession(message: types.Message):
    if is_user_banned(str(message.from_user.id)):
        await message.answer("⚠️ You are currently banned and cannot post confessions.")
        return
    user_state[message.from_user.id] = {"mode": "share_confession", "active_conf_id": None}
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None),
                              "📝 Okay — send your confession text now.", reply_markup=menu_reply_keyboard())

@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    txt = render_profile_text(message.from_user.id)
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), txt, reply_markup=profile_main_kb())

@dp.message(Command("rules"))
async def cmd_rules(message: types.Message):
    txt = (
        "RULES:\n"
        "1. Be respectful. No hate speech, harassment, or threats.\n"
        "2. No doxxing or sharing personal information.\n"
        "3. Report inappropriate content with 🚩.\n"
        "4. Admins may remove content that violates rules."
    )
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), txt, reply_markup=menu_reply_keyboard())

# ------------------ Comment flows ------------------
@dp.callback_query(lambda c: c.data.startswith("add_c_"))
async def add_comment_cb(call: types.CallbackQuery):
    conf_id = int(call.data.split("_")[1])
    if is_user_banned(str(call.from_user.id)):
        await call.message.answer("⚠️ You are currently banned and cannot post comments.")
        return
    user_reply_state[call.from_user.id] = {"confession_id": conf_id, "parent_comment_id": None, "page": 1}
    await bot.send_message(call.from_user.id, "💬 Send your comment text now.")
    await call.answer()

@dp.message(lambda m: m.from_user.id in user_reply_state)
async def handle_reply(message: types.Message):
    st = user_reply_state.get(message.from_user.id)
    conf_id = st["confession_id"]
    txt = (message.text or "").strip()
    if is_user_banned(str(message.from_user.id)):
        await message.answer("⚠️ You are currently banned and cannot post comments.")
        return
    cid = db_add_comment(conf_id, str(message.from_user.id), message.from_user.username or "anon", txt)
    await message.answer(f"✅ Comment added (ID {cid}).", reply_markup=build_comment_keyboard(cid))
    user_reply_state.pop(message.from_user.id, None)

# ------------------ Comment edit/delete ------------------
@dp.callback_query(lambda c: c.data.startswith("edit_comment_"))
async def edit_comment_cb(call: types.CallbackQuery):
    comment_id = int(call.data.split("_")[2])
    user_reply_state[call.from_user.id] = {"edit_comment_id": comment_id}
    await bot.send_message(call.from_user.id, "✏️ Send the new text for your comment.")
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith("delete_comment_"))
async def delete_comment_cb(call: types.CallbackQuery):
    comment_id = int(call.data.split("_")[2])
    db_delete_comment(comment_id)
    await bot.send_message(call.from_user.id, "🗑️ Comment deleted.")
    await call.answer()

@dp.message(lambda m: user_reply_state.get(m.from_user.id, {}).get("edit_comment_id"))
async def handle_edit_comment(message: types.Message):
    st = user_reply_state.get(message.from_user.id)
    comment_id = st["edit_comment_id"]
    txt = (message.text or "").strip()
    supabase.table("comments").update({"text": txt}).eq("id", comment_id).execute()
    await message.answer("✅ Comment updated.")
    user_reply_state.pop(message.from_user.id, None)

# ------------------ Confession admin edit/delete ------------------
@dp.callback_query(lambda c: c.data.startswith("edit_confession_"))
async def edit_confession_cb(call: types.CallbackQuery):
    conf_id = int(call.data.split("_")[2])
    user_reply_state[call.from_user.id] = {"edit_confession_id": conf_id}
    await bot.send_message(call.from_user.id, "✏️ Send the new text for this confession.")
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith("delete_confession_"))
async def delete_confession_cb(call: types.CallbackQuery):
    conf_id = int(call.data.split("_")[2])
    supabase.table("confessions").delete().eq("id", conf_id).execute()
    await bot.send_message(call.from_user.id, "🗑️ Confession deleted.")
    await call.answer()

@dp.message(lambda m: user_reply_state.get(m.from_user.id, {}).get("edit_confession_id"))
async def handle_edit_confession(message: types.Message):
    st = user_reply_state.get(message.from_user.id)
    conf_id = st["edit_confession_id"]
    txt = (message.text or "").strip()
    supabase.table("confessions").update({"text": txt}).eq("id", conf_id).execute()
    await message.answer("✅ Confession updated.")
    user_reply_state.pop(message.from_user.id, None)

#part 5
# ------------------ Follow/Unfollow ------------------
@dp.callback_query(lambda c: c.data.startswith("follow_"))
async def follow_cb(call: types.CallbackQuery):
    target_id = call.data.split("_")[1]
    db_follow(str(call.from_user.id), target_id)
    await bot.send_message(call.from_user.id, f"✅ You are now following user {target_id}.")
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith("unfollow_"))
async def unfollow_cb(call: types.CallbackQuery):
    target_id = call.data.split("_")[1]
    db_unfollow(str(call.from_user.id), target_id)
    await bot.send_message(call.from_user.id, f"➖ You unfollowed user {target_id}.")
    await call.answer()

# ------------------ Request to Chat ------------------
@dp.callback_query(lambda c: c.data.startswith("request_chat_"))
async def request_chat_cb(call: types.CallbackQuery):
    to_user = int(call.data.split("_")[2])
    from_user = call.from_user.id
    db_add_chat_request(str(from_user), str(to_user))
    profile = db_get_user_profile(str(from_user))
    emoji = profile.get("emoji") or "🙂"
    nickname = profile.get("nickname") or "Anonymous"
    gender = profile.get("gender") or "unspecified"
    bio = profile.get("bio") or "No bio set"
    card = f"👤 {emoji} {nickname}\n⚧ Gender: {gender}\n📝 Bio: {bio}\n\nWants to chat with you"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Share My Profile", callback_data=f"share_profile_{from_user}")],
        [InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_chat_{from_user}")]
    ])
    await bot.send_message(to_user, card, reply_markup=kb)
    await call.answer()

# ------------------ User Reports ------------------
@dp.callback_query(lambda c: c.data.startswith("report_user_"))
async def report_user_cb(call: types.CallbackQuery):
    reported_id = call.data.split("_")[2]
    kb = build_report_reason_keyboard(reported_id)
    await bot.send_message(call.from_user.id, "⚠️ Why are you reporting this user?", reply_markup=kb)
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith("report_reason_"))
async def report_reason_cb(call: types.CallbackQuery):
    parts = call.data.split("_")
    reason = parts[2]
    reported_id = parts[3]
    reporter_id = str(call.from_user.id)
    res = db_add_user_report(reporter_id, reported_id, reason)
    report_id = res.data[0]["id"]
    await bot.send_message(reporter_id, "✅ Report submitted for review.")
    kb = build_admin_review_keyboard(reported_id, report_id)
    await bot.send_message(ADMIN_GROUP_ID,
        f"⚠️ New User Report\nReporter: {reporter_id}\nReported User: {reported_id}\nReason: {reason}",
        reply_markup=kb
    )
    await call.answer()

# ------------------ Ban / Retract Ban ------------------
@dp.callback_query(lambda c: c.data.startswith("ban_"))
async def ban_cb(call: types.CallbackQuery):
    parts = call.data.split("_")
    duration = parts[1]
    user_id = parts[2]
    report_id = parts[3]
    if duration == "1w":
        ban_until = datetime.utcnow() + timedelta(weeks=1)
    elif duration == "1m":
        ban_until = datetime.utcnow() + timedelta(days=30)
    elif duration == "perm":
        ban_until = None
    else:
        ban_until = datetime.utcnow() + timedelta(days=7)
    supabase.table("bans").upsert({"user_id": user_id, "ban_until": ban_until}).execute()
    await bot.send_message(ADMIN_GROUP_ID, f"🚫 User {user_id} banned ({duration}).")
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith("retract_ban_"))
async def retract_ban_cb(call: types.CallbackQuery):
    user_id = call.data.split("_")[2]
    supabase.table("bans").delete().eq("user_id", user_id).execute()
    await bot.send_message(ADMIN_GROUP_ID, f"↩️ Ban retracted for user {user_id}.")
    await call.answer()

# ------------------ Webhook Runner ------------------
@app.post("/")
async def webhook(request: Request):
    try:
        update = await request.json()
        await dp.feed_update(bot, update)
    except Exception as e:
        print("Webhook error:", e, traceback.format_exc())
    return {"ok": True}


