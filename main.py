#!/usr/bin/env python3
"""
Final Confession Bot — full system (Render + Supabase)
- Webhook via FastAPI (deploy with: 
  gunicorn main:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT)
- Uses Aiogram 3.x (async) and supabase-py
- Env vars (exact names):
    BOT_TOKEN
    ADMIN_GROUP_ID
    TARGET_CHANNEL_ID
    SUPABASE_URL
    SUPABASE_KEY
    PORT
"""

import os
import math
from typing import List, Optional

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from supabase import create_client

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
bot = Bot(BOT_TOKEN, parse_mode="Markdown")
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
def db_add_confession(user_id: str, text: str) -> int:
    res = supabase.table("confessions").insert({
        "user_id": user_id,
        "text": text,
        "is_approved": False
    }).execute()
    return int(res.data[0]["id"])

def db_get_confession(conf_id: int) -> Optional[dict]:
    r = supabase.table("confessions").select("*").eq("id", conf_id).execute()
    return r.data[0] if r.data else None

def db_set_confession_published(conf_id: int, channel_msg_id: int):
    supabase.table("confessions").update({"is_approved": True, "channel_msg_id": channel_msg_id}).eq("id", conf_id).execute()

def db_set_confession_rejected(conf_id: int):
    supabase.table("confessions").update({"is_approved": False}).eq("id", conf_id).execute()

def db_add_comment(confession_id: int, user_id: str, username: str, text: str) -> int:
    res = supabase.table("comments").insert({
        "confession_id": confession_id,
        "user_id": user_id,
        "username": username,
        "text": text
    }).execute()
    return int(res.data[0]["id"])

def db_get_comments(confession_id: int) -> List[dict]:
    r = supabase.table("comments").select("*").eq("confession_id", confession_id).order("id", {"ascending": True}).execute()
    return r.data or []

def db_get_comment(comment_id: int) -> Optional[dict]:
    r = supabase.table("comments").select("*").eq("id", comment_id).execute()
    return r.data[0] if r.data else None

def db_count_comments(confession_id: int) -> int:
    r = supabase.table("comments").select("id", count="exact").eq("confession_id", confession_id).execute()
    return int(r.count or 0)

def db_delete_comment(comment_id: int):
    supabase.table("comments").delete().eq("id", comment_id).execute()
    # remove votes cascade via DB FK if configured; otherwise delete votes explicitly
    supabase.table("votes").delete().eq("comment_id", comment_id).execute()
    supabase.table("reports").update({"reason": "resolved"}).eq("comment_id", comment_id).execute()

def db_upsert_vote(user_id: str, comment_id: int, vote_value: int):
    # upsert into votes (primary key user_id+comment_id)
    supabase.table("votes").upsert({
        "user_id": user_id,
        "comment_id": comment_id,
        "vote": vote_value
    }).execute()

def db_delete_vote(user_id: str, comment_id: int):
    supabase.table("votes").delete().eq("user_id", user_id).eq("comment_id", comment_id).execute()

def db_get_vote_counts(comment_id: int) -> (int, int):
    l = supabase.table("votes").select("*", count="exact").eq("comment_id", comment_id).eq("vote", 1).execute().count or 0
    d = supabase.table("votes").select("*", count="exact").eq("comment_id", comment_id).eq("vote", -1).execute().count or 0
    return int(l), int(d)

def db_add_report(comment_id: int, reporting_user_id: str, reason: str) -> bool:
    # prevent duplicate by same user
    existing = supabase.table("reports").select("*").eq("comment_id", comment_id).eq("user_id", reporting_user_id).execute()
    if existing.data:
        return False
    supabase.table("reports").insert({
        "comment_id": comment_id,
        "user_id": reporting_user_id,
        "reason": reason
    }).execute()
    return True

# ------------------ Bot handlers ------------------

# /start handler supports deep link payloads like /start conf_123
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # If payload deep-link present (aiogram attaches payload differently), handle simple flow by showing Terms or hub
    text = message.text or ""
    payload = None
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1]
    # If deep link like t.me/YourBot?start=conf_123 Telegram may send the payload in message.text after /start
    if payload and payload.startswith("conf_"):
        try:
            conf_id = int(payload.split("_", 1)[1])
        except Exception:
            await message.answer("Invalid confession link.")
            return
        conf = db_get_confession(conf_id)
        if not conf or not conf.get("is_approved"):
            await message.answer("Confession not found or not published.")
            return
        total = db_count_comments(conf_id)
        hub_text = f"*Confession #{conf_id}*\n\n_{conf.get('text')}_\n\nYou can always 🚩 report inappropriate comments.\n\nSelect an option below:"
        kb = hub_keyboard(conf_id, total)
        await message.answer(hub_text, reply_markup=kb, parse_mode="Markdown")
        return

    # Normal /start -> Terms or menu depending on user state
    # Show Terms & Accept for first-time or just show options for returning
    if message.from_user.id not in user_state.get("accepted_terms", {}):
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
        await message.answer(terms_text, reply_markup=kb, parse_mode="Markdown")
    else:
        # Returning user, present share options
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
            [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")]
        ])
        await message.answer("What do you want to share?", reply_markup=kb)

# Accept / decline Terms callbacks
@dp.callback_query(lambda c: c.data == "accept_terms" or c.data == "decline_terms")
async def accept_terms_cb(callback: types.CallbackQuery):
    if callback.data == "decline_terms":
        await callback.message.edit_text("❌ You declined.")
        await callback.answer()
        return
    # mark user accepted (ephemeral)
    # We'll keep a simple in-memory set of accepted users so returning users skip terms
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
    # save in user_state
    user_state[callback.from_user.id] = {"mode": callback.data, "active_conf_id": None}
    await bot.send_message(callback.from_user.id, "✔ Okay — send your text now.")
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
        c_id = db_add_comment(conf_id, str(uid), message.from_user.username or message.from_user.full_name, text)
        # Update channel button count
        new_count = db_count_comments(conf_id)
        conf = db_get_confession(conf_id)
        if conf and conf.get("channel_msg_id"):
            try:
                bot_username = (await bot.get_me()).username
                new_kb = build_channel_markup(bot_username, conf_id, new_count)
                await bot.edit_message_reply_markup(TARGET_CHANNEL_ID, conf.get("channel_msg_id"), reply_markup=new_kb)
            except Exception as e:
                # log silently
                print("Failed to update channel markup:", e)
        await message.reply(f"✅ Your comment on Confession #{conf_id} is live!")
        # clear active_conf_id
        user_state.pop(uid, None)
        return

    # Otherwise, assume it's a confession (user clicked share_experience/share_thought previously)
    if state.get("mode") in ("share_experience", "share_thought"):
        # save confession and forward to admin group (admins see who sent it)
        conf_id = db_add_confession(str(uid), text)
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
        try:
    await bot.send_message(ADMIN_GROUP_ID, review_text, parse_mode="Markdown", reply_markup=kb)
except Exception as e:
    print("FAILED TO SEND REVIEW MESSAGE:", e)
    await message.reply(f"⚠ Failed to send confession to admin group.\nError: `{e}`")
# persist admin metadata in DB if you want; for now DB contains confession and Admin sees ID
        await message.reply("✅ Confession sent for review!")
        user_state.pop(uid, None)
        return

    # If message arrives without mode/state, show the quick menu
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Share Experience", callback_data="share_experience")],
        [InlineKeyboardButton(text="💭 Share Thought", callback_data="share_thought")]
    ])
    await message.reply("What would you like to do?", reply_markup=kb)

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
        # format: add_c_{conf_id}
        try:
            conf_id = int(data.split("_")[2])
        except:
            await call.answer("Invalid data")
            return
        user_state[call.from_user.id] = {"active_conf_id": conf_id}
        await bot.send_message(call.from_user.id, "📝 Please type your comment now:")
        await call.answer()
        return

    # Browse comments: browse_{conf_id}_{page}
    if data.startswith("browse_"):
        # parse
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

        # delete the callback message to make space
        try:
            await bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        if not chunk:
            await bot.send_message(call.from_user.id, "No comments yet.")
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
            await bot.send_message(call.from_user.id, txt, parse_mode="Markdown", reply_markup=kb)

        # pagination controls (include add comment button)
        nav_kb = pagination_kb(conf_id, page, total_pages)
        await bot.send_message(call.from_user.id, f"Displaying page {page}/{total_pages}. Total {total} Comments", reply_markup=nav_kb)
        await call.answer()
        return

    # Voting: vote_{comment_id}_{type}_{conf_id}_{page}
    if data.startswith("vote_"):
        parts = data.split("_")
        if len(parts) < 5:
            await call.answer("Invalid vote"); return
        c_id = int(parts[1]); vtype = parts[2]; conf_id = int(parts[3]); page = int(parts[4])
        user_id = str(call.from_user.id)

        # 1 vote per user per comment (1 or -1). Toggle logic:
        # If same vote exists -> remove; if different -> upsert with new value
        existing = supabase.table("votes").select("*").eq("comment_id", c_id).eq("user_id", user_id).execute()
        want = 1 if vtype == "up" else -1

        if existing.data:
            cur = existing.data[0]
            if int(cur["vote"]) == want:
                # remove vote
                db_delete_vote(user_id, c_id)
            else:
                db_upsert_vote(user_id, c_id, want)
        else:
            db_upsert_vote(user_id, c_id, want)

        # fetch fresh counts and edit the keyboard on the message
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
        _, c_id_s, conf_id_s = data.split("_")
        c_id = int(c_id_s); conf_id = int(conf_id_s)
        # store in ephemeral user_state and ask reason
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
        await bot.send_message(call.from_user.id, "🚨 *What is wrong with this comment?* (Your report is anonymous)", parse_mode="Markdown", reply_markup=kb)
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
            await bot.send_message(call.from_user.id, "🚫 You already reported this comment.")
            await call.answer()
            return

        # forward report to admin group with confession text and comment
        comment = db_get_comment(c_id)
        conf = db_get_confession(conf_id)
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
        await bot.send_message(ADMIN_GROUP_ID, report_msg, parse_mode="Markdown", reply_markup=admin_kb)
        await bot.send_message(call.from_user.id, f"✅ Report submitted successfully for reason: *{reason}*", parse_mode="Markdown")
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
        # delete comment
        db_delete_comment(c_id)
        # update channel button count
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
        await bot.edit_message_text(f"🗑️ Comment ID *#{c_id}* deleted. Channel count updated.", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        await call.answer()
        return

    # Admin dismiss report
    if data.startswith("admin_dis_r_"):
        parts = data.split("_")
        try:
            c_id = int(parts[3])
        except:
            await call.answer("Invalid data"); return
        # mark report resolved/dismissed
        supabase.table("reports").update({"reason": "dismissed"}).eq("comment_id", c_id).execute()
        await bot.edit_message_text(f"✅ Reports for Comment ID *#{c_id}* dismissed.", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        await call.answer()
        return

    # Admin approve/reject from review message
    if data.startswith("admin_approve_") or data.startswith("admin_reject_"):
        parts = data.split("_")
        action = parts[1]
        conf_id = int(parts[2])
        # get current message text (admins may have edited)
        current_text = call.message.text or ""
        # try to extract content after "📝 Content:" if admin sanitized; fallback to DB
        if "📝 Content:" in current_text:
            try:
                final_text = current_text.split("📝 Content:")[1].strip()
            except:
                final_text = db_get_confession(conf_id).get("text","")
        else:
            final_text = db_get_confession(conf_id).get("text","")

        if action == "reject":
            db_set_confession_rejected(conf_id)
            await bot.edit_message_text(f"❌ Rejected.\n\nOriginal: {final_text}", call.message.chat.id, call.message.message_id)
            await call.answer()
            return
        else:
            # publish to channel (anonymous)
            post_text = f"*Confession #{conf_id}*\n\n{final_text}\n\n#Confession"
            sent = await bot.send_message(TARGET_CHANNEL_ID, post_text, parse_mode="Markdown", reply_markup=build_channel_markup((await bot.get_me()).username, conf_id, 0))
            db_set_confession_published(conf_id, sent.message_id)
            await bot.edit_message_text(f"✅ Confession #{conf_id} Published.", call.message.chat.id, call.message.message_id)
            await call.answer()
            return

    # fallback
    await call.answer()

# ------------------ Webhook route (FastAPI) ------------------
@app.post("/")
async def webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
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

# Local debug (optional)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

