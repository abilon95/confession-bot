#!/usr/bin/env python3
"""
Final Confession Bot — Render + Supabase (webhook)
- FastAPI webhook intended for: gunicorn main:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
- Aiogram 3.x dispatcher (using dp.feed_update in webhook)
- Supabase via supabase-py
- Environment vars (exact names expected):
    BOT_TOKEN
    ADMIN_GROUP_ID
    TARGET_CHANNEL_ID
    SUPABASE_URL
    SUPABASE_KEY
    PORT
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

# ------------------ Helpers / UI Builders ------------------
def build_channel_markup(bot_username: str, conf_id: int, count: int) -> InlineKeyboardMarkup:
    """Button on the channel post that deep-links into bot start with conf payload."""
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
        ]
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

# ------------------ Small in-memory user state (ephemeral) ------------------
# For flows: accept terms -> choose type -> send confession / add comment / report reason
user_state: dict = {}  # {user_id: {...}}

# ------------------ DB helper functions ------------------
def _safe_insert(table: str, payload: dict):
    """
    Insert with fallback: some Supabase projects may not have the same schema.
    If insertion fails due to missing column in schema cache (PGRST204), try a reduced payload.
    Returns the response object (res.data etc) or raises.
    """
    try:
        return supabase.table(table).insert(payload).execute()
    except APIError as e:
        # Try to detect missing column error and retry with minimal payload
        msg = getattr(e, "args", [None])[0]
        if isinstance(msg, dict) and "message" in msg and "Could not find the" in msg["message"]:
            # filter payload to only primitive text fields (best-effort)
            reduced = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool, type(None)))}
            try:
                return supabase.table(table).insert(reduced).execute()
            except Exception:
                raise
        raise

def db_add_confession(user_id: str, text: str) -> int:
    # Try with recommended fields - fallback handled in _safe_insert
    payload = {"user_id": user_id, "text": text, "is_approved": False}
    res = _safe_insert("confessions", payload)
    # server may return created row in res.data
    try:
        return int(res.data[0]["id"])
    except Exception:
        # if no row data returned, attempt to fetch last inserted by text+user (best-effort)
        r = supabase.table("confessions").select("*").eq("user_id", user_id).eq("text", text).order("id", {"ascending": False}).limit(1).execute()
        if r.data:
            return int(r.data[0]["id"])
        # as last resort raise
        raise RuntimeError("Could not determine confession id after insert")

def db_get_confession(conf_id: int) -> Optional[dict]:
    r = supabase.table("confessions").select("*").eq("id", conf_id).execute()
    return r.data[0] if r.data else None

def db_set_confession_published(conf_id: int, channel_msg_id: int):
    # update; if column doesn't exist will be ignored by Supabase - but we try
    try:
        supabase.table("confessions").update({"is_approved": True, "channel_msg_id": channel_msg_id}).eq("id", conf_id).execute()
    except Exception:
        # fallback: try to update only channel_msg_id
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
    r = supabase.table("comments").select("*").eq("confession_id", confession_id).order("id", {"ascending": True}).execute()
    return r.data or []

def db_get_comment(comment_id: int) -> Optional[dict]:
    r = supabase.table("comments").select("*").eq("id", comment_id).execute()
    return r.data[0] if r.data else None

def db_count_comments(confession_id: int) -> int:
    # Use exact count if supported
    r = supabase.table("comments").select("id", count="exact").eq("confession_id", confession_id).execute()
    return int(r.count or 0)

def db_delete_comment(comment_id: int):
    try:
        supabase.table("comments").delete().eq("id", comment_id).execute()
    except Exception:
        pass
    # remove votes cascade via DB FK if configured; otherwise delete votes explicitly
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

def db_get_vote_counts(comment_id: int) -> (int, int):
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

# ------------------ Bot handlers ------------------
# Note: aiogram handlers are registered through decorators; dp.feed_update used in webhook.

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

# /start handler supports deep link payloads like /start conf_123
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
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "Invalid confession link.")
            return
        conf = db_get_confession(conf_id)
        if not conf or not conf.get("is_approved"):
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "Confession not found or not published.")
            return
        total = db_count_comments(conf_id)
        hub_text = f"*Confession #{conf_id}*\n\n_{conf.get('text')}_\n\nYou can always 🚩 report inappropriate comments.\n\nSelect an option below:"
        kb = hub_keyboard(conf_id, total)
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), hub_text, reply_markup=kb)
        return

    # Normal /start -> Terms or menu depending on user state
    accepted_set = user_state.get("accepted_terms", set())
    if message.from_user.id not in accepted_set:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Accept Terms", callback_data="accept_terms")],
            [InlineKeyboardButton(text="❌ Decline", callback_data="decline_terms")]
        ])
        terms_text = (
            "📜 *Terms & Conditions*\n\n"
            "1. Admins will review your message.\n"
            "2. Admins see your identity during review.\n"
            "3. Approved messages are posted anonymously.\n\nClick *Accept* to continue."
        )
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), terms_text, reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
            [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")]
        ])
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "What do you want to share?", reply_markup=kb)

# Accept / decline Terms callbacks
@dp.callback_query(lambda c: c.data == "accept_terms" or c.data == "decline_terms")
async def accept_terms_cb(callback: types.CallbackQuery):
    if callback.data == "decline_terms":
        await callback.message.edit_text("❌ You declined.")
        await callback.answer()
        return
    accepted = user_state.get("accepted_terms", set())
    accepted.add(callback.from_user.id)
    user_state["accepted_terms"] = accepted
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
        [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")]
    ])
    await callback.message.edit_text("What are you sharing?", reply_markup=kb)
    await callback.answer()

# choose type -> prompt to send text
@dp.callback_query(lambda c: c.data in ("share_experience", "share_thought"))
async def choose_type_cb(callback: types.CallbackQuery):
    user_state[callback.from_user.id] = {"mode": callback.data, "active_conf_id": None}
    # send a private message asking for the text
    try:
        await bot.send_message(callback.from_user.id, "✔ Okay — send your text now.")
    except Exception:
        # user may not have started direct chat; reply in current chat as fallback
        await _safe_reply_or_send(callback.message.chat.id, callback.message.message_id, "✔ Okay — send your text now.")
    await callback.answer()

# handle incoming messages: either confession text or comment text depending on user_state
@dp.message()
async def handle_message(message: types.Message):
    uid = message.from_user.id
    text = message.text or ""
    state = user_state.get(uid, {})

    # If user is currently writing a comment (active_conf_id present)
    if state.get("active_conf_id"):
        conf_id = state["active_conf_id"]
        try:
            c_id = db_add_comment(conf_id, str(uid), message.from_user.username or message.from_user.full_name, text)
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
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), f"✅ Your comment on Confession #{conf_id} is live!")
        user_state.pop(uid, None)
        return

    # Otherwise, assume it's a confession (user clicked share_experience/share_thought previously)
    if state.get("mode") in ("share_experience", "share_thought"):
        try:
            conf_id = db_add_confession(str(uid), text)
        except Exception as e:
            print("Failed adding confession:", e, traceback.format_exc())
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Failed to submit confession. Try again later.")
            user_state.pop(uid, None)
            return

        # send to admin group for review (include author info)
        review_text = (
            f"🛂 *Review New Confession*\n"
            f"👤 Author: {message.from_user.full_name} (ID: {uid})\n"
            f"Confession ID: {conf_id}\n\n"
            f"📝 Content:\n{text}\n\n"
            "Admins: Edit this message to sanitize, then Approve."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data=f"admin_approve_{conf_id}"),
             InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject_{conf_id}")]
        ])
        # send review to admin group - robust send
        try:
            await bot.send_message(ADMIN_GROUP_ID, review_text, reply_markup=kb)
        except Exception:
            # sometimes admins group may block bot or group id wrong
            print("Failed to forward confession to admin group. Check ADMIN_GROUP_ID and bot permissions.")
            await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "❌ Could not forward confession to admin group. Contact admin.")
            user_state.pop(uid, None)
            return

        # confirm to user
        await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "✅ Confession sent for review!")
        user_state.pop(uid, None)
        return

    # If message arrives without mode/state, show the quick menu
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
        [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")]
    ])
    await _safe_reply_or_send(message.chat.id, getattr(message, "message_id", None), "What would you like to do?", reply_markup=kb)

# ---------------- Callback handler for hub, browse, vote, report, admin ----------------
@dp.callback_query()
async def general_callback(call: types.CallbackQuery):
    data = call.data or ""

    # NOOP
    if data == "noop":
        await call.answer()
        return

    # Add Comment button (from channel deep link hub or inside bot)
    if data.startswith("add_c_"):
        try:
            conf_id = int(data.split("_")[2])
        except:
            await call.answer("Invalid data")
            return
        user_state[call.from_user.id] = {"active_conf_id": conf_id}
        try:
            await bot.send_message(call.from_user.id, "📝 Please type your comment now:")
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, call.message.message_id, "📝 Please type your comment now:")
        await call.answer()
        return

    # Browse comments: browse_{conf_id}_{page}
    if data.startswith("browse_"):
        try:
            _, conf_id_s, page_s = data.split("_")
            conf_id = int(conf_id_s); page = int(page_s)
        except:
            await call.answer("Invalid data"); return

        comments = db_get_comments(conf_id)
        per_page = 3
        total = len(comments)
        total_pages = max(1, math.ceil(total / per_page))
        page = max(1, min(page, total_pages))
        start = (page-1)*per_page
        chunk = comments[start:start+per_page]

        # delete the callback message to reduce clutter (best-effort)
        try:
            await bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        if not chunk:
            try:
                await bot.send_message(call.from_user.id, "No comments yet.")
            except Exception:
                await _safe_reply_or_send(call.message.chat.id, None, "No comments yet.")
            await call.answer()
            return

        # Show each comment with vote buttons and report
        for c in chunk:
            c_id = int(c["id"])
            u_name = c.get("username") or "Anon"
            c_text = c.get("text", "")
            likes, dislikes = db_get_vote_counts(c_id)
            txt = f"💬 {c_text}\n👤 *{u_name}*"
            kb = comment_vote_kb(c_id, likes, dislikes, conf_id, page)
            try:
                await bot.send_message(call.from_user.id, txt, reply_markup=kb)
            except Exception:
                await _safe_reply_or_send(call.message.chat.id, None, txt, reply_markup=kb)

        # pagination controls (include add comment button)
        nav_kb = pagination_kb(conf_id, page, total_pages)
        try:
            await bot.send_message(call.from_user.id, f"Displaying page {page}/{total_pages}. Total {total} Comments", reply_markup=nav_kb)
        except Exception:
            await _safe_reply_or_send(call.message.chat.id, None, f"Displaying page {page}/{total_pages}. Total {total} Comments", reply_markup=nav_kb)
        await call.answer()
        return

    # Voting: vote_{comment_id}_{type}_{conf_id}_{page}
    if data.startswith("vote_"):
        parts = data.split("_")
        if len(parts) < 5:
            await call.answer("Invalid vote"); return
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
        return

    # Reporting: report_{comment_id}_{conf_id}
    if data.startswith("report_"):
        try:
            _, c_id_s, conf_id_s = data.split("_")
            c_id = int(c_id_s); conf_id = int(conf_id_s)
        except:
            await call.answer("Invalid report data"); return
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
        return

    # Reason selected -> submit report
    if data.startswith("reason_"):
        reason_raw = data.split("_",1)[1]
        reason = reason_raw.replace("_", " ")
        st = user_state.get(call.from_user.id, {})
        c_id = st.get("report_c_id")
        conf_id = st.get("report_conf_id")
        if not c_id:
            await call.answer("Error: comment ID lost."); return
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
            await _safe_reply_or_send(call.message.chat.id, None, "✅ Report submitted successfully for reason: *{reason}*")
        user_state.pop(call.from_user.id, None)
        await call.answer()
        return

    # Admin delete comment
    if data.startswith("admin_del_c_"):
        parts = data.split("_")
        try:
            c_id = int(parts[3]); conf_id = int(parts[4])
        except:
            await call.answer("Invalid data"); return
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
        return

    # Admin dismiss report
    if data.startswith("admin_dis_r_"):
        parts = data.split("_")
        try:
            c_id = int(parts[3])
        except:
            await call.answer("Invalid data"); return
        try:
            supabase.table("reports").update({"reason": "dismissed"}).eq("comment_id", c_id).execute()
        except Exception:
            pass
        try:
            await bot.edit_message_text(f"✅ Reports for Comment ID *#{c_id}* dismissed.", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        await call.answer()
        return

    # Admin approve/reject from review message
    if data.startswith("admin_approve_") or data.startswith("admin_reject_"):
        parts = data.split("_")
        # action expected like admin_approve_{id} => parts[1] == 'approve' parts[2] == id
        action = parts[1]
        conf_id = int(parts[2])
        current_text = call.message.text or ""
        if "📝 Content:" in current_text:
            try:
                final_text = current_text.split("📝 Content:")[1].strip()
            except:
                final_text = db_get_confession(conf_id).get("text","")
        else:
            final_text = db_get_confession(conf_id).get("text","")

        if action == "reject":
            db_set_confession_rejected(conf_id)
            try:
                await bot.edit_message_text(f"❌ Rejected.\n\nOriginal: {final_text}", call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            await call.answer()
            return
        else:
            # publish to channel (anonymous)
            post_text = f"*Confession #{conf_id}*\n\n{final_text}\n\n#Confession"
            try:
                sent = await bot.send_message(TARGET_CHANNEL_ID, post_text, reply_markup=build_channel_markup((await bot.get_me()).username, conf_id, 0))
                # update db with channel message id (best-effort)
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
            return

    await call.answer()

# ------------------ Webhook route (FastAPI) ------------------
@app.post("/")
async def webhook(request: Request):
    data = await request.json()
    try:
        update = types.Update(**data)
    except Exception:
        # invalid update
        return {"ok": False, "error": "invalid update"}
    try:
        await dp.feed_update(bot, update)
    except Exception as e:
        # Log - do not let exceptions kill the server
        print("Error while feeding update:", e, traceback.format_exc())
        # swallow errors and return ok so Telegram doesn't keep retrying too fast
        return {"ok": False, "error": "handler error"}
    return {"ok": True}

# Health endpoints
@app.get("/")
def root():
    return {"status": "running"}

@app.get("/render/health")
def health():
    return {"status": "ok"}

# ------------------ Start-up note ------------------
# This file is intended to be run by Gunicorn on Render:
# gunicorn main:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

