#!/usr/bin/env python3
"""
Confession Bot — Avien-style JSON DB version (Web Service friendly)

Features:
- Confessions (admin review -> publish to channel)
- Comments (instant publish)
- Likes / Dislikes (live update)
- Pagination
- Reports (forwarded to admin group)
- Admin moderation (delete comment, dismiss report)
- Uses a JSON file DB (avien.json) with atomic writes + simple locking
- Small Flask endpoint for health-check / keep-alive (suitable for Render Web Service)

Deployment notes:
- Put this file in your project root.
- Ensure persistent disk is enabled on Render and set DB_PATH to a writable path (recommended: /data/avien.json)
- Required environment variables:
    BOT_TOKEN (your Telegram bot token)
    ADMIN_GROUP_ID (admin group id, e.g. -100....)
    TARGET_CHANNEL_ID (target public channel id, e.g. -100....)
    DB_PATH (optional, default: "./avien.json")
    PORT (optional, default: 5000) -- Render sets this automatically for web services
- Start command on Render: `python main.py`

Author: Generated for you (Avien JSON conversion)
"""
import os
import json
import threading
import tempfile
import logging
import math
from typing import Optional, Tuple, List, Dict, Any

from flask import Flask
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG from env ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # required
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "-1003318000000"))
TARGET_CHANNEL_ID = int(os.environ.get("TARGET_CHANNEL_ID", "-1003351000000"))
DB_PATH = os.environ.get("DB_PATH", "./avien.json")
PORT = int(os.environ.get("PORT", "5000"))
# -------------------------------------------------

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States
ACCEPT_TERMS, CHOOSE_TYPE, COLLECT_CONTENT = range(3)

# ---------------- JSON DB (Avien-style) ----------------
class JSONDB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        # default structure
        self._default = {
            "meta": {"next_confession_id": 1, "next_comment_id": 1, "next_report_id": 1},
            "confessions": {},  # id -> {id, original_text, channel_message_id}
            "comments": {},     # id -> {id, confession_id, user_id, user_name, text, likes, dislikes}
            "reports": {}       # id -> {id, comment_id, reporting_user_id, reason, status}
        }
        self._data = None
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            self._data = self._default.copy()
            self._atomic_write(self._data)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            # ensure keys exist
            for k in self._default:
                if k not in self._data:
                    self._data[k] = self._default[k]
        except Exception:
            logger.exception("Failed to load DB, reinitializing.")
            self._data = self._default.copy()
            self._atomic_write(self._data)

    def _atomic_write(self, data: dict):
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        with tempfile.NamedTemporaryFile("w", delete=False, dir=dirn, encoding="utf-8") as tf:
            json.dump(data, tf, indent=2, ensure_ascii=False)
            tmpname = tf.name
        os.replace(tmpname, self.path)

    def _save(self):
        with self._lock:
            self._atomic_write(self._data)

    # Confessions
    def add_confession(self, text: str) -> int:
        with self._lock:
            cid = self._data["meta"]["next_confession_id"]
            self._data["meta"]["next_confession_id"] += 1
            self._data["confessions"][str(cid)] = {
                "id": cid,
                "original_text": text,
                "channel_message_id": 0
            }
            self._save()
            return cid

    def get_confession(self, conf_id: int) -> Optional[dict]:
        return self._data["confessions"].get(str(conf_id))

    def update_channel_msg_id(self, conf_id: int, msg_id: int):
        with self._lock:
            conf = self._data["confessions"].get(str(conf_id))
            if conf:
                conf["channel_message_id"] = int(msg_id)
                self._save()

    def get_channel_msg_id(self, conf_id: int) -> int:
        conf = self._data["confessions"].get(str(conf_id))
        return int(conf.get("channel_message_id", 0)) if conf else 0

    # Comments
    def add_comment(self, confession_id: int, user_id: int, user_name: str, text: str) -> int:
        with self._lock:
            cid = self._data["meta"]["next_comment_id"]
            self._data["meta"]["next_comment_id"] += 1
            self._data["comments"][str(cid)] = {
                "id": cid,
                "confession_id": int(confession_id),
                "user_id": int(user_id),
                "user_name": user_name,
                "text": text,
                "likes": 0,
                "dislikes": 0
            }
            self._save()
            return cid

    def get_comments(self, confession_id: int, page: int = 1, per_page: int = 3) -> Tuple[List[dict], int]:
        # Filter comments by confession_id and order by (likes - dislikes) desc
        all_comments = [
            v for v in self._data["comments"].values()
            if int(v["confession_id"]) == int(confession_id)
        ]
        all_comments.sort(key=lambda x: (int(x.get("likes", 0)) - int(x.get("dislikes", 0))), reverse=True)
        total = len(all_comments)
        start = (page - 1) * per_page
        end = start + per_page
        page_list = all_comments[start:end]
        return page_list, total

    def get_comment_count(self, confession_id: int) -> int:
        return sum(1 for v in self._data["comments"].values() if int(v["confession_id"]) == int(confession_id))

    def vote_comment(self, comment_id: int, vote_type: str):
        with self._lock:
            com = self._data["comments"].get(str(comment_id))
            if not com:
                return
            if vote_type == "up":
                com["likes"] = int(com.get("likes", 0)) + 1
            else:
                com["dislikes"] = int(com.get("dislikes", 0)) + 1
            self._save()

    def get_comment_votes(self, comment_id: int) -> Tuple[int, int]:
        com = self._data["comments"].get(str(comment_id))
        if not com:
            return 0, 0
        return int(com.get("likes", 0)), int(com.get("dislikes", 0))

    def get_comment_details(self, comment_id: int) -> Optional[Tuple[str, int]]:
        com = self._data["comments"].get(str(comment_id))
        if not com:
            return None
        return com.get("text"), int(com.get("confession_id"))

    def delete_comment(self, comment_id: int):
        with self._lock:
            if str(comment_id) in self._data["comments"]:
                del self._data["comments"][str(comment_id)]
            # mark reports resolved
            for rid, rep in list(self._data["reports"].items()):
                if int(rep["comment_id"]) == int(comment_id):
                    rep["status"] = "resolved"
            self._save()

    # Reports
    def add_report(self, comment_id: int, user_id: int, reason: str) -> bool:
        with self._lock:
            # check existing report from same user for same comment
            for r in self._data["reports"].values():
                if int(r["comment_id"]) == int(comment_id) and int(r["reporting_user_id"]) == int(user_id):
                    return False
            rid = self._data["meta"]["next_report_id"]
            self._data["meta"]["next_report_id"] += 1
            self._data["reports"][str(rid)] = {
                "id": rid,
                "comment_id": int(comment_id),
                "reporting_user_id": int(user_id),
                "reason": reason,
                "status": "pending"
            }
            self._save()
            return True

    def dismiss_reports_for_comment(self, comment_id: int):
        with self._lock:
            for rep in self._data["reports"].values():
                if int(rep["comment_id"]) == int(comment_id) and rep.get("status") == "pending":
                    rep["status"] = "dismissed"
            self._save()

# instantiate DB
db = JSONDB(DB_PATH)

# ---------------- UTILS ----------------
async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    me = await context.bot.get_me()
    return me.username

def build_channel_markup(bot_username: str, conf_id: int, count: int):
    url = f"https://t.me/{bot_username}?start=conf_{conf_id}"
    text = f"💬 View/Add Comments ({count})"
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, url=url)]])

# ---------------- HANDLERS (Adapted from your original) ----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_parts = update.message.text.split(maxsplit=1) if update.message and update.message.text else []
    payload = text_parts[1] if len(text_parts) > 1 else None

    # CASE A: deep link like start=conf_55
    if payload and payload.startswith("conf_"):
        try:
            conf_id = int(payload.split("_")[1])
            context.user_data["active_conf_id"] = conf_id

            conf = db.get_confession(conf_id)
            if not conf:
                await update.message.reply_text("Confession not found.")
                return ConversationHandler.END

            total_comments = db.get_comment_count(conf_id)

            keyboard = [
                [InlineKeyboardButton("➕ Add Comment", callback_data=f"add_c_{conf_id}")],
                [InlineKeyboardButton(f"📂 Browse Comments ({total_comments})", callback_data=f"browse_{conf_id}_1")]
            ]

            menu_text = (
                f"**Confession #{conf_id}**\n\n"
                f"_{conf['original_text']}_\n\n"
                f"You can always 🚩 report inappropriate comments.\n\n"
                f"Select an option below:"
            )

            await update.message.reply_text(
                menu_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END
        except ValueError:
            await update.message.reply_text("Invalid link.")
            return ConversationHandler.END

    # CASE B: normal /start -> submit new confession
    context.user_data["active_conf_id"] = None

    keyboard = [
        [InlineKeyboardButton("✅ Accept Terms", callback_data="accept_terms")],
        [InlineKeyboardButton("❌ Decline", callback_data="decline_terms")]
    ]
    terms_text = (
        "📜 *Terms & Conditions*\n\n"
        "1. Admins will review your message.\n"
        "2. Admins see your identity during review.\n"
        "3. Approved messages are posted anonymously.\n"
        "Click *Accept* to continue."
    )
    await update.message.reply_text(terms_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ACCEPT_TERMS

async def accept_terms_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "decline_terms":
        await query.edit_message_text("❌ You declined.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("💬 Experience", callback_data="type_exp")],
        [InlineKeyboardButton("💭 Thought", callback_data="type_tht")]
    ]
    await query.edit_message_text("What are you sharing?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_TYPE

async def choose_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["type"] = query.data
    await query.edit_message_text("✔ Okay — send your text now.")
    return COLLECT_CONTENT

async def collect_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text
    parent_id = context.user_data.get("active_conf_id")

    if parent_id is None:
        # New confession -> send to admin group for review
        review_msg = (
            f"🛂 *Review New Confession*\n"
            f"👤 Author: {user.full_name} (ID: {user.id})\n"
            f"📝 Content:\n{text}\n\n"
            "Admins: Edit this message to sanitize, then Approve."
        )
        keyboard = [[
            InlineKeyboardButton("✅ Approve", callback_data="admin_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="admin_reject")
        ]]

        sent_msg = await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=review_msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        # Save metadata for admin review callbacks
        context.bot_data[f"review_{sent_msg.chat.id}_{sent_msg.message_id}"] = {
            "user_id": user.id,
            "user_name": user.first_name,
            "parent_id": None,
            "original_text": text
        }

        await update.message.reply_text("✅ Confession sent for review!")
    else:
        # New comment -> instant publish
        db.add_comment(parent_id, user.id, user.first_name, text)

        bot_username = await get_bot_username(context)
        chan_msg_id = db.get_channel_msg_id(parent_id)
        new_count = db.get_comment_count(parent_id)

        if chan_msg_id:
            try:
                new_kb = build_channel_markup(bot_username, parent_id, new_count)
                await context.bot.edit_message_reply_markup(
                    chat_id=TARGET_CHANNEL_ID,
                    message_id=chan_msg_id,
                    reply_markup=new_kb
                )
            except Exception as e:
                logger.error(f"Failed to update channel button: {e}")

        await update.message.reply_text(f"✅ Your comment on Confession #{parent_id} is live!")

    return ConversationHandler.END

async def general_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # 1. add comment button
    if data.startswith("add_c_"):
        conf_id = int(data.split("_")[2])
        context.user_data["active_conf_id"] = conf_id
        await query.answer()
        await query.message.reply_text("📝 Please type your comment now:")
        return COLLECT_CONTENT

    # 2. browse comments
    if data.startswith("browse_"):
        _, conf_id, page = data.split("_")
        conf_id = int(conf_id); page = int(page)
        comments, total_count = db.get_comments(conf_id, page, per_page=3)

        try:
            await query.message.delete()
        except Exception:
            pass

        if not comments:
            await query.message.reply_text("No comments yet.", quote=False)
            return

        for c in comments:
            c_id = int(c["id"])
            u_name = c["user_name"]
            c_text = c["text"]
            likes = int(c.get("likes", 0))
            dislikes = int(c.get("dislikes", 0))
            txt = f"💬 {c_text}\n👤 *{u_name}*"
            kb = [[
                InlineKeyboardButton(f"👍 {likes}", callback_data=f"vote_{c_id}_up_{conf_id}_{page}"),
                InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"vote_{c_id}_dw_{conf_id}_{page}"),
                InlineKeyboardButton("🚩", callback_data=f"report_{c_id}_{conf_id}")
            ]]
            await query.message.reply_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb), quote=False)

        total_pages = max(1, math.ceil(total_count / 3))
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅ Prev", callback_data=f"browse_{conf_id}_{page-1}"))
        nav_row.append(InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("Next ➡", callback_data=f"browse_{conf_id}_{page+1}"))

        control_kb = [
            nav_row,
            [InlineKeyboardButton("➕ Add Comment", callback_data=f"add_c_{conf_id}")]
        ]

        await query.message.reply_text(f"Displaying page {page}/{total_pages}. Total {total_count} Comments",
                                       reply_markup=InlineKeyboardMarkup(control_kb))
        await query.answer()
        return

    # 3. voting
    if data.startswith("vote_"):
        parts = data.split("_")
        c_id = int(parts[1]); v_type = parts[2]; conf_id = parts[3]; page = parts[4]
        db.vote_comment(c_id, "up" if v_type == "up" else "dw")
        new_likes, new_dislikes = db.get_comment_votes(c_id)
        new_kb = [[
            InlineKeyboardButton(f"👍 {new_likes}", callback_data=f"vote_{c_id}_up_{conf_id}_{page}"),
            InlineKeyboardButton(f"👎 {new_dislikes}", callback_data=f"vote_{c_id}_dw_{conf_id}_{page}"),
            InlineKeyboardButton("🚩", callback_data=f"report_{c_id}_{conf_id}")
        ]]
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb))
        except Exception:
            pass
        await query.answer("Vote recorded!")
        return

    # 4. reporting (choose reason)
    if data.startswith("report_"):
        parts = data.split("_")
        c_id = int(parts[1]); conf_id = int(parts[2])
        context.user_data['report_c_id'] = c_id
        context.user_data['report_conf_id'] = conf_id
        reasons = ["Violence", "Racism", "Sexual Harassment", "Hate Speech", "Spam/Scam", "I don't like it"]
        keyboard = [
            [InlineKeyboardButton(r, callback_data=f"reason_{r.replace(' ', '')}") for r in reasons[i:i+2]]
            for i in range(0, len(reasons), 2)
        ]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="noop")])
        await query.answer()
        await query.message.reply_text("🚨 **What is wrong with this comment?** (Your report is anonymous)",
                                     reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # 5. reason selected
    if data.startswith("reason_"):
        reason_raw = data.split("_", 1)[1]
        reason = reason_raw.replace('_', ' ')
        user_id = query.from_user.id
        c_id = context.user_data.get('report_c_id')
        conf_id = context.user_data.get('report_conf_id')
        if c_id is None:
            await query.answer("Error: Comment ID lost.")
            return
        success = db.add_report(c_id, user_id, reason)
        if success:
            comment_details = db.get_comment_details(c_id)
            if comment_details:
                reported_text, conf_id_from_db = comment_details
            else:
                reported_text = "[Error: Comment not found in DB]"
                conf_id_from_db = conf_id
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=(f"🚨 **NEW REPORT** on Comment ID **#{c_id}** (Confession #{conf_id_from_db}).\n\n"
                      f"**Reason:** `{reason.title()}`\n\n"
                      f"**📝 Reported Comment Content:**\n> {reported_text}\n\n**Action:**"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete Comment", callback_data=f"admin_del_c_{c_id}_{conf_id_from_db}"),
                                                   InlineKeyboardButton("✅ Dismiss Report", callback_data=f"admin_dis_r_{c_id}")]]),
                parse_mode="Markdown"
            )
            await query.edit_message_text(f"✅ Report submitted successfully for reason: **{reason.title()}**.")
        else:
            await query.edit_message_text("🚫 You have already reported this comment.")
        context.user_data.pop('report_c_id', None)
        context.user_data.pop('report_conf_id', None)
        await query.answer()
        return

    if data == "noop":
        await query.answer()
        return

async def admin_review_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # admin delete comment: admin_del_c_{comment_id}_{conf_id}
    if data.startswith("admin_del_c_"):
        parts = data.split("_")
        # parts: ["admin","del","c","{comment_id}","{conf_id}"]
        try:
            c_id = int(parts[3]); conf_id = int(parts[4])
        except Exception:
            await query.answer("Invalid data")
            return
        db.delete_comment(c_id)
        # update channel button count
        bot_username = await get_bot_username(context)
        chan_msg_id = db.get_channel_msg_id(conf_id)
        new_count = db.get_comment_count(conf_id)
        if chan_msg_id:
            try:
                new_kb = build_channel_markup(bot_username, conf_id, new_count)
                await context.bot.edit_message_reply_markup(
                    chat_id=TARGET_CHANNEL_ID, message_id=chan_msg_id, reply_markup=new_kb
                )
            except Exception as e:
                logger.error(f"Failed to update channel button after deletion: {e}")
        await query.edit_message_text(f"🗑️ Comment ID **#{c_id}** deleted. Channel count updated.")
        return

    if data.startswith("admin_dis_r_"):
        parts = data.split("_")
        try:
            c_id = int(parts[3])
        except Exception:
            await query.answer("Invalid data")
            return
        db.dismiss_reports_for_comment(c_id)
        await query.edit_message_text(f"✅ Reports for Comment ID **#{c_id}** dismissed.")
        return

    # Admin approve/reject for new confession review
    key = f"review_{query.message.chat.id}_{query.message.message_id}"
    meta = context.bot_data.get(key)
    if not meta:
        await query.answer("Data lost (restart?).")
        return

    current_text = query.message.text or ""
    if "📝 Content:" in current_text:
        final_text = current_text.split("📝 Content:")[1].split("Admins:")[0].strip()
    else:
        final_text = meta.get("original_text", "")

    if data == "admin_reject":
        await query.edit_message_text(f"❌ Rejected.\n\nOriginal: {final_text}")
        del context.bot_data[key]
        return

    if data == "admin_approve":
        new_id = db.add_confession(final_text)
        bot_username = await get_bot_username(context)
        post_text = f"Confession #{new_id}\n\n{final_text}\n\n#Confession"
        kb = build_channel_markup(bot_username, new_id, 0)
        sent = await context.bot.send_message(TARGET_CHANNEL_ID, post_text, reply_markup=kb)
        db.update_channel_msg_id(new_id, sent.message_id)
        await query.edit_message_text(f"✅ Confession #{new_id} Published.")
        # cleanup
        del context.bot_data[key]
        return

# ---------------- MAIN ----------------
def main():
    # Flask app for keep-alive / health-check
    flask_app = Flask("confession_bot")

    @flask_app.route("/")
    def index():
        return "Bot is running."

    @flask_app.route("/healthz")
    def health():
        return "ok"

    # run Flask in a thread
    def run_flask():
        flask_app.run(host="0.0.0.0", port=PORT, threaded=True)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask thread started on port {PORT}")

    # Build Telegram application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_command),
                      CallbackQueryHandler(general_callback_handler, pattern="^add_c_")],
        states={
            ACCEPT_TERMS: [CallbackQueryHandler(accept_terms_handler)],
            CHOOSE_TYPE: [CallbackQueryHandler(choose_type_handler)],
            COLLECT_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_content_handler)],
        },
        fallbacks=[CommandHandler("start", start_command)],
        per_chat=True
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_review_handler, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(general_callback_handler))  # catch-all for browse/vote/report

    logger.info("Starting bot polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
