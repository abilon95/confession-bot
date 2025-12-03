#!/usr/bin/env python3
"""
Full Confession Bot (Version A)
- Supabase-backed
- FastAPI webhook for Render
- Admin review / publish flow
- Comment hub, pagination, likes/dislikes, reporting
"""

import os
from typing import List, Tuple, Optional
from fastapi import FastAPI, Request
from pydantic import BaseModel
from supabase import create_client, Client
import telebot
from telebot import types
import math

# ---------------- CONFIG / ENV ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0"))
TARGET_CHANNEL_ID = int(os.environ.get("TARGET_CHANNEL_ID", "0"))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
PORT = int(os.environ.get("PORT", "5000"))

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing BOT_TOKEN or SUPABASE config in environment variables")

# ---------------- SUPABASE CLIENT ----------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------- TELEGRAM BOT ----------------
# threaded=False is safer under webhook mode
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown", threaded=False)

# ---------------- FASTAPI ----------------
app = FastAPI()

# ---------------- Helper DB functions ----------------
def add_confession_to_db(user_id: str, text: str) -> int:
    res = supabase.table("confessions").insert({"user_id": user_id, "original_text": text}).execute()
    if res.error:
        raise RuntimeError(f"DB error: {res.error.message}")
    return int(res.data[0]["id"])

def set_confession_admin_meta(conf_id: int, chat_id: int, message_id: int):
    supabase.table("confessions").update(
        {"admin_chat_id": chat_id, "admin_message_id": message_id}
    ).eq("id", conf_id).execute()

def publish_confession(conf_id: int, channel_message_id: int):
    supabase.table("confessions").update(
        {"status": "published", "channel_message_id": channel_message_id}
    ).eq("id", conf_id).execute()

def reject_confession(conf_id: int):
    supabase.table("confessions").update({"status": "rejected"}).eq("id", conf_id).execute()

def get_confession(conf_id: int) -> Optional[dict]:
    r = supabase.table("confessions").select("*").eq("id", conf_id).execute()
    return r.data[0] if r.data else None

def update_channel_msg_id(conf_id: int, msg_id: int):
    supabase.table("confessions").update({"channel_message_id": msg_id}).eq("id", conf_id).execute()

# Comments
def add_comment_to_db(confession_id: int, user_id: str, user_name: str, text: str) -> int:
    res = supabase.table("comments").insert({
        "confession_id": confession_id,
        "user_id": user_id,
        "user_name": user_name,
        "text": text
    }).execute()
    return int(res.data[0]["id"])

def get_comments(confession_id: int, page: int = 1, per_page: int = 3) -> Tuple[List[dict], int]:
    # select all comments for confession, order by (likes - dislikes) desc
    res = supabase.table("comments").select("*").eq("confession_id", confession_id).execute()
    rows = res.data or []
    # compute net and sort
    rows.sort(key=lambda x: (int(x.get("likes", 0)) - int(x.get("dislikes", 0))), reverse=True)
    total = len(rows)
    start = (page - 1) * per_page
    page_rows = rows[start:start+per_page]
    return page_rows, total

def comment_count(confession_id: int) -> int:
    res = supabase.table("comments").select("id", count="exact").eq("confession_id", confession_id).execute()
    return int(res.count or 0)

def get_comment_by_id(comment_id: int) -> Optional[dict]:
    r = supabase.table("comments").select("*").eq("id", comment_id).execute()
    return r.data[0] if r.data else None

# Votes: toggle logic
def vote_toggle(comment_id: int, user_id: str, vote: int) -> Tuple[int,int]:
    """
    vote: 1 for like, -1 for dislike
    Toggle behavior: if existing same vote -> remove, if opposite vote -> change
    Returns (likes, dislikes)
    """
    # fetch existing vote
    r = supabase.table("votes").select("*").eq("comment_id", comment_id).eq("user_id", user_id).execute()
    existing = r.data[0] if r.data else None

    if existing:
        if existing["vote"] == vote:
            # remove
            supabase.table("votes").delete().eq("id", existing["id"]).execute()
        else:
            # change
            supabase.table("votes").update({"vote": vote}).eq("id", existing["id"]).execute()
    else:
        supabase.table("votes").insert({"comment_id": comment_id, "user_id": user_id, "vote": vote}).execute()

    # compute counts
    likes_r = supabase.rpc("count_votes", {"p_comment_id": comment_id, "p_vote": 1}) \
            if hasattr(supabase, 'rpc') else None
    # We will fallback to simple select if RPC not available
    res_l = supabase.table("votes").select("*").eq("comment_id", comment_id).eq("vote", 1).execute()
    res_d = supabase.table("votes").select("*").eq("comment_id", comment_id).eq("vote", -1).execute()
    likes = len(res_l.data or [])
    dislikes = len(res_d.data or [])

    # update comment counts (not strictly necessary but speeds later reads)
    supabase.table("comments").update({"likes": likes, "dislikes": dislikes}).eq("id", comment_id).execute()
    return likes, dislikes

# Reports
def add_report(comment_id: int, reporting_user_id: str, reason: str) -> bool:
    # check existing
    r = supabase.table("reports").select("*").eq("comment_id", comment_id).eq("reporting_user_id", reporting_user_id).execute()
    if r.data:
        return False
    supabase.table("reports").insert({
        "comment_id": comment_id,
        "reporting_user_id": reporting_user_id,
        "reason": reason
    }).execute()
    return True

def resolve_reports_for_comment(comment_id: int, status: str = "resolved"):
    supabase.table("reports").update({"status": status}).eq("comment_id", comment_id).execute()

def delete_comment(comment_id: int):
    supabase.table("comments").delete().eq("id", comment_id).execute()
    # mark reports resolved
    resolve_reports_for_comment(comment_id, "resolved")

# ---------------- TELEGRAM UI helpers ----------------
def build_channel_markup(bot_username: str, conf_id: int, count: int) -> types.InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=conf_{conf_id}"
    text = f"💬 View/Add Comments ({count})"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text, url=url))
    return kb

def make_hub_keyboard(conf_id: int, total_comments: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Add Comment", callback_data=f"add_c_{conf_id}"))
    kb.add(types.InlineKeyboardButton(f"📂 Browse Comments ({total_comments})", callback_data=f"browse_{conf_id}_1"))
    return kb

def make_comments_nav(conf_id: int, page: int, total_count: int, per_page: int = 3) -> types.InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(total_count / per_page))
    kb = types.InlineKeyboardMarkup()
    row = []
    if page > 1:
        row.append(types.InlineKeyboardButton("⬅ Prev", callback_data=f"browse_{conf_id}_{page-1}"))
    row.append(types.InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        row.append(types.InlineKeyboardButton("Next ➡", callback_data=f"browse_{conf_id}_{page+1}"))
    kb.row(*row)
    kb.add(types.InlineKeyboardButton("➕ Add Comment", callback_data=f"add_c_{conf_id}"))
    return kb

# ---------------- FastAPI webhook and helpers ----------------
class UpdateModel(BaseModel):
    update_id: int
    message: dict = None
    callback_query: dict = None

@app.post("/")
async def webhook(request: Request):
    data = await request.json()
    # process update synchronously via telebot
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return {"ok": True}

@app.get("/")
def health():
    return {"status": "running"}

# ---------------- Bot Handlers ----------------

# start with optional payload conf_N
@bot.message_handler(commands=["start"])
def start_command(msg):
    payload = None
    if msg.text and " " in msg.text:
        payload = msg.text.split(maxsplit=1)[1]
    if not payload and msg.text and msg.text.startswith("/start"):
        parts = msg.text.split()
        if len(parts) > 1:
            payload = parts[1]
    if payload and payload.startswith("conf_"):
        try:
            conf_id = int(payload.split("_")[1])
        except:
            bot.reply_to(msg, "Invalid link.")
            return
        conf = get_confession(conf_id)
        if not conf or conf.get("status") != "published":
            bot.reply_to(msg, "Confession not found or not published.")
            return
        total = comment_count(conf_id)
        hub_text = f"*Confession #{conf_id}*\n\n_{conf.get('original_text')}_\n\nYou can always 🚩 report inappropriate comments.\n\nSelect an option below:"
        kb = make_hub_keyboard(conf_id, total)
        bot.send_message(msg.chat.id, hub_text, parse_mode="Markdown", reply_markup=kb)
    else:
        # start for sending new confession
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("✅ Accept Terms", callback_data="accept_terms"),
               types.InlineKeyboardButton("❌ Decline", callback_data="decline_terms"))
        terms = ("📜 *Terms & Conditions*\n\n"
                 "1. Admins will review your message.\n"
                 "2. Admins see your identity during review.\n"
                 "3. Approved messages are posted anonymously.\n\nClick Accept to continue.")
        bot.send_message(msg.chat.id, terms, parse_mode="Markdown", reply_markup=kb)

# Accept terms -> choose type -> send content flow (simplified type handling)
@bot.callback_query_handler(func=lambda call: call.data in ("accept_terms", "decline_terms"))
def accept_terms_cb(call: types.CallbackQuery):
    if call.data == "decline_terms":
        bot.edit_message_text("❌ You declined.", call.message.chat.id, call.message.message_id)
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💬 Experience", callback_data="type_exp"))
    kb.add(types.InlineKeyboardButton("💭 Thought", callback_data="type_tht"))
    bot.edit_message_text("What are you sharing?", call.message.chat.id, call.message.message_id, reply_markup=kb)

# choose type -> ask to send text
@bot.callback_query_handler(func=lambda call: call.data in ("type_exp", "type_tht"))
def choose_type_cb(call: types.CallbackQuery):
    # save type in user state: simple in-memory mapping (ephemeral)
    # For persistence across restarts, you can save pending reviews to DB
    bot.send_message(call.message.chat.id, "✔ Okay — send your text now.")
    # store in memory
    user_state[call.from_user.id] = {"type": call.data, "active_conf_id": None}

# To hold ephemeral state for user during conversation:
user_state = {}

# collect message for confession or comment
@bot.message_handler(func=lambda m: True)
def collect_content(msg):
    user = msg.from_user
    text = msg.text
    state = user_state.get(user.id, {})
    active_conf = state.get("active_conf_id")
    if active_conf:
        # it's a comment on an existing confession
        cid = active_conf
        add_comment_to_db(cid, str(user.id), user.first_name or user.username or "Anonymous", text)
        # update channel button
        bot_username = bot.get_me().username
        chan_msg_id = get_confession(cid).get("channel_message_id")
        new_count = comment_count(cid)
        if chan_msg_id:
            try:
                new_kb = build_channel_markup(bot_username, cid, new_count)
                bot.edit_message_reply_markup(TARGET_CHANNEL_ID, chan_msg_id, reply_markup=new_kb)
            except Exception as e:
                print("Failed updating channel markup:", e)
        bot.reply_to(msg, f"✅ Your comment on Confession #{cid} is live!")
        # clear state
        user_state.pop(user.id, None)
        return
    else:
        # new confession -> send to admin group for review
        conf_id = add_confession_to_db(str(user.id), text)
        # send admin review message containing the conf content; admins can edit text then approve
        review_text = (f"🛂 *Review New Confession*\n"
                       f"👤 Author: {user.full_name} (ID: {user.id})\n"
                       f"Confession ID: {conf_id}\n"
                       f"📝 Content:\n{text}\n\n"
                       "Admins: Edit this message to sanitize, then Approve.")
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_{conf_id}"),
               types.InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{conf_id}"))
        sent = bot.send_message(ADMIN_GROUP_ID, review_text, parse_mode="Markdown", reply_markup=kb)
        # persist admin metadata
        set_confession_admin_meta(conf_id, sent.chat.id, sent.message_id)
        bot.reply_to(msg, "✅ Confession sent for review!")

# ---------------- Callback handler for hub, browse, vote, report, admin ----------------
@bot.callback_query_handler(func=lambda call: True)
def general_callback(call: types.CallbackQuery):
    data = call.data

    # NOOP
    if data == "noop":
        bot.answer_callback_query(call.id)
        return

    # Add Comment button from hub or browse controls
    if data.startswith("add_c_"):
        conf_id = int(data.split("_")[2])
        user_state[call.from_user.id] = {"active_conf_id": conf_id}
        bot.answer_callback_query(call.id)
        bot.send_message(call.from_user.id, "📝 Please type your comment now:")
        return

    # Browse comments: browse_{conf_id}_{page}
    if data.startswith("browse_"):
        try:
            _, conf_id_str, page_str = data.split("_")
            conf_id = int(conf_id_str)
            page = int(page_str)
        except:
            bot.answer_callback_query(call.id, "Invalid data")
            return

        comments, total = get_comments(conf_id, page, per_page=3)

        try:
            # delete the calling message (clean UX)
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass

        if not comments:
            bot.send_message(call.from_user.id, "No comments yet.")
            return

        # send each comment with vote buttons under it
        for c in comments:
            c_id = int(c["id"])
            u_name = c.get("user_name", "Anon")
            c_text = c.get("text", "")
            likes = int(c.get("likes", 0) or 0)
            dislikes = int(c.get("dislikes", 0) or 0)
            txt = f"💬 {c_text}\n👤 *{u_name}*"
            kb = types.InlineKeyboardMarkup()
            kb.row(types.InlineKeyboardButton(f"👍 {likes}", callback_data=f"vote_{c_id}_up_{conf_id}_{page}"),
                   types.InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"vote_{c_id}_dw_{conf_id}_{page}"),
                   types.InlineKeyboardButton("🚩", callback_data=f"report_{c_id}_{conf_id}"))
            bot.send_message(call.from_user.id, txt, parse_mode="Markdown", reply_markup=kb)

        # pagination & add comment
        nav_kb = make_comments_nav(conf_id, page, total, per_page=3)
        bot.send_message(call.from_user.id, f"Displaying page {page}. Total {total} Comments", reply_markup=nav_kb)
        bot.answer_callback_query(call.id)
        return

    # Voting: vote_{comment_id}_{type}_{conf_id}_{page}
    if data.startswith("vote_"):
        parts = data.split("_")
        if len(parts) < 5:
            bot.answer_callback_query(call.id, "Invalid vote")
            return
        c_id = int(parts[1]); vtype = parts[2]; conf_id = int(parts[3]); page = int(parts[4])
        if vtype == "up":
            likes, dislikes = vote_toggle(c_id, str(call.from_user.id), 1)
        else:
            likes, dislikes = vote_toggle(c_id, str(call.from_user.id), -1)
        # rebuild keyboard for this comment
        new_kb = types.InlineKeyboardMarkup()
        new_kb.row(types.InlineKeyboardButton(f"👍 {likes}", callback_data=f"vote_{c_id}_up_{conf_id}_{page}"),
                   types.InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"vote_{c_id}_dw_{conf_id}_{page}"),
                   types.InlineKeyboardButton("🚩", callback_data=f"report_{c_id}_{conf_id}"))
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_kb)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Vote recorded!")
        return

    # Reporting: show reasons
    if data.startswith("report_"):
        # report_{comment_id}_{conf_id}
        _, c_id_str, conf_id_str = data.split("_")
        c_id = int(c_id_str); conf_id = int(conf_id_str)
        # store in ephemeral state for user
        user_state[call.from_user.id] = {"report_c_id": c_id, "report_conf_id": conf_id}
        reasons = ["Violence", "Racism", "Sexual Harassment", "Hate Speech", "Spam/Scam", "I don't like it"]
        kb = types.InlineKeyboardMarkup()
        for i in range(0, len(reasons), 2):
            row = []
            row.append(types.InlineKeyboardButton(reasons[i], callback_data=f"reason_{reasons[i].replace(' ','_')}"))
            if i+1 < len(reasons):
                row.append(types.InlineKeyboardButton(reasons[i+1], callback_data=f"reason_{reasons[i+1].replace(' ','_')}"))
            kb.row(*row)
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="noop"))
        bot.send_message(call.from_user.id, "🚨 *What is wrong with this comment?* (Your report is anonymous)", parse_mode="Markdown", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    # Reason selected: reason_{Reason}
    if data.startswith("reason_"):
        reason_raw = data.split("_", 1)[1]
        reason = reason_raw.replace("_", " ")
        state = user_state.get(call.from_user.id, {})
        c_id = state.get("report_c_id")
        conf_id = state.get("report_conf_id")
        if not c_id:
            bot.answer_callback_query(call.id, "Error: comment lost")
            return
        ok = add_report(c_id, str(call.from_user.id), reason)
        if not ok:
            bot.send_message(call.from_user.id, "🚫 You already reported this comment.")
            bot.answer_callback_query(call.id)
            return
        # forward report to admin group with action buttons
        comment_details = get_comment_by_id(c_id) or {}
        reported_text = comment_details.get("text", "[deleted]")
        admin_kb = types.InlineKeyboardMarkup()
        admin_kb.row(types.InlineKeyboardButton("🗑️ Delete Comment", callback_data=f"admin_del_c_{c_id}_{conf_id}"),
                     types.InlineKeyboardButton("✅ Dismiss Report", callback_data=f"admin_dis_r_{c_id}"))
        bot.send_message(ADMIN_GROUP_ID,
                         f"🚨 *NEW REPORT* on Comment ID *#{c_id}* (Confession #{conf_id}).\n\n"
                         f"*Reason:* `{reason}`\n\n"
                         f"*Comment:* {reported_text}\n\n*Action:*",
                         parse_mode="Markdown", reply_markup=admin_kb)
        bot.send_message(call.from_user.id, f"✅ Report submitted for reason: *{reason}*", parse_mode="Markdown")
        # clear ephemeral
        user_state.pop(call.from_user.id, None)
        bot.answer_callback_query(call.id)
        return

    # Admin handlers:
    if data.startswith("admin_del_c_"):
        parts = data.split("_")
        c_id = int(parts[3]); conf_id = int(parts[4])
        delete_comment(c_id)
        # update channel button count
        conf = get_confession(conf_id)
        chan_msg_id = conf.get("channel_message_id") if conf else None
        new_count = comment_count(conf_id)
        if chan_msg_id:
            try:
                bot_username = bot.get_me().username
                new_kb = build_channel_markup(bot_username, conf_id, new_count)
                bot.edit_message_reply_markup(TARGET_CHANNEL_ID, chan_msg_id, reply_markup=new_kb)
            except Exception as e:
                print("Failed updating channel markup after delete:", e)
        bot.edit_message_text(f"🗑️ Comment ID *#{c_id}* deleted. Channel count updated.", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_dis_r_"):
        parts = data.split("_")
        c_id = int(parts[3])
        resolve_reports_for_comment(c_id, "dismissed")
        bot.edit_message_text(f"✅ Reports for Comment ID *#{c_id}* dismissed.", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        bot.answer_callback_query(call.id)
        return

    # Admin approve/reject from review message
    if data.startswith("admin_approve_") or data.startswith("admin_reject_"):
        # format: admin_approve_{conf_id}
        parts = data.split("_")
        action = parts[1]  # approve or reject
        conf_id = int(parts[2])
        # fetch current message text (admins may have edited for sanitization)
        current_text = call.message.text or ""
        # try to pull content after "📝 Content:" if present, otherwise fallback to DB original
        if "📝 Content:" in current_text:
            final_text = current_text.split("📝 Content:")[1].split("Admins:")[0].strip()
        else:
            conf = get_confession(conf_id)
            final_text = conf.get("original_text") if conf else ""
        if action == "reject":
            reject_confession(conf_id)
            bot.edit_message_text(f"❌ Rejected.\n\nOriginal: {final_text}", call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return
        else:
            # publish confession to channel
            new_conf_id = conf_id
            post_text = f"*Confession #{new_conf_id}*\n\n{final_text}\n\n#Confession"
            kb = build_channel_markup(bot.get_me().username, new_conf_id, 0)
            sent = bot.send_message(TARGET_CHANNEL_ID, post_text, parse_mode="Markdown", reply_markup=kb)
            # update DB
            publish_confession(new_conf_id, sent.message_id)
            bot.edit_message_text(f"✅ Confession #{new_conf_id} Published.", call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return

# ---------------- START (no polling; webhook only) ----------------
# No code to start polling here. FastAPI webhook handles updates.
# Deploy with gunicorn: main:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
