#!/usr/bin/env python3
"""
Final Confession Bot — Render + Supabase (webhook)
- FastAPI webhook intended for: gunicorn main:app --worker-class uvicorn.workers.UvicornWorker --workers 1 --bind 0.0.0.0:$PORT
- Aiogram 3.x dispatcher (using dp.feed_update in webhook)
- Supabase via supabase-py
- Environment vars (exact names expected):
    BOT_TOKEN
    ADMIN_GROUP_ID
    TARGET_CHANNEL_ID
    SUPABASE_URL
    SUPABASE_KEY
    PORT

This version includes:
- Strict single-worker assumption (set via Render Start Command with --workers 1).
- Filtered profile and reply handlers to avoid swallowing general messages.
- Catch-all general message handler placed last to process confessions and comments.
- Streamlined persistent inline menu: Share Confession, /profile, /rules, /cancel (no /help or /privacy).
- Robust /start flow: deep-link /start conf_<id> shows confession hub; normal /start shows share options.
- Debug prints at the top of every handler and webhook for tracing.
- Defensive Supabase calls with best-effort fallbacks.
- Clean separation of accepted_terms set from per-user state dict.

NOTE: Intentionally verbose to exceed ~1000 lines for clarity and traceability.
"""

# ------------------ Standard library imports ------------------
import os
import math
import traceback
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
    Simplified per Abel's request: /profile, /rules, /cancel only.
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
# For flows: accept terms -> choose type -> send confession / add comment / report reason
user_state: Dict[int, Dict[str, Any]] = {}  # {user_id: {...}}

# For reply flow: next message treated as reply
user_reply_state: Dict[int, Dict[str, Any]] = {}  # {user_id: {"confession_id": int, "parent_comment_id": int, "page": int}}

# Profile edit flows: track awaiting input
profile_flow_state: Dict[int, Dict[str, Any]] = {}  # {user_id: {"await": "bio"|"nick"}}

# Accepted terms set (split from user_state to avoid confusion)
accepted_terms: set = set()

# ------------------ UI builders ------------------
def build_channel_markup(bot_username: str, conf_id: int, count: int) -> InlineKeyboardMarkup:
    """Button on the channel post that deep-links into bot start with conf payload."""
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

# Persistent reply keyboard with a Menu button
def menu_reply_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Menu")]
        ],
        resize_keyboard=True
    )
    return kb

# Inline menu showing the main commands (simplified)
def menu_commands_inline() -> InlineKeyboardMarkup:
    """
    Persistent inline menu per Abel's spec:
    - Share Confession (callback: share_confession)
    - /profile (callback: cmd_profile)
    - /rules (callback: cmd_rules)
    - /cancel (callback: cmd_cancel)
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Share Confession", callback_data="share_confession")],
        [InlineKeyboardButton(text="/profile", callback_data="cmd_profile")],
        [InlineKeyboardButton(text="/rules", callback_data="cmd_rules")],
        [InlineKeyboardButton(text="/cancel", callback_data="cmd_cancel")],
    ])
    return kb

# ------------------ Supabase DB helper functions ------------------
def _safe_insert(table: str, payload: dict):
    """
    Insert with fallback: some Supabase projects may not have the same schema.
    If insertion fails due to missing column in schema cache (PGRST204), try a reduced payload.
    Returns the response object (res.data etc) or raises.
    """
    try:
        return supabase.table(table).insert(payload).execute()
    except APIError as e:
        msg = getattr(e, "args", [None])[0]
        if isinstance(msg, dict) and "message" in msg and "Could not find the" in msg["message"]:
            reduced = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool, type(None)))}
            try:
                return supabase.table(table).insert(reduced).execute()
            except Exception:
                raise
        raise

def db_add_confession(user_id: str, text: str) -> int:
    payload = {"user_id": user_id, "text": text, "is_approved": False}
    res = _safe_insert("confessions", payload)
    try:
        return int(res.data[0]["id"])
    except Exception:
        r = supabase.table("confessions").select("*").eq("user_id", user_id).eq("text", text).order("id", {"ascending": False}).limit(1).execute()
        if r.data:
            return int(r.data[0]["id"])
        raise RuntimeError("Could not determine confession id after insert")

def db_get_confession(conf_id: int) -> Optional[dict]:
    r = supabase.table("confessions").select("*").eq("id", conf_id).execute()
    return r.data[0] if r.data else None

def db_set_confession_published(conf_id: int, channel_msg_id: int):
    try:
        supabase.table("confessions").update({"is_approved": True, "channel_msg_id": channel_msg_id}).eq("id", conf_id).execute()
    except Exception:
        try:
            supabase.table("confessions").update({"channel_msg_id": channel_msg_id}).eq("id", conf_id).execute()
        except Exception:
            pass

def db_set_confession_rejected(conf_id: int):
    try:
        supabase.table("confessions").update({"is_approved": False}).eq("id", conf_id).execute()
    except Exception:
        pass

def db_add_comment(confession_id: int, user_id: str, username: str, text: str) -> int:
    payload = {
        "confession_id": confession_id,
        "user_id": user_id,
        "username": username,
        "text": text
    }
    res = _safe_insert("comments", payload)
    try:
        return int(res.data[0]["id"])
    except Exception:
        r = supabase.table("comments").select("*").eq("confession_id", confession_id).eq("user_id", user_id).eq("text", text).order("id", {"ascending": False}).limit(1).execute()
        if r.data:
            return int(r.data[0]["id"])
        raise RuntimeError("Could not determine comment id after insert")

def db_get_comments(confession_id: int) -> List[dict]:
    r = (
        supabase.table("comments")
        .select("*")
        .eq("confession_id", confession_id)
        .order("id", desc=False)   # ascending order
        .execute()
    )
    return r.data or []

def db_get_comment(comment_id: int) -> Optional[dict]:
    r = supabase.table("comments").select("*").eq("id", comment_id).execute()
    return r.data[0] if r.data else None

def db_count_comments(confession_id: int) -> int:
    r = supabase.table("comments").select("id", count="exact").eq("confession_id", confession_id).execute()
    return int(r.count or 0)

def db_delete_comment(comment_id: int):
    try:
        # delete the target comment
        supabase.table("comments").delete().eq("id", comment_id).execute()
        # optional cascade delete replies
        supabase.table("comments").delete().eq("parent_comment_id", comment_id).execute()
    except Exception:
        pass
    try:
        supabase.table("votes").delete().eq("comment_id", comment_id).execute()
    except Exception:
        pass
    try:
        supabase.table("reports").update({"reason": "resolved"}).eq("comment_id", comment_id).execute()
    except Exception:
        pass

def db_upsert_vote(user_id: str, comment_id: int, vote_value: int):
    try:
        supabase.table("votes").upsert({
            "user_id": user_id,
            "comment_id": comment_id,
            "vote": vote_value
        }).execute()
    except Exception:
        pass

def db_delete_vote(user_id: str, comment_id: int):
    try:
        supabase.table("votes").delete().eq("user_id", user_id).eq("comment_id", comment_id).execute()
    except Exception:
        pass

def db_get_vote_counts(comment_id: int) -> Tuple[int, int]:
    try:
        l = supabase.table("votes").select("*", count="exact").eq("comment_id", comment_id).eq("vote", 1).execute().count or 0
        d = supabase.table("votes").select("*", count="exact").eq("comment_id", comment_id).eq("vote", -1).execute().count or 0
        return int(l), int(d)
    except Exception:
        return 0, 0

def db_add_report(comment_id: int, reporting_user_id: str, reason: str) -> bool:
    try:
        existing = supabase.table("reports").select("*").eq("comment_id", comment_id).eq("user_id", reporting_user_id).execute()
        if existing.data:
            return False
        supabase.table("reports").insert({
            "comment_id": comment_id,
            "user_id": reporting_user_id,
            "reason": reason
        }).execute()
        return True
    except Exception:
        return False

# ------------------ Supabase Profile helpers ------------------
def db_get_user_profile(user_id: int) -> dict:
    try:
        r = supabase.table("profiles").select("*").eq("user_id", str(user_id)).limit(1).execute()
        if r.data:
            row = r.data[0]
            return {
                "emoji": row.get("emoji"),
                "nickname": row.get("nickname"),
                "bio": row.get("bio")
            }
        else:
            supabase.table("profiles").insert({
                "user_id": str(user_id),
                "emoji": None,
                "nickname": None,
                "bio": None
            }).execute()
            return {"emoji": None, "nickname": None, "bio": None}
    except Exception:
        return {"emoji": None, "nickname": None, "bio": None}

def db_set_profile_emoji(user_id: int, emoji: Optional[str]):
    try:
        supabase.table("profiles").upsert({"user_id": str(user_id), "emoji": emoji}).execute()
    except Exception:
        pass

def db_set_profile_bio(user_id: int, bio: Optional[str]):
    try:
        supabase.table("profiles").upsert({"user_id": str(user_id), "bio": bio}).execute()
    except Exception:
        pass

def db_set_profile_nickname(user_id: int, nickname: Optional[str]):
    try:
        supabase.table("profiles").upsert({"user_id": str(user_id), "nickname": nickname}).execute()
    except Exception:
        pass

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
    name_line = f"{emoji} {nickname}"
    return f"{name_line}\n\n📝 Bio: {bio}"

# ------------------ Helpers ------------------
def _safe_reply_or_send(target_chat_id: int, reply_to_message_id: Optional[int], text: str, **kwargs):
    """
    Coroutine wrapper helper that tries to reply; if reply fails (message missing) send directly.
    Returns coroutine (await it).
    """
    async def _inner():
        try:
            if reply_to_message_id:
                return await bot.send_message(target_chat_id, text, reply_to_message_id=reply_to_message_id, **kwargs)
            else:
                return await bot.send_message(target_chat_id, text, **kwargs)
        except Exception:
            # fallback to send_message without reply
            return await bot.send_message(target_chat_id, text, **kwargs)
    return _inner()

# ------------------ Commands: start/profile/rules/cancel + Menu trigger ------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    print("cmd_start triggered:", message.text)
    text = message.text or ""
    payload = None
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1]

    # Deep link: /start conf_<id> — show confession hub with add/browse
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

    # Normal /start -> Terms or share menu
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
    print("cmd_share_confession triggered:", message.text)
    user_state[message.from_user.id] = {"mode": "share_confession", "active_conf_id": None}
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None),
                              "📝 Okay — send your confession text now.", reply_markup=menu_reply_keyboard())

@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    print("cmd_profile triggered:", message.text)
    txt = render_profile_text(message.from_user.id)
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), txt, reply_markup=profile_main_kb())

@dp.message(Command("rules"))
async def cmd_rules(message: types.Message):
    print("cmd_rules triggered:", message.text)
    txt = (
        "Rules:\n"
        "1. Be respectful. No hate speech, harassment, or threats.\n"
        "2. No doxxing or sharing personal information.\n"
        "3. Report inappropriate content with 🚩.\n"
        "4. Admins may remove content that violates rules."
    )
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), txt, reply_markup=menu_reply_keyboard())

# Reply keyboard "Menu" trigger
@dp.message(lambda m: (m.text or "").strip().lower() == "menu")
async def show_menu(message: types.Message):
    print("show_menu triggered:", message.text)
    txt = "Menu:\n📝 Share Confession • /profile • /rules • /cancel"
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), txt, reply_markup=menu_commands_inline())

# Inline menu commands
@dp.callback_query(lambda c: c.data in ("cmd_profile","cmd_rules","cmd_cancel","share_confession"))
async def menu_inline_commands(call: types.CallbackQuery):
    print("menu_inline_commands triggered:", call.data)
    if call.data == "cmd_profile":
        txt = render_profile_text(call.from_user.id)
        await bot.send_message(call.from_user.id, txt, reply_markup=profile_main_kb())
    elif call.data == "cmd_rules":
        await bot.send_message(call.from_user.id,
            "Rules:\n1. Be respectful.\n2. No doxxing.\n3. Use 🚩 to report.\n4. Admins may remove content.",
            reply_markup=menu_reply_keyboard()
        )
    elif call.data == "cmd_cancel":
        user_state.pop(call.from_user.id, None)
        user_reply_state.pop(call.from_user.id, None)
        profile_flow_state.pop(call.from_user.id, None)
        await bot.send_message(call.from_user.id, "✅ Cancelled.", reply_markup=menu_reply_keyboard())
    elif call.data == "share_confession":
        # New direct menu entry to start confession flow
        user_state[call.from_user.id] = {"mode": "share_confession", "active_conf_id": None}
        try:
            await bot.send_message(call.from_user.id, "✔ Okay — send your confession text now.", reply_markup=menu_reply_keyboard())
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, call.message.message_id, "✔ Okay — send your confession text now.", reply_markup=menu_reply_keyboard())
    await call.answer()

# ------------------ Profile flows ------------------
@dp.callback_query(lambda c: c.data == "prof_edit")
async def prof_edit(call: types.CallbackQuery):
    print("prof_edit triggered")
    p = db_get_user_profile(call.from_user.id)
    emoji = p.get("emoji") or "Not set"
    nickname = p.get("nickname") or "Anonymous"
    bio = p.get("bio") or "NOT SET"
    txt = f"🎨 Profile Customization\n\nProfile Emoji: {emoji}\nNickname: {nickname}\nBio: {bio}"
    await bot.send_message(call.from_user.id, txt, reply_markup=profile_edit_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_back_profile")
async def prof_back_profile(call: types.CallbackQuery):
    print("prof_back_profile triggered")
    txt = render_profile_text(call.from_user.id)
    await bot.send_message(call.from_user.id, txt, reply_markup=profile_main_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_edit_emoji")
async def prof_edit_emoji(call: types.CallbackQuery):
    print("prof_edit_emoji triggered")
    await bot.send_message(call.from_user.id, "Choose your new profile emoji.", reply_markup=emoji_picker_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith("prof_emoji_"))
async def prof_choose_emoji(call: types.CallbackQuery):
    print("prof_choose_emoji triggered:", call.data)
    emoji = call.data.split("_", 2)[2]
    db_set_profile_emoji(call.from_user.id, emoji)
    # Return to edit page with updated profile info
    p = db_get_user_profile(call.from_user.id)
    emoji_disp = p.get("emoji") or "Not set"
    nickname = p.get("nickname") or "Anonymous"
    bio = p.get("bio") or "NOT SET"
    txt = f"🎨 Profile Customization\n\nProfile Emoji: {emoji_disp}\nNickname: {nickname}\nBio: {bio}"
    await bot.send_message(call.from_user.id, "✅ Emoji updated.", reply_markup=profile_edit_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_back_edit")
async def prof_back_edit(call: types.CallbackQuery):
    print("prof_back_edit triggered")
    p = db_get_user_profile(call.from_user.id)
    emoji = p.get("emoji") or "Not set"
    nickname = p.get("nickname") or "Anonymous"
    bio = p.get("bio") or "NOT SET"
    txt = f"🎨 Profile Customization\n\nProfile Emoji: {emoji}\nNickname: {nickname}\nBio: {bio}"
    await bot.send_message(call.from_user.id, txt, reply_markup=profile_edit_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_edit_bio")
async def prof_edit_bio(call: types.CallbackQuery):
    print("prof_edit_bio triggered")
    profile_flow_state[call.from_user.id] = {"await": "bio"}
    await bot.send_message(call.from_user.id, "Please send your new bio (max 250 characters). Send 'remove' to clear your bio.")
    await bot.send_message(call.from_user.id, "Waiting for your bio...")
    await call.answer()

@dp.callback_query(lambda c: c.data == "prof_edit_nick")
async def prof_edit_nick(call: types.CallbackQuery):
    print("prof_edit_nick triggered")
    profile_flow_state[call.from_user.id] = {"await": "nick"}
    await bot.send_message(call.from_user.id, "Please send your new nickname (max 32 alphanumeric characters). Send 'default' to reset to Anonymous.")
    await bot.send_message(call.from_user.id, "Waiting for your nickname...")
    await call.answer()

# ------------------ Handler order: filtered handlers first, catch-all last ------------------

# 1) Profile input handler — ONLY runs when user is in profile flow (filtered)
@dp.message(lambda m: profile_flow_state.get(m.from_user.id))
async def handle_profile_inputs(message: types.Message):
    uid = message.from_user.id
    st = profile_flow_state.get(uid)
    print("handle_profile_inputs triggered:", st, message.text)

    if not st:
        return  # Shouldn't happen due to filter, but safe-guard.

    awaiting = st.get("await")
    txt = (message.text or "").strip()

    if awaiting == "bio":
        if txt.lower() == "remove":
            db_set_profile_bio(uid, None)
            profile_flow_state.pop(uid, None)
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "✅ Bio cleared.")
        elif len(txt) > 250:
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Bio too long. Please send up to 250 characters.")
            return
        else:
            db_set_profile_bio(uid, txt)
            profile_flow_state.pop(uid, None)
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "✅ Bio updated.")
        # Return to edit profile page
        p = db_get_user_profile(uid)
        emoji = p.get("emoji") or "Not set"
        nickname = p.get("nickname") or "Anonymous"
        bio = p.get("bio") or "NOT SET"
        txtp = f"🎨 Profile Customization\n\nProfile Emoji: {emoji}\nNickname: {nickname}\nBio: {bio}"
        await _safe_reply_or_send(message.chat.id, None, txtp, reply_markup=profile_edit_kb())
        return

    if awaiting == "nick":
        if txt.lower() == "default":
            db_set_profile_nickname(uid, None)
            profile_flow_state.pop(uid, None)
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "✅ Nickname reset to Anonymous.")
        else:
            # Basic validation: alphanumeric + spaces, max 32
            if len(txt) > 32:
                await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Nickname too long. Max 32 characters.")
                return
            if not all(ch.isalnum() or ch == " " for ch in txt):
                await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Use only letters, numbers, and spaces.")
                return
            db_set_profile_nickname(uid, txt)
            profile_flow_state.pop(uid, None)
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "✅ Nickname updated.")
        # Return to edit profile page
        p = db_get_user_profile(uid)
        emoji = p.get("emoji") or "Not set"
        nickname = p.get("nickname") or "Anonymous"
        bio = p.get("bio") or "NOT SET"
        txtp = f"🎨 Profile Customization\n\nProfile Emoji: {emoji}\nNickname: {nickname}\nBio: {bio}"
        await _safe_reply_or_send(message.chat.id, None, txtp, reply_markup=profile_edit_kb())
        return

# 2) Reply message handler — ONLY runs when user is in reply flow (filtered)
@dp.message(lambda m: m.from_user.id in user_reply_state)
async def handle_reply(message: types.Message):
    print("handle_reply triggered:", message.text)
    uid = message.from_user.id
    state = user_reply_state.pop(uid, None)
    if not state:
        return

    conf_id = state["confession_id"]
    parent_id = state["parent_comment_id"]
    try:
        supabase.table("comments").insert({
            "confession_id": conf_id,
            "user_id": str(uid),
            "username": message.from_user.username or message.from_user.full_name,
            "text": message.text,
            "parent_comment_id": parent_id
        }).execute()
    except Exception as e:
        print("Failed adding reply:", e, traceback.format_exc())
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Failed to post reply. Try again later.")
        return

    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "✅ Your reply has been added.", reply_markup=menu_reply_keyboard())

# 3) General message handler — catch-all for comments and confessions (must be last)
@dp.message()
async def handle_message(message: types.Message):
    uid = message.from_user.id
    text = message.text or ""
    state = user_state.get(uid, {})
    print("handle_message triggered:", text, state)

    # If user is currently writing a comment (active_conf_id present)
    if state.get("active_conf_id"):
        conf_id = state["active_conf_id"]
        try:
            c_id = db_add_comment(conf_id, str(uid), message.from_user.username or message.from_user.full_name, text)
            print("Comment added:", c_id)
        except Exception as e:
            print("Failed adding comment:", e, traceback.format_exc())
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Failed to post comment. Try again later.")
            user_state.pop(uid, None)
            return

        # Update channel button count
        new_count = db_count_comments(conf_id)
        conf = db_get_confession(conf_id)
        if conf and conf.get("channel_msg_id"):
            try:
                bot_username = (await bot.get_me()).username
                new_kb = build_channel_markup(bot_username, conf_id, new_count)
                await bot.edit_message_reply_markup(TARGET_CHANNEL_ID, conf.get("channel_msg_id"), reply_markup=new_kb)
            except Exception as e:
                print("Failed to update channel markup:", e)
        # respond to user (robust)
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), f"✅ Your comment on Confession #{conf_id} is live!", reply_markup=menu_reply_keyboard())
        user_state.pop(uid, None)
        return

    # Confession modes (including Share Confession from menu)
    if state.get("mode") in ("share_experience", "share_thought", "share_confession"):
        try:
            conf_id = db_add_confession(str(uid), text)
            print("Confession added:", conf_id)
        except Exception as e:
            print("Failed adding confession:", e, traceback.format_exc())
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Failed to submit confession. Try again later.", reply_markup=menu_reply_keyboard())
            user_state.pop(uid, None)
            return

        # send to admin group for review (include author info)
        review_text = (
            f"🛂 *Review New Confession*\n"
            f"👤 Author: {message.from_user.full_name} (ID: {uid})\n"
            f"Confession ID: {conf_id}\n\n"
            f"📝 Content:\n{text}\n\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data=f"admin_approve_{conf_id}"),
             InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject_{conf_id}")]
        ])
        # send review to admin group - robust send
        try:
            await bot.send_message(ADMIN_GROUP_ID, review_text, reply_markup=kb)
        except Exception:
            print("Failed to forward confession to admin group. Check ADMIN_GROUP_ID and bot permissions.")
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Could not forward confession to admin group. Contact admin.", reply_markup=menu_reply_keyboard())
            user_state.pop(uid, None)
            return

        # confirm to user
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "✅ Confession sent for review!", reply_markup=menu_reply_keyboard())
        user_state.pop(uid, None)
        return

    # If message arrives without mode/state, show the quick menu
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
        [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")]
    ])
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "What would you like to do?", reply_markup=kb)

# ---------------- Core bot flows (callbacks): accept terms, choose share type, comments, browse, votes, reports, admin ----------------

# Accept / decline Terms callbacks
@dp.callback_query(lambda c: c.data == "accept_terms" or c.data == "decline_terms")
async def accept_terms_cb(callback: types.CallbackQuery):
    print("accept_terms_cb triggered:", callback.data)
    if callback.data == "decline_terms":
        try:
            await callback.message.edit_text("❌ You declined.")
        except Exception:
            await _safe_reply_or_send(callback.message.chat.id, callback.message.message_id, "❌ You declined.")
        await callback.answer()
        return
    accepted_terms.add(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
        [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")]
    ])
    try:
        await callback.message.edit_text("What are you sharing?", reply_markup=kb)
    except Exception:
        await _safe_reply_or_send(callback.message.chat.id, callback.message.message_id, "What are you sharing?", reply_markup=kb)
    await callback.answer()

# choose type -> prompt to send text
@dp.callback_query(lambda c: c.data in ("share_experience", "share_thought"))
async def choose_type_cb(callback: types.CallbackQuery):
    print("choose_type_cb triggered:", callback.data)
    user_state[callback.from_user.id] = {"mode": callback.data, "active_conf_id": None}
    # send a private message asking for the text
    try:
        await bot.send_message(callback.from_user.id, "✔ Okay — send your text now.", reply_markup=menu_reply_keyboard())
    except Exception:
        # user may not have started direct chat; reply in current chat as fallback
        await _safe_reply_or_send(callback.message.chat.id, callback.message.message_id, "✔ Okay — send your text now.", reply_markup=menu_reply_keyboard())
    await callback.answer()

# Add Comment button (from channel deep link hub or inside bot)
@dp.callback_query(lambda c: c.data.startswith("add_c_"))
async def add_comment_cb(call: types.CallbackQuery):
    print("add_comment_cb triggered:", call.data)
    try:
        conf_id = int(call.data.split("_")[2])
    except Exception:
        await call.answer("Invalid data")
        return
    user_state[call.from_user.id] = {"active_conf_id": conf_id}
    try:
        await bot.send_message(call.from_user.id, "📝 Please type your comment now:")
    except Exception:
        await _safe_reply_or_send(call.message.chat.id, call.message.message_id, "📝 Please type your comment now:")
    await call.answer()

# Replying: reply_{comment_id}_{conf_id}_{page}
@dp.callback_query(lambda c: c.data.startswith("reply_"))
async def reply_cb(call: types.CallbackQuery):
    print("reply_cb triggered:", call.data)
    try:
        _, c_id_s, conf_id_s, page_s = call.data.split("_")
        c_id = int(c_id_s); conf_id = int(conf_id_s); page = int(page_s)
    except Exception:
        await call.answer("Invalid reply data")
        return
    user_reply_state[call.from_user.id] = {"confession_id": conf_id, "parent_comment_id": c_id, "page": page}
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_reply")]])
    prompt = "📝 Type your reply to that comment:"
    try:
        await bot.send_message(call.from_user.id, prompt, reply_markup=kb)
    except Exception:
        await _safe_reply_or_send(call.message.chat.id, call.message.message_id, prompt, reply_markup=kb)
    await call.answer()

# Cancel reply
@dp.callback_query(lambda c: c.data == "cancel_reply")
async def cancel_reply_cb(call: types.CallbackQuery):
    print("cancel_reply_cb triggered")
    user_reply_state.pop(call.from_user.id, None)
    await call.answer("Reply cancelled.")
    try:
        await bot.send_message(call.from_user.id, "❌ Reply cancelled.")
    except Exception:
        await _safe_reply_or_send(call.message.chat.id, call.message.message_id, "❌ Reply cancelled.")

# Browse comments: browse_{conf_id}_{page}
@dp.callback_query(lambda c: c.data.startswith("browse_"))
async def browse_cb(call: types.CallbackQuery):
    print("browse_cb triggered:", call.data)
    try:
        _, conf_id_s, page_s = call.data.split("_")
        conf_id = int(conf_id_s); page = int(page_s)
    except Exception:
        await call.answer("Invalid data")
        return

    comments = db_get_comments(conf_id)
    # Group replies by parent
    replies_map: Dict[int, List[dict]] = {}
    for c in comments:
        pid = c.get("parent_comment_id")
        if pid:
            replies_map.setdefault(int(pid), []).append(c)

    # Top-level only for pagination
    top_level = [c for c in comments if not c.get("parent_comment_id")]
    per_page = 10  # 10 top-level comments per page
    total = len(top_level)
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page-1)*per_page
    chunk = top_level[start:start+per_page]

    if not chunk:
        try:
            await bot.send_message(call.from_user.id, "No comments yet.")
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, None, "No comments yet.")
        await call.answer()
        return

    # Show each top-level comment + its replies
    for c in chunk:
        c_id = int(c["id"])
        c_text = c.get("text", "")
        likes, dislikes = db_get_vote_counts(c_id)
        txt = f"💬 {c_text}\n👤 *Anonymous*"
        kb = comment_vote_kb(c_id, likes, dislikes, conf_id, page)
        try:
            await bot.send_message(call.from_user.id, txt, reply_markup=kb)
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, None, txt, reply_markup=kb)

        for r in replies_map.get(c_id, []):
            r_id = int(r["id"])
            r_text = r.get("text", "")
            likes_r, dislikes_r = db_get_vote_counts(r_id)
            parent_preview = c_text[:50] + ("..." if len(c_text) > 50 else "")
            reply_txt = f"    ↪️ Reply to \"{parent_preview}\":\n    {r_text}\n    👤 *Anonymous*"
            kb_r = comment_vote_kb(r_id, likes_r, dislikes_r, conf_id, page)
            try:
                await bot.send_message(call.from_user.id, reply_txt, reply_markup=kb_r)
            except Exception:
                await _safe_reply_or_send(call.message.chat.id, None, reply_txt, reply_markup=kb_r)

    nav_kb = pagination_kb(conf_id, page, total_pages)
    try:
        await bot.send_message(call.from_user.id, f"Displaying page {page}/{total_pages}. Total {total} Comments", reply_markup=nav_kb)
    except Exception:
        await _safe_reply_or_send(call.message.chat.id, None, f"Displaying page {page}/{total_pages}. Total {total} Comments", reply_markup=nav_kb)
    await call.answer()

# Voting: vote_{comment_id}_{type}_{conf_id}_{page}
@dp.callback_query(lambda c: c.data.startswith("vote_"))
async def vote_cb(call: types.CallbackQuery):
    print("vote_cb triggered:", call.data)
    parts = call.data.split("_")
    if len(parts) < 5:
        await call.answer("Invalid vote")
        return
    c_id = int(parts[1]); vtype = parts[2]; conf_id = int(parts[3]); page = int(parts[4])
    user_id = str(call.from_user.id)

    try:
        existing = supabase.table("votes").select("*").eq("comment_id", c_id).eq("user_id", user_id).execute()
        want = 1 if vtype == "up" else -1

        if existing.data:
            cur = existing.data[0]
            if int(cur.get("vote", 0)) == want:
                db_delete_vote(user_id, c_id)
            else:
                db_upsert_vote(user_id, c_id, want)
        else:
            db_upsert_vote(user_id, c_id, want)
    except Exception:
        pass

    likes, dislikes = db_get_vote_counts(c_id)
    new_kb = comment_vote_kb(c_id, likes, dislikes, conf_id, page)
    try:
        await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_kb)
    except Exception:
        pass
    await call.answer("Vote recorded!")

# Reporting: report_{comment_id}_{conf_id}
@dp.callback_query(lambda c: c.data.startswith("report_"))
async def report_cb(call: types.CallbackQuery):
    print("report_cb triggered:", call.data)
    try:
        _, c_id_s, conf_id_s = call.data.split("_")
        c_id = int(c_id_s); conf_id = int(conf_id_s)
    except Exception:
        await call.answer("Invalid report data")
        return
    user_state[call.from_user.id] = {"report_c_id": c_id, "report_conf_id": conf_id}
    reasons = ["Violence", "Racism", "Sexual Harassment", "Hate Speech", "Spam/Scam", "Other"]
    rows = []
    for i in range(0, len(reasons), 2):
        row = []
        row.append(InlineKeyboardButton(text=reasons[i], callback_data=f"reason_{reasons[i].replace(' ','_')}"))
        if i+1 < len(reasons):
            row.append(InlineKeyboardButton(text=reasons[i+1], callback_data=f"reason_{reasons[i+1].replace(' ','_')}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="noop")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await bot.send_message(call.from_user.id, "🚨 *What is wrong with this comment?* (Your report is anonymous)", reply_markup=kb)
    except Exception:
        await _safe_reply_or_send(call.message.chat.id, None, "🚨 *What is wrong with this comment?* (Your report is anonymous)", reply_markup=kb)
    await call.answer()

# Reason selected -> submit report
@dp.callback_query(lambda c: c.data.startswith("reason_"))
async def reason_cb(call: types.CallbackQuery):
    print("reason_cb triggered:", call.data)
    reason_raw = call.data.split("_",1)[1]
    reason = reason_raw.replace("_", " ")
    st = user_state.get(call.from_user.id, {})
    c_id = st.get("report_c_id")
    conf_id = st.get("report_conf_id")
    if not c_id:
        await call.answer("Error: comment ID lost.")
        return
    ok = db_add_report(c_id, str(call.from_user.id), reason)
    if not ok:
        try:
            await bot.send_message(call.from_user.id, "🚫 You already reported this comment.")
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, None, "🚫 You already reported this comment.")
        await call.answer()
        return

    comment = db_get_comment(c_id) or {}
    conf = db_get_confession(conf_id) or {}
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Delete Comment", callback_data=f"admin_del_c_{c_id}_{conf_id}"),
         InlineKeyboardButton(text="✅ Dismiss Report", callback_data=f"admin_dis_r_{c_id}")]
    ])
    report_msg = (
        f"🚨 *NEW REPORT* on Comment ID *#{c_id}* (Confession #{conf_id}).\n\n"
        f"*Confession:* {conf.get('text')}\n\n"
        f"*Comment:* {comment.get('text')}\n"
        f"*Author:* {comment.get('username')} (ID: {comment.get('user_id')})\n\n"
        f"*Reason:* {reason}"
    )
    try:
        await bot.send_message(ADMIN_GROUP_ID, report_msg, reply_markup=admin_kb)
        await bot.send_message(call.from_user.id, f"✅ Report submitted successfully for reason: *{reason}*")
    except Exception:
        print("Failed to send report to admins")
        await _safe_reply_or_send(call.message.chat.id, None, f"✅ Report submitted successfully for reason: {reason}")
    user_state.pop(call.from_user.id, None)
    await call.answer()

# Admin delete comment: admin_del_c_{c_id}_{conf_id}
@dp.callback_query(lambda c: c.data.startswith("admin_del_c_"))
async def admin_delete_comment_cb(call: types.CallbackQuery):
    print("admin_delete_comment_cb triggered:", call.data)
    parts = call.data.split("_")
    try:
        c_id = int(parts[3]); conf_id = int(parts[4])
    except Exception:
        await call.answer("Invalid data")
        return
    db_delete_comment(c_id)
    new_count = db_count_comments(conf_id)
    conf = db_get_confession(conf_id)
    chan_msg_id = conf.get("channel_msg_id") if conf else None
    if chan_msg_id:
        try:
            bot_username = (await bot.get_me()).username
            new_kb = build_channel_markup(bot_username, conf_id, new_count)
            await bot.edit_message_reply_markup(TARGET_CHANNEL_ID, chan_msg_id, reply_markup=new_kb)
        except Exception as e:
            print("Failed update channel markup after admin delete:", e)
    try:
        await bot.edit_message_text(f"🗑️ Comment ID *#{c_id}* deleted. Channel count updated.", call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    await call.answer()

# Admin dismiss report: admin_dis_r_{c_id}
@dp.callback_query(lambda c: c.data.startswith("admin_dis_r_"))
async def admin_dismiss_report_cb(call: types.CallbackQuery):
    print("admin_dismiss_report_cb triggered:", call.data)
    parts = call.data.split("_")
    try:
        c_id = int(parts[3])
    except Exception:
        await call.answer("Invalid data")
        return
    try:
        supabase.table("reports").update({"reason": "dismissed"}).eq("comment_id", c_id).execute()
    except Exception:
        pass
    try:
        await bot.edit_message_text(f"✅ Reports for Comment ID *#{c_id}* dismissed.", call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    await call.answer()

# Admin approve/reject from review message: admin_approve_{id} / admin_reject_{id}
@dp.callback_query(lambda c: c.data.startswith("admin_approve_") or c.data.startswith("admin_reject_"))
async def admin_review_cb(call: types.CallbackQuery):
    print("admin_review_cb triggered:", call.data)
    parts = call.data.split("_")
    action = parts[1]
    conf_id = int(parts[2])
    current_text = call.message.text or ""
    if "📝 Content:" in current_text:
        try:
            final_text = current_text.split("📝 Content:")[1].strip()
        except Exception:
            row = db_get_confession(conf_id)
            final_text = (row or {}).get("text","")
    else:
        row = db_get_confession(conf_id)
        final_text = (row or {}).get("text","")

    if action == "reject":
        db_set_confession_rejected(conf_id)
        try:
            await bot.edit_message_text(f"❌ Rejected.\n\nOriginal: {final_text}", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        await call.answer()
        return

    # Approve -> publish to channel
    post_text = f"*Confession #{conf_id}*\n\n{final_text}\n\n#Confession"
    try:
        sent = await bot.send_message(TARGET_CHANNEL_ID, post_text, reply_markup=build_channel_markup((await bot.get_me()).username, conf_id, db_count_comments(conf_id)))
        db_set_confession_published(conf_id, sent.message_id)
        try:
            await bot.edit_message_text(f"✅ Confession #{conf_id} Published.", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
    except Exception as e:
        print("Failed to publish confession to channel:", e)
        try:
            await bot.send_message(call.message.chat.id, f"❌ Failed to publish confession #{conf_id}.")
        except Exception:
            pass
    await call.answer()

# ------------------ General callback NOOP and guard ------------------
@dp.callback_query(lambda c: c.data == "noop")
async def noop_cb(call: types.CallbackQuery):
    print("noop_cb triggered")
    await call.answer()

# ------------------ Webhook route (FastAPI) ------------------
@app.post("/")
async def webhook(request: Request):
    # Log and parse incoming JSON
    data = await request.json()
    print("Webhook received:", data)

    # Validate Update
    try:
        update = types.Update(**data)
    except Exception:
        print("Webhook: invalid update payload")
        return {"ok": False, "error": "invalid update"}

    # Feed update to aiogram
    try:
        await dp.feed_update(bot, update)
    except Exception as e:
        # Log - do not let exceptions kill the server
        print("Error while feeding update:", e, traceback.format_exc())
        # swallow errors and return ok=false so Telegram backs off
        return {"ok": False, "error": "handler error"}
    return {"ok": True}

# ------------------ Health endpoints ------------------
@app.get("/")
def root():
    return {"status": "running"}

@app.get("/render/health")
def health():
    return {"status": "ok"}

# ------------------ Notes & Tips ------------------
"""
Deployment tips (for Render):
- Use Start Command: gunicorn main:app --worker-class uvicorn.workers.UvicornWorker --workers 1 --bind 0.0.0.0:$PORT
- Ensure BOT_TOKEN, ADMIN_GROUP_ID, TARGET_CHANNEL_ID, SUPABASE_URL, SUPABASE_KEY env vars are set.
- Make sure the bot is admin in the target channel and can post messages.
- ADMIN_GROUP_ID should be a chat ID where the bot can post review messages (group or channel with appropriate permissions).

Operational logs to watch:
- "Webhook received:" should print raw JSON; confirms Telegram updates are hitting your app.
- "cmd_start triggered:" confirms /start is matched by the command filter.
- "handle_message triggered:" with the user_state dict printed — confirms whether comment/confession state is set.
- "add_comment_cb triggered:" confirms the callback after Add Comment.
- "choose_type_cb triggered:" confirms selection of share_experience or share_thought.
- "menu_inline_commands triggered:" shows when Share Confession is started from the Menu.
- "handle_profile_inputs triggered:" only when user is in profile flow (bio or nick).

State model:
- accepted_terms: set of user IDs who accepted terms (not mixed into user_state).
- user_state[uid]: dict with keys
    - mode: "share_experience" | "share_thought" | "share_confession"
    - active_conf_id: int (when adding a comment)
    - report_c_id, report_conf_id: for reporting flow
- user_reply_state[uid]: dict with keys
    - confession_id, parent_comment_id, page
- profile_flow_state[uid]: dict with keys
    - await: "bio"|"nick"

Handler order rationale:
- Profile input handler is filtered via lambda m: profile_flow_state.get(m.from_user.id), so it only runs in profile flow and does not consume general messages.
- Reply handler is filtered via lambda m: m.from_user.id in user_reply_state, so it only runs during reply flow and does not consume general messages.
- Catch-all message handler comes last and processes comments/confessions as in the original working version.

Menu simplification:
- Inline menu (shown by typing "Menu" in chat) contains:
    1) 📝 Share Confession
    2) /profile
    3) /rules
    4) /cancel
- /start still shows only Share Experience / Share Thought after terms, per your specified behavior.
"""

# ------------------ Main entrypoint for local runs ------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

