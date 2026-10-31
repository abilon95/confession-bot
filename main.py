#!/usr/bin/env python3
"""
MuConfessionsBot - clean rebuild (Render + Supabase + Telegram webhook)

Render start command (keep exactly ONE worker, state lives in memory):
    gunicorn main:app --worker-class uvicorn.workers.UvicornWorker --workers 1 --bind 0.0.0.0:$PORT

Required env vars:
    BOT_TOKEN, ADMIN_GROUP_ID, TARGET_CHANNEL_ID, SUPABASE_URL, SUPABASE_KEY, PORT
Optional env vars:
    WEBHOOK_SECRET  - letters/numbers/_/- only. If set, Telegram must send it back
                      (the bot registers it automatically on startup).
    WEBHOOK_URL     - public URL of the service. Defaults to RENDER_EXTERNAL_URL,
                      which Render sets automatically. The webhook is re-registered
                      on every startup, so a lost webhook fixes itself.
"""

import math
import os
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client

# =====================================================================
# Environment
# =====================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0"))
TARGET_CHANNEL_ID = int(os.environ.get("TARGET_CHANNEL_ID", "0"))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
PORT = int(os.environ.get("PORT", "5000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or None
PUBLIC_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY are required")

# =====================================================================
# Clients
# =====================================================================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="share_confession", description="💬 Share a new confession"),
                BotCommand(command="profile", description="View your profile and history"),
                BotCommand(command="rules", description="View the bot's rules"),
                BotCommand(command="cancel", description="Cancel what you are doing"),
            ]
        )
    except Exception as e:
        print("Startup: set_my_commands failed:", e)

    if PUBLIC_URL:
        try:
            await bot.set_webhook(
                url=PUBLIC_URL.rstrip("/") + "/",
                secret_token=WEBHOOK_SECRET,
                allowed_updates=dp.resolve_used_update_types(),
            )
            print("Startup: webhook registered ->", PUBLIC_URL)
        except Exception as e:
            print("Startup: set_webhook failed:", e)
    else:
        print("Startup: no WEBHOOK_URL / RENDER_EXTERNAL_URL, webhook not touched.")

    print("Startup: app is ready.")
    yield
    await bot.session.close()


app = FastAPI(lifespan=lifespan)

# =====================================================================
# In-memory state (lost on restart/deploy - see "move to Supabase" roadmap)
# =====================================================================
user_state: Dict[int, Dict[str, Any]] = {}
user_reply_state: Dict[int, Dict[str, Any]] = {}
profile_flow_state: Dict[int, Dict[str, Any]] = {}
accepted_terms: set = set()

SHARE_MODES = ("share_confession", "share_experience", "share_thought")
SHARE_LABELS = {
    "share_confession": "Confession",
    "share_experience": "Experience",
    "share_thought": "Thought",
}


def clear_state(uid: int) -> None:
    user_state.pop(uid, None)
    user_reply_state.pop(uid, None)
    profile_flow_state.pop(uid, None)


_bot_username: Optional[str] = None


async def get_bot_username() -> str:
    global _bot_username
    if not _bot_username:
        _bot_username = (await bot.get_me()).username
    return _bot_username


# =====================================================================
# Keyboards
# =====================================================================
def build_channel_markup(bot_username: str, conf_id: int, count: int) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=conf_{conf_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"💬 Add/View Comment ({count})", url=url)]]
    )


def hub_keyboard(conf_id: int, total_comments: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add Comment", callback_data=f"add_c_{conf_id}")],
            [InlineKeyboardButton(text=f"📂 Browse Comments ({total_comments})", callback_data=f"browse_{conf_id}_1")],
        ]
    )


def comment_vote_kb(comment_id: int, likes: int, dislikes: int, conf_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"👍 {likes}", callback_data=f"vote_{comment_id}_up_{conf_id}_{page}"),
                InlineKeyboardButton(text=f"👎 {dislikes}", callback_data=f"vote_{comment_id}_dw_{conf_id}_{page}"),
                InlineKeyboardButton(text="🚩", callback_data=f"report_{comment_id}_{conf_id}"),
            ],
            [InlineKeyboardButton(text="↪️ Reply", callback_data=f"reply_{comment_id}_{conf_id}_{page}")],
        ]
    )


def pagination_kb(conf_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="⬅ Prev", callback_data=f"browse_{conf_id}_{page - 1}"))
    row.append(InlineKeyboardButton(text=f"Page {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        row.append(InlineKeyboardButton(text="Next ➡", callback_data=f"browse_{conf_id}_{page + 1}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[row, [InlineKeyboardButton(text="➕ Add Comment", callback_data=f"add_c_{conf_id}")]]
    )


def menu_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Menu")]], resize_keyboard=True)


def menu_commands_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Share Confession", callback_data="share_confession")],
            [InlineKeyboardButton(text="/profile", callback_data="cmd_profile")],
            [InlineKeyboardButton(text="/rules", callback_data="cmd_rules")],
            [InlineKeyboardButton(text="/cancel", callback_data="cmd_cancel")],
        ]
    )


def share_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
            [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")],
        ]
    )


PROFILE_EMOJIS = [
    "🗣", "👻", "🥸", "🧐", "😇", "🤠",
    "😎", "😜", "🦋", "👁", "☠️", "🐼",
    "🐱", "🐶", "🦊", "🦄", "🐢", "🤡",
    "🤖", "👽", "👀", "👤", "🤵‍♂️", "🤵‍♀️",
    "🥷", "🧚‍♀️", "🙎‍♀️", "🙎‍♂️", "👩‍🦱", "🧑‍🦱",
]
GENDER_OPTIONS = ["Male", "Female", "Prefer not to say"]


def profile_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎨 Edit Profile", callback_data="prof_edit")],
            [InlineKeyboardButton(text="📝 My Confessions", callback_data="prof_my_confessions")],
            [InlineKeyboardButton(text="💬 My Comments", callback_data="prof_my_comments")],
        ]
    )


def profile_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎨 Change Profile Emoji", callback_data="prof_edit_emoji")],
            [InlineKeyboardButton(text="✏️ Change Nickname", callback_data="prof_edit_nick")],
            [InlineKeyboardButton(text="⚧ Change Gender", callback_data="prof_edit_gender")],
            [InlineKeyboardButton(text="📝 Set/Update Bio", callback_data="prof_edit_bio")],
            [InlineKeyboardButton(text="🔙 Back to Profile", callback_data="prof_back_profile")],
        ]
    )


def emoji_picker_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, e in enumerate(PROFILE_EMOJIS, start=1):
        row.append(InlineKeyboardButton(text=e, callback_data=f"prof_emoji_{e}"))
        if i % 6 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Back to Edit Profile", callback_data="prof_back_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gender_picker_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=g, callback_data=f"prof_gender_{i}")] for i, g in enumerate(GENDER_OPTIONS)]
    rows.append([InlineKeyboardButton(text="🗑 Clear gender", callback_data="prof_gender_x")])
    rows.append([InlineKeyboardButton(text="🔙 Back to Edit Profile", callback_data="prof_back_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =====================================================================
# Database helpers
# =====================================================================
# ---- confessions ----
def db_add_confession(user_id: str, text: str) -> int:
    try:
        res = supabase.table("confessions").insert({"user_id": user_id, "text": text, "is_approved": False}).execute()
    except Exception as e:
        # schema without is_approved? retry with the minimum payload
        print("Insert confession failed, retrying reduced payload:", e)
        res = supabase.table("confessions").insert({"user_id": user_id, "text": text}).execute()
    return int(res.data[0]["id"])


def db_get_confession(conf_id: int) -> Optional[dict]:
    r = supabase.table("confessions").select("*").eq("id", conf_id).execute()
    return r.data[0] if r.data else None


def db_set_confession_published(conf_id: int, channel_msg_id: int) -> None:
    try:
        supabase.table("confessions").update({"is_approved": True, "channel_msg_id": channel_msg_id}).eq("id", conf_id).execute()
    except Exception as e:
        print("Publish update failed, retrying without is_approved:", e)
        supabase.table("confessions").update({"channel_msg_id": channel_msg_id}).eq("id", conf_id).execute()


def db_set_confession_rejected(conf_id: int) -> None:
    try:
        supabase.table("confessions").update({"is_approved": False}).eq("id", conf_id).execute()
    except Exception as e:
        print("Reject update failed:", e)


def db_get_confession_author(conf_id: int) -> Optional[dict]:
    conf = db_get_confession(conf_id)
    if not conf:
        return None
    uid = conf.get("user_id")
    p = db_get_user_profile(int(uid), create=False) if uid else {}
    return {
        "user_id": uid,
        "text": conf.get("text"),
        "emoji": p.get("emoji"),
        "nickname": p.get("nickname"),
        "gender": p.get("gender"),
        "bio": p.get("bio"),
    }


# ---- comments ----
def db_add_comment(confession_id: int, user_id: str, username: str, text: str,
                   parent_comment_id: Optional[int] = None) -> int:
    payload: Dict[str, Any] = {
        "confession_id": confession_id,
        "user_id": user_id,
        "username": username,
        "text": text,
    }
    if parent_comment_id:
        payload["parent_comment_id"] = parent_comment_id
    res = supabase.table("comments").insert(payload).execute()
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


def db_delete_comment(comment_id: int) -> None:
    """
    Hard-delete a comment and everything hanging off it.
    Order matters (children first) and errors are NOT swallowed: the old version
    deleted the comment first, hit a foreign-key error from replies/votes/reports,
    ignored it, and still reported success.
    """
    # 1. collect every descendant reply, level by level
    seen = {comment_id}
    levels: List[List[int]] = []
    frontier = [comment_id]
    while frontier:
        rows = supabase.table("comments").select("id").in_("parent_comment_id", frontier).execute().data or []
        nxt = [int(r["id"]) for r in rows if int(r["id"]) not in seen]
        if not nxt:
            break
        seen.update(nxt)
        levels.append(nxt)
        frontier = nxt
    all_ids = list(seen)

    # 2. votes and reports pointing at any of them
    supabase.table("votes").delete().in_("comment_id", all_ids).execute()
    supabase.table("reports").delete().in_("comment_id", all_ids).execute()

    # 3. replies, deepest level first, then the comment itself
    for level in reversed(levels):
        supabase.table("comments").delete().in_("id", level).execute()
    supabase.table("comments").delete().eq("id", comment_id).execute()

    # 4. verify - RLS can make a delete "succeed" while removing 0 rows
    if db_get_comment(comment_id) is not None:
        raise RuntimeError("Comment still exists after delete (check RLS policies / foreign keys in Supabase)")


# ---- votes ----
def db_toggle_vote(user_id: str, comment_id: int, want: int) -> None:
    existing = supabase.table("votes").select("*").eq("comment_id", comment_id).eq("user_id", user_id).execute().data
    if existing:
        if int(existing[0].get("vote", 0)) == want:
            supabase.table("votes").delete().eq("comment_id", comment_id).eq("user_id", user_id).execute()
        else:
            supabase.table("votes").update({"vote": want}).eq("comment_id", comment_id).eq("user_id", user_id).execute()
    else:
        supabase.table("votes").insert({"user_id": user_id, "comment_id": comment_id, "vote": want}).execute()


def db_get_vote_counts(comment_id: int) -> Tuple[int, int]:
    try:
        likes = supabase.table("votes").select("comment_id", count="exact").eq("comment_id", comment_id).eq("vote", 1).execute().count or 0
        dislikes = supabase.table("votes").select("comment_id", count="exact").eq("comment_id", comment_id).eq("vote", -1).execute().count or 0
        return int(likes), int(dislikes)
    except Exception as e:
        print("Vote count failed:", e)
        return 0, 0


# ---- reports ----
def db_add_report(comment_id: int, reporting_user_id: str, reason: str) -> str:
    """Returns 'ok', 'dup' or 'error'."""
    try:
        existing = supabase.table("reports").select("*").eq("comment_id", comment_id).eq("user_id", reporting_user_id).execute()
        if existing.data:
            return "dup"
        supabase.table("reports").insert(
            {"comment_id": comment_id, "user_id": reporting_user_id, "reason": reason}
        ).execute()
        return "ok"
    except Exception as e:
        print("Report insert failed:", e)
        return "error"


# ---- profiles ----
def db_get_user_profile(user_id: int, create: bool = True) -> dict:
    empty = {"emoji": None, "nickname": None, "bio": None, "gender": None}
    try:
        r = supabase.table("profiles").select("*").eq("user_id", str(user_id)).limit(1).execute()
        if r.data:
            row = r.data[0]
            return {k: row.get(k) for k in empty}
        if create:
            supabase.table("profiles").insert({"user_id": str(user_id)}).execute()
    except Exception as e:
        print("Profile fetch failed:", e)
    return empty


def _update_profile(user_id: int, field: str, value: Optional[str]) -> bool:
    try:
        db_get_user_profile(user_id)  # make sure the row exists
        supabase.table("profiles").update({field: value}).eq("user_id", str(user_id)).execute()
        return True
    except Exception as e:
        print(f"Profile update ({field}) failed:", e)
        return False


def db_set_profile_emoji(user_id: int, emoji: Optional[str]) -> bool:
    return _update_profile(user_id, "emoji", emoji)


def db_set_profile_bio(user_id: int, bio: Optional[str]) -> bool:
    return _update_profile(user_id, "bio", bio)


def db_set_profile_nickname(user_id: int, nickname: Optional[str]) -> bool:
    return _update_profile(user_id, "nickname", nickname)


def db_set_profile_gender(user_id: int, gender: Optional[str]) -> bool:
    return _update_profile(user_id, "gender", gender)


# ---- profile text builders ----
def render_profile_text(user_id: int) -> str:
    p = db_get_user_profile(user_id)
    return (
        f"👤 {p.get('emoji') or '🙂'} {p.get('nickname') or 'Anonymous'}\n\n"
        f"⚧ Gender: {p.get('gender') or 'NOT SET'}\n\n"
        f"📝 Bio: {p.get('bio') or 'NOT SET'}"
    )


def render_edit_text(user_id: int) -> str:
    p = db_get_user_profile(user_id)
    return (
        "🎨 Profile Customization\n\n"
        f"Profile Emoji: {p.get('emoji') or 'Not set'}\n"
        f"Nickname: {p.get('nickname') or 'Anonymous'}\n"
        f"Gender: {p.get('gender') or 'NOT SET'}\n"
        f"Bio: {p.get('bio') or 'NOT SET'}"
    )


def comment_identity(p: dict) -> str:
    """Customized profile -> emoji/nickname/gender/bio, otherwise Anonymous."""
    if not any(p.get(k) for k in ("emoji", "nickname", "bio", "gender")):
        return "👤 Anonymous"
    lines = [f"{p.get('emoji') or '🙂'} {p.get('nickname') or 'Anonymous'}"]
    if p.get("gender"):
        lines.append(f"⚧ {p['gender']}")
    if p.get("bio"):
        lines.append(f"📝 {p['bio']}")
    return "\n".join(lines)


def display_name_for(uid: int) -> str:
    p = db_get_user_profile(uid)
    return f"{p.get('emoji') or '🙂'} {p.get('nickname') or 'Anonymous'}"


# =====================================================================
# Small helpers
# =====================================================================
async def dm(call: types.CallbackQuery, text: str, **kwargs):
    """Send to the user privately; fall back to the chat the button was in."""
    try:
        return await bot.send_message(call.from_user.id, text, **kwargs)
    except Exception:
        if call.message:
            return await bot.send_message(call.message.chat.id, text, **kwargs)


def from_admin_chat(call: types.CallbackQuery) -> bool:
    return bool(call.message) and call.message.chat.id == ADMIN_GROUP_ID


async def refresh_channel_count(conf_id: int) -> None:
    try:
        conf = db_get_confession(conf_id)
        if conf and conf.get("channel_msg_id"):
            kb = build_channel_markup(await get_bot_username(), conf_id, db_count_comments(conf_id))
            await bot.edit_message_reply_markup(
                chat_id=TARGET_CHANNEL_ID, message_id=int(conf["channel_msg_id"]), reply_markup=kb
            )
    except Exception as e:
        print("Channel counter update failed:", e)


RULES_TEXT = (
    "RULES:\n"
    "1. Be respectful. No hate speech, harassment, or threats.\n"
    "2. No doxxing or sharing personal information.\n"
    "3. Report inappropriate content with 🚩.\n"
    "4. Admins may remove content that violates rules."
)

# =====================================================================
# Commands
# =====================================================================
@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    clear_state(uid)
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else None

    # Deep link: /start conf_<id>
    if payload and payload.startswith("conf_"):
        try:
            conf_id = int(payload.split("_", 1)[1])
        except Exception:
            await message.answer("Invalid confession link.", reply_markup=menu_reply_keyboard())
            return
        conf = db_get_confession(conf_id)
        if not conf or not conf.get("is_approved"):
            await message.answer("Confession not found or not published.", reply_markup=menu_reply_keyboard())
            return
        hub_text = f"Confession #{conf_id}\n\n{conf.get('text')}\n\nSelect an option below:"
        await message.answer(hub_text, reply_markup=hub_keyboard(conf_id, db_count_comments(conf_id)))
        return

    if uid not in accepted_terms:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Accept Terms", callback_data="accept_terms")],
                [InlineKeyboardButton(text="❌ Decline", callback_data="decline_terms")],
            ]
        )
        await message.answer(
            "📜 Terms & Conditions\n\n"
            "1. Admins will review your message.\n"
            "2. Approved messages are posted anonymously.\n"
            "3. Any comments containing inappropriate content will be removed.\n\n"
            "Click Accept to continue.",
            reply_markup=kb,
        )
    else:
        await message.answer("What do you want to share?", reply_markup=share_type_kb())


@dp.message(Command("share_confession"), F.chat.type == "private")
async def cmd_share_confession(message: types.Message):
    clear_state(message.from_user.id)
    user_state[message.from_user.id] = {"mode": "share_confession", "active_conf_id": None}
    await message.answer("📝 Okay — send your confession text now.", reply_markup=menu_reply_keyboard())


@dp.message(Command("profile"), F.chat.type == "private")
async def cmd_profile(message: types.Message):
    await message.answer(render_profile_text(message.from_user.id), reply_markup=profile_main_kb())


@dp.message(Command("rules"), F.chat.type == "private")
async def cmd_rules(message: types.Message):
    await message.answer(RULES_TEXT, reply_markup=menu_reply_keyboard())


@dp.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel(message: types.Message):
    clear_state(message.from_user.id)
    await message.answer("✅ Cancelled.", reply_markup=menu_reply_keyboard())


@dp.message(Command("trace"))
async def cmd_trace(message: types.Message):
    """Admin group only: /trace <confession_id> -> who wrote it."""
    if message.chat.id != ADMIN_GROUP_ID:
        return
    args = (message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Usage: /trace <confession_id>")
        return
    info = db_get_confession_author(int(args[1]))
    if not info:
        await message.answer("Confession not found.")
        return
    await message.answer(
        f"🔎 Confession #{args[1]}\n"
        f"User ID: {info['user_id']}\n"
        f"Profile: {info.get('emoji') or '🙂'} {info.get('nickname') or 'Anonymous'}\n"
        f"Gender: {info.get('gender') or '-'}\n"
        f"Bio: {info.get('bio') or '-'}\n\n"
        f"Text:\n{info.get('text')}"
    )


@dp.message(F.chat.type == "private", lambda m: (m.text or "").strip().lower() == "menu")
async def show_menu(message: types.Message):
    await message.answer(
        "Menu:\n📝 Share Confession • /profile • /rules • /cancel",
        reply_markup=menu_commands_inline(),
    )


# =====================================================================
# Menu / terms / share-type callbacks
# =====================================================================
@dp.callback_query(F.data.in_({"cmd_profile", "cmd_rules", "cmd_cancel", "share_confession"}))
async def menu_inline_commands(call: types.CallbackQuery):
    uid = call.from_user.id
    if call.data == "cmd_profile":
        await dm(call, render_profile_text(uid), reply_markup=profile_main_kb())
    elif call.data == "cmd_rules":
        await dm(call, RULES_TEXT, reply_markup=menu_reply_keyboard())
    elif call.data == "cmd_cancel":
        clear_state(uid)
        await dm(call, "✅ Cancelled.", reply_markup=menu_reply_keyboard())
    elif call.data == "share_confession":
        clear_state(uid)
        user_state[uid] = {"mode": "share_confession", "active_conf_id": None}
        await dm(call, "✔ Okay — send your confession text now.", reply_markup=menu_reply_keyboard())
    await call.answer()


@dp.callback_query(F.data.in_({"accept_terms", "decline_terms"}))
async def accept_terms_cb(call: types.CallbackQuery):
    if call.data == "decline_terms":
        try:
            await call.message.edit_text("❌ You declined.")
        except Exception:
            await dm(call, "❌ You declined.")
        await call.answer()
        return
    accepted_terms.add(call.from_user.id)
    try:
        await call.message.edit_text("What are you sharing?", reply_markup=share_type_kb())
    except Exception:
        await dm(call, "What are you sharing?", reply_markup=share_type_kb())
    await call.answer()


@dp.callback_query(F.data.in_({"share_experience", "share_thought"}))
async def choose_type_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    clear_state(uid)
    user_state[uid] = {"mode": call.data, "active_conf_id": None}
    await dm(call, "✔ Okay — send your text now.", reply_markup=menu_reply_keyboard())
    await call.answer()


# =====================================================================
# Profile callbacks
# =====================================================================
@dp.callback_query(F.data == "prof_edit")
async def prof_edit(call: types.CallbackQuery):
    await dm(call, render_edit_text(call.from_user.id), reply_markup=profile_edit_kb())
    await call.answer()


@dp.callback_query(F.data == "prof_back_profile")
async def prof_back_profile(call: types.CallbackQuery):
    await dm(call, render_profile_text(call.from_user.id), reply_markup=profile_main_kb())
    await call.answer()


@dp.callback_query(F.data == "prof_back_edit")
async def prof_back_edit(call: types.CallbackQuery):
    await dm(call, render_edit_text(call.from_user.id), reply_markup=profile_edit_kb())
    await call.answer()


@dp.callback_query(F.data == "prof_edit_emoji")
async def prof_edit_emoji(call: types.CallbackQuery):
    await dm(call, "Choose your new profile emoji.", reply_markup=emoji_picker_kb())
    await call.answer()


@dp.callback_query(F.data.startswith("prof_emoji_"))
async def prof_choose_emoji(call: types.CallbackQuery):
    emoji = call.data.split("_", 2)[2]
    ok = db_set_profile_emoji(call.from_user.id, emoji)
    await dm(call, "✅ Emoji updated." if ok else "❌ Could not save. Try again later.", reply_markup=profile_edit_kb())
    await call.answer()


@dp.callback_query(F.data == "prof_edit_gender")
async def prof_edit_gender(call: types.CallbackQuery):
    await dm(call, "Choose your gender:", reply_markup=gender_picker_kb())
    await call.answer()


@dp.callback_query(F.data.startswith("prof_gender_"))
async def prof_choose_gender(call: types.CallbackQuery):
    key = call.data.split("_", 2)[2]
    if key == "x":
        value = None
    else:
        try:
            value = GENDER_OPTIONS[int(key)]
        except Exception:
            await call.answer("Invalid option")
            return
    ok = db_set_profile_gender(call.from_user.id, value)
    await dm(call, "✅ Gender updated." if ok else "❌ Could not save. Try again later.", reply_markup=profile_edit_kb())
    await call.answer()


@dp.callback_query(F.data == "prof_edit_bio")
async def prof_edit_bio(call: types.CallbackQuery):
    profile_flow_state[call.from_user.id] = {"await": "bio"}
    await dm(call, "Please send your new bio (max 250 characters). Send 'remove' to clear your bio.")
    await call.answer()


@dp.callback_query(F.data == "prof_edit_nick")
async def prof_edit_nick(call: types.CallbackQuery):
    profile_flow_state[call.from_user.id] = {"await": "nick"}
    await dm(call, "Please send your new nickname (max 32 letters/numbers/spaces). Send 'default' to reset to Anonymous.")
    await call.answer()


@dp.callback_query(F.data == "prof_my_confessions")
async def prof_my_confessions(call: types.CallbackQuery):
    try:
        rows = (
            supabase.table("confessions").select("id,text,is_approved")
            .eq("user_id", str(call.from_user.id)).order("id", desc=True).limit(10).execute().data or []
        )
    except Exception as e:
        print("My confessions failed:", e)
        rows = []
    if not rows:
        await dm(call, "You have no confessions yet.")
    else:
        lines = []
        for r in rows:
            status = "✅ Published" if r.get("is_approved") else "⏳ Not published"
            snippet = (r.get("text") or "")[:60]
            lines.append(f"#{r['id']} {status}\n{snippet}")
        await dm(call, "📝 Your last confessions:\n\n" + "\n\n".join(lines))
    await call.answer()


@dp.callback_query(F.data == "prof_my_comments")
async def prof_my_comments(call: types.CallbackQuery):
    try:
        rows = (
            supabase.table("comments").select("id,confession_id,text")
            .eq("user_id", str(call.from_user.id)).order("id", desc=True).limit(10).execute().data or []
        )
    except Exception as e:
        print("My comments failed:", e)
        rows = []
    if not rows:
        await dm(call, "You have no comments yet.")
    else:
        lines = [f"On confession #{r['confession_id']}:\n{(r.get('text') or '')[:60]}" for r in rows]
        await dm(call, "💬 Your last comments:\n\n" + "\n\n".join(lines))
    await call.answer()


# =====================================================================
# Message handlers (filtered ones first, catch-all LAST). Private chats only,
# so the bot never answers random chatter in the admin group.
# =====================================================================
@dp.message(F.chat.type == "private", lambda m: bool(m.from_user and profile_flow_state.get(m.from_user.id)))
async def handle_profile_inputs(message: types.Message):
    uid = message.from_user.id
    st = profile_flow_state.get(uid) or {}
    awaiting = st.get("await")
    txt = (message.text or "").strip()
    if not txt:
        await message.answer("Please send text only.")
        return

    if awaiting == "bio":
        if txt.lower() == "remove":
            ok = db_set_profile_bio(uid, None)
            note = "✅ Bio cleared."
        elif len(txt) > 250:
            await message.answer("❌ Bio too long. Please send up to 250 characters.")
            return
        else:
            ok = db_set_profile_bio(uid, txt)
            note = "✅ Bio updated."
    elif awaiting == "nick":
        if txt.lower() == "default":
            ok = db_set_profile_nickname(uid, None)
            note = "✅ Nickname reset to Anonymous."
        else:
            if len(txt) > 32:
                await message.answer("❌ Nickname too long. Max 32 characters.")
                return
            if not all(ch.isalnum() or ch == " " for ch in txt):
                await message.answer("❌ Use only letters, numbers, and spaces.")
                return
            ok = db_set_profile_nickname(uid, txt)
            note = "✅ Nickname updated."
    else:
        profile_flow_state.pop(uid, None)
        return

    profile_flow_state.pop(uid, None)
    await message.answer(note if ok else "❌ Could not save. Try again later.")
    await message.answer(render_edit_text(uid), reply_markup=profile_edit_kb())


@dp.message(F.chat.type == "private", lambda m: bool(m.from_user and m.from_user.id in user_reply_state))
async def handle_reply(message: types.Message):
    uid = message.from_user.id
    if not message.text:
        await message.answer("Please send text only.")
        return
    state = user_reply_state.pop(uid, None)
    if not state:
        return
    conf_id, parent_id = state["confession_id"], state["parent_comment_id"]

    try:
        db_add_comment(conf_id, str(uid), display_name_for(uid), message.text, parent_id)
    except Exception as e:
        print("Failed adding reply:", e, traceback.format_exc())
        await message.answer("❌ Failed to post reply. Try again later.")
        return

    # notify the parent commenter
    try:
        parent = db_get_comment(parent_id)
        if parent:
            parent_uid = int(parent.get("user_id") or 0)
            if parent_uid and parent_uid != uid:
                preview = (parent.get("text") or "")[:50]
                await bot.send_message(
                    parent_uid,
                    f"🔔 New reply to your comment:\n\n🗨️ {preview}\n↪️ {message.text}",
                    reply_markup=menu_reply_keyboard(),
                )
    except Exception as e:
        print("Failed to notify parent commenter:", e)

    await refresh_channel_count(conf_id)
    await message.answer("✅ Your reply has been added.", reply_markup=menu_reply_keyboard())


@dp.message(F.chat.type == "private")
async def handle_message(message: types.Message):
    uid = message.from_user.id
    text = message.text or ""
    state = user_state.get(uid, {})

    # ---- writing a comment ----
    if state.get("active_conf_id"):
        if not text:
            await message.answer("Please send text only.")
            return
        conf_id = state["active_conf_id"]
        try:
            db_add_comment(conf_id, str(uid), display_name_for(uid), text)
        except Exception as e:
            print("Failed adding comment:", e, traceback.format_exc())
            await message.answer("❌ Failed to post comment. Try again later.")
            user_state.pop(uid, None)
            return
        await refresh_channel_count(conf_id)
        await message.answer(f"✅ Your comment on Confession #{conf_id} is live!", reply_markup=menu_reply_keyboard())
        user_state.pop(uid, None)
        return

    # ---- submitting a confession / experience / thought ----
    if state.get("mode") in SHARE_MODES:
        if not text:
            await message.answer("Please send text only.")
            return
        label = SHARE_LABELS[state["mode"]]
        try:
            conf_id = db_add_confession(str(uid), text)
        except Exception as e:
            print("Failed adding confession:", e, traceback.format_exc())
            await message.answer("❌ Failed to submit. Try again later.", reply_markup=menu_reply_keyboard())
            user_state.pop(uid, None)
            return

        review_text = (
            f"🛂 Review New {label}\n"
            f"👤 Author: {message.from_user.full_name} (ID: {uid})\n"
            f"Confession ID: {conf_id}\n\n"
            f"📝 Content:\n{text}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✅ Approve", callback_data=f"admin_approve_{conf_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject_{conf_id}"),
            ]]
        )
        try:
            await bot.send_message(ADMIN_GROUP_ID, review_text, reply_markup=kb)
        except Exception as e:
            print("Failed to forward to admin group:", e, traceback.format_exc())
            await message.answer("❌ Could not forward to the admin group. Contact admin.", reply_markup=menu_reply_keyboard())
            user_state.pop(uid, None)
            return

        await message.answer("✅ Sent for review!", reply_markup=menu_reply_keyboard())
        user_state.pop(uid, None)
        return

    # ---- fallback ----
    await message.answer("What would you like to do?", reply_markup=share_type_kb())


# =====================================================================
# Comments: add / browse / reply / vote / report
# =====================================================================
@dp.callback_query(F.data.startswith("add_c_"))
async def add_comment_cb(call: types.CallbackQuery):
    try:
        conf_id = int(call.data.split("_")[2])
    except Exception:
        await call.answer("Invalid data")
        return
    user_reply_state.pop(call.from_user.id, None)
    profile_flow_state.pop(call.from_user.id, None)
    user_state[call.from_user.id] = {"active_conf_id": conf_id}
    await dm(call, "📝 Please type your comment now:")
    await call.answer()


@dp.callback_query(F.data.startswith("reply_"))
async def reply_cb(call: types.CallbackQuery):
    try:
        _, c_id_s, conf_id_s, page_s = call.data.split("_")
        c_id, conf_id, page = int(c_id_s), int(conf_id_s), int(page_s)
    except Exception:
        await call.answer("Invalid reply data")
        return
    user_state.pop(call.from_user.id, None)
    profile_flow_state.pop(call.from_user.id, None)
    user_reply_state[call.from_user.id] = {"confession_id": conf_id, "parent_comment_id": c_id, "page": page}
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_reply")]])
    await dm(call, "📝 Type your reply to that comment:", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "cancel_reply")
async def cancel_reply_cb(call: types.CallbackQuery):
    user_reply_state.pop(call.from_user.id, None)
    await call.answer("Reply cancelled.")
    await dm(call, "❌ Reply cancelled.")


@dp.callback_query(F.data.startswith("browse_"))
async def browse_cb(call: types.CallbackQuery):
    try:
        _, conf_id_s, page_s = call.data.split("_")
        conf_id, page = int(conf_id_s), int(page_s)
    except Exception:
        await call.answer("Invalid data")
        return

    comments = db_get_comments(conf_id)
    replies_map: Dict[int, List[dict]] = {}
    for c in comments:
        pid = c.get("parent_comment_id")
        if pid:
            replies_map.setdefault(int(pid), []).append(c)

    top_level = [c for c in comments if not c.get("parent_comment_id")]
    per_page = 10
    total = len(top_level)
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    chunk = top_level[(page - 1) * per_page: page * per_page]

    if not chunk:
        await dm(call, "No comments yet.")
        await call.answer()
        return

    for c in chunk:
        c_id = int(c["id"])
        c_text = c.get("text", "")
        ident = comment_identity(db_get_user_profile(int(c.get("user_id") or 0), create=False))
        likes, dislikes = db_get_vote_counts(c_id)
        await dm(call, f"💬 {c_text}\n{ident}", reply_markup=comment_vote_kb(c_id, likes, dislikes, conf_id, page))

        for r in replies_map.get(c_id, []):
            r_id = int(r["id"])
            preview = c_text[:50] + ("..." if len(c_text) > 50 else "")
            ident_r = comment_identity(db_get_user_profile(int(r.get("user_id") or 0), create=False))
            ident_r = "\n    ".join(ident_r.split("\n"))
            reply_txt = f'    ↪️ Reply to "{preview}":\n    {r.get("text", "")}\n    {ident_r}'
            likes_r, dislikes_r = db_get_vote_counts(r_id)
            await dm(call, reply_txt, reply_markup=comment_vote_kb(r_id, likes_r, dislikes_r, conf_id, page))

    await dm(call, f"Displaying page {page}/{total_pages}. Total {total} Comments",
             reply_markup=pagination_kb(conf_id, page, total_pages))
    await call.answer()


@dp.callback_query(F.data.startswith("vote_"))
async def vote_cb(call: types.CallbackQuery):
    parts = call.data.split("_")
    if len(parts) < 5:
        await call.answer("Invalid vote")
        return
    try:
        c_id, vtype, conf_id, page = int(parts[1]), parts[2], int(parts[3]), int(parts[4])
    except Exception:
        await call.answer("Invalid vote")
        return
    try:
        db_toggle_vote(str(call.from_user.id), c_id, 1 if vtype == "up" else -1)
    except Exception as e:
        print("Vote failed:", e)
        await call.answer("Could not record vote")
        return
    likes, dislikes = db_get_vote_counts(c_id)
    try:
        await bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=comment_vote_kb(c_id, likes, dislikes, conf_id, page),
        )
    except Exception:
        pass
    await call.answer("Vote recorded!")


REPORT_REASONS = ["Violence", "Racism", "Sexual Harassment", "Hate Speech", "Spam/Scam", "Other"]


@dp.callback_query(F.data.startswith("report_"))
async def report_cb(call: types.CallbackQuery):
    try:
        _, c_id_s, conf_id_s = call.data.split("_")
        c_id, conf_id = int(c_id_s), int(conf_id_s)
    except Exception:
        await call.answer("Invalid report data")
        return
    rows = []
    for i in range(0, len(REPORT_REASONS), 2):
        row = []
        for reason in REPORT_REASONS[i:i + 2]:
            key = reason.replace(" ", "_")
            row.append(InlineKeyboardButton(text=reason, callback_data=f"reason_{c_id}_{conf_id}_{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="noop")])
    await dm(call, "🚨 What is wrong with this comment? (Your report is anonymous)",
             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("reason_"))
async def reason_cb(call: types.CallbackQuery):
    # reason_{comment_id}_{conf_id}_{Reason_With_Underscores}
    try:
        _, c_id_s, conf_id_s, reason_key = call.data.split("_", 3)
        c_id, conf_id = int(c_id_s), int(conf_id_s)
    except Exception:
        await call.answer("Error: report data lost.")
        return
    reason = reason_key.replace("_", " ")

    result = db_add_report(c_id, str(call.from_user.id), reason)
    if result == "dup":
        await dm(call, "🚫 You already reported this comment.")
        await call.answer()
        return
    if result == "error":
        await dm(call, "❌ Could not submit the report. Try again later.")
        await call.answer()
        return

    comment = db_get_comment(c_id) or {}
    conf = db_get_confession(conf_id) or {}
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🗑️ Delete Comment", callback_data=f"admin_del_c_{c_id}_{conf_id}"),
            InlineKeyboardButton(text="✅ Dismiss Report", callback_data=f"admin_dis_r_{c_id}"),
        ]]
    )
    report_msg = (
        f"🚨 NEW REPORT on Comment ID #{c_id} (Confession #{conf_id}).\n\n"
        f"Confession: {conf.get('text')}\n\n"
        f"Comment: {comment.get('text')}\n"
        f"Author: {comment.get('username')} (ID: {comment.get('user_id')})\n\n"
        f"Reason: {reason}"
    )
    try:
        await bot.send_message(ADMIN_GROUP_ID, report_msg, reply_markup=admin_kb)
    except Exception as e:
        print("Failed to send report to admins:", e)
    await dm(call, f"✅ Report submitted successfully for reason: {reason}")
    await call.answer()


# =====================================================================
# Admin actions (only honoured when pressed inside the admin group)
# =====================================================================
@dp.callback_query(F.data.startswith("admin_del_c_"))
async def admin_delete_comment_cb(call: types.CallbackQuery):
    if not from_admin_chat(call):
        await call.answer("Not allowed", show_alert=True)
        return
    try:
        parts = call.data.split("_")
        c_id, conf_id = int(parts[3]), int(parts[4])
    except Exception:
        await call.answer("Invalid data")
        return
    try:
        db_delete_comment(c_id)
    except Exception as e:
        print("Admin delete failed:", e, traceback.format_exc())
        await call.answer(f"Delete failed: {str(e)[:150]}", show_alert=True)
        return
    await refresh_channel_count(conf_id)
    try:
        await bot.edit_message_text(
            f"🗑️ Comment ID #{c_id} deleted. Channel count updated.",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
        )
    except Exception:
        pass
    await call.answer("Deleted")


@dp.callback_query(F.data.startswith("admin_dis_r_"))
async def admin_dismiss_report_cb(call: types.CallbackQuery):
    if not from_admin_chat(call):
        await call.answer("Not allowed", show_alert=True)
        return
    try:
        c_id = int(call.data.split("_")[3])
    except Exception:
        await call.answer("Invalid data")
        return
    try:
        supabase.table("reports").update({"reason": "dismissed"}).eq("comment_id", c_id).execute()
    except Exception as e:
        print("Dismiss failed:", e)
    try:
        await bot.edit_message_text(
            f"✅ Reports for Comment ID #{c_id} dismissed.",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
        )
    except Exception:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("admin_approve_") | F.data.startswith("admin_reject_"))
async def admin_review_cb(call: types.CallbackQuery):
    if not from_admin_chat(call):
        await call.answer("Not allowed", show_alert=True)
        return
    try:
        parts = call.data.split("_")
        action, conf_id = parts[1], int(parts[2])
    except Exception:
        await call.answer("Invalid data")
        return

    conf = db_get_confession(conf_id)
    if not conf:
        await call.answer("Confession not found", show_alert=True)
        return
    final_text = conf.get("text", "")

    if action == "reject":
        db_set_confession_rejected(conf_id)
        try:
            await bot.edit_message_text(
                f"❌ Rejected.\n\nOriginal: {final_text}",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
            )
        except Exception:
            pass
        await call.answer("Rejected")
        return

    # approve -> publish (guard against double clicks)
    if conf.get("is_approved") and conf.get("channel_msg_id"):
        await call.answer("Already published", show_alert=True)
        return

    post_text = f"Confession #{conf_id}\n\n{final_text}\n\n#Confession"
    try:
        markup = build_channel_markup(await get_bot_username(), conf_id, db_count_comments(conf_id))
        sent = await bot.send_message(TARGET_CHANNEL_ID, post_text, reply_markup=markup)
        db_set_confession_published(conf_id, sent.message_id)
    except Exception as e:
        print("Failed to publish confession:", e, traceback.format_exc())
        await call.answer(f"Publish failed: {str(e)[:150]}", show_alert=True)
        return
    try:
        await bot.edit_message_text(
            f"✅ Confession #{conf_id} Published.",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
        )
    except Exception:
        pass
    await call.answer("Published")


@dp.callback_query(F.data == "noop")
async def noop_cb(call: types.CallbackQuery):
    await call.answer()


# =====================================================================
# Webhook + health
# =====================================================================
@app.post("/")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
    except Exception as e:
        print("Webhook: invalid update payload:", e)
        return {"ok": False, "error": "invalid update"}

    # Log only the update id: this is an anonymous platform, keep message text out of logs.
    print("Webhook update:", data.get("update_id"))
    try:
        await dp.feed_update(bot, update)
    except Exception as e:
        print("Error while handling update:", e, traceback.format_exc())
        return {"ok": False, "error": "handler error"}
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "running"}


@app.get("/render/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
