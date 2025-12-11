#!/usr/bin/env python3
"""
Final Confession Bot — Render + Supabase (webhook)
"""

import os
import math
import traceback
from typing import List, Optional

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from supabase import create_client
from postgrest.exceptions import APIError

# ------------------ Environment ------------------
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

# ------------------ Small in-memory user state ------------------
user_state: dict = {}        # {user_id: {...}}
user_reply_state: dict = {}  # {user_id: {"confession_id":..., "parent_comment_id":..., "page":...}}

# ------------------ Helpers / UI Builders ------------------
def build_channel_markup(bot_username: str, conf_id: int, count: int) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=conf_{conf_id}"
    text = f"💬 Add/View Comment ({count})"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])
    return kb

def hub_keyboard(conf_id: int, total_comments: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Comment", callback_data=f"add_c_{conf_id}")],
        [InlineKeyboardButton(text=f"📂 Browse Comments ({total_comments})", callback_data=f"browse_{conf_id}_1")]
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
    kb = InlineKeyboardMarkup(inline_keyboard=[row, [InlineKeyboardButton(text="➕ Add Comment", callback_data=f"add_c_{conf_id}")]])
    return kb

# ------------------ DB helper functions ------------------
# (your Supabase functions remain unchanged, except db_add_comment now supports parent_comment_id)

# ------------------ Bot handlers ------------------
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

# ------------------ Callback handler ------------------
@dp.callback_query()
async def general_callback(call: types.CallbackQuery):
    data = call.data or ""

    # Replying to a comment
    if data.startswith("reply_"):
        try:
            _, c_id_s, conf_id_s, page_s = data.split("_")
            c_id = int(c_id_s); conf_id = int(conf_id_s); page = int(page_s)
        except:
            await call.answer("Invalid reply data"); return

        user_reply_state[call.from_user.id] = {
            "confession_id": conf_id,
            "parent_comment_id": c_id,
            "page": page
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_reply")]
        ])

        try:
            await bot.send_message(call.from_user.id, f"📝 Type your reply to comment #{c_id}:", reply_markup=kb)
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, call.message.message_id, f"📝 Type your reply to comment #{c_id}:", reply_markup=kb)
        await call.answer()
        return

    # Cancel reply
    if data == "cancel_reply":
        user_reply_state.pop(call.from_user.id, None)
        await call.answer("Reply cancelled.")
        try:
            await bot.send_message(call.from_user.id, "❌ Reply cancelled.")
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, call.message.message_id, "❌ Reply cancelled.")
        return

    # Browse comments
    if data.startswith("browse_"):
        try:
            _, conf_id_s, page_s = data.split("_")
            conf_id = int(conf_id_s); page = int(page_s)
        except:
            await call.answer("Invalid data"); return

        comments = db_get_comments(conf_id)

        # Build mapping: parent_id -> list of replies
        replies_map = {}
        for c in comments:
            parent_id = c.get("parent_comment_id")
            if parent_id:
                replies_map.setdefault(parent_id, []).append(c)

        # Only top-level comments for pagination
        top_level = [c for c in comments if not c.get("parent_comment_id")]

        total = len(top_level)
        per_page = 10
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

        # Show each comment with replies nested
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

            # Show replies indented
            for r in replies_map.get(c_id, []):
                r_text = r.get("text", "")
                likes_r, dislikes_r = db_get_vote_counts(int(r["id"]))
                parent_preview = c_text[:50] + ("..." if len(c_text) > 50 else "")
                reply_txt = f"    ↪️ Reply to \"{parent_preview}\":\n    {r_text}\n    👤 *Anonymous*"
                kb_r = comment_vote_kb(int(r["id"]), likes_r, dislikes_r, conf_id, page)
                try:
                    await bot.send_message(call.from_user.id, reply_txt, reply_markup=kb_r)
                except Exception:
                    await _safe_reply_or_send(call.message.chat.id, None, reply_txt, reply_markup=kb_r)

        # Pagination controls
        nav_kb = pagination_kb(conf_id, page, total_pages)
        try:
            await bot.send_message(call.from_user.id, f"Displaying page {page}/{total_pages}. Total {total} Comments", reply_markup=nav_kb)
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, None, f"Displaying page {page}/{total_pages}. Total {total} Comments", reply_markup=nav_kb)
        await call.answer()
        return

    # ... (rest of your existing handlers: add_c_, vote_, report_, admin actions, etc.)
   
