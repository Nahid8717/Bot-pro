#!/usr/bin/env python3
"""
Full Telegram file-access bot.

Features:
- Admin upload (+title) via /upload
- /generate creates a token + deep-link and uses GPLinks API to produce a short link
- Deep-link: https://t.me/Enjoyvideo_bot?start=<token>
- When user returns from GPLinks (i.e. visits the deep link), bot auto-verifies user for 3 hours
- Verified users can click "Get files" to receive files attached to that token
- Sent files are recorded and auto-deleted from the user's chat after 20 minutes
- Files stored in MongoDB GridFS for persistence; metadata in collections
"""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List

import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from bson import ObjectId

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

# -------------------------
# Configuration (তুমি দিয়েছ তা এখানে বসানো আছে)
# -------------------------
BOT_TOKEN = "8130807460:AAEtWSYLcyrKQdxLZT3u4npB-1KRIOSAaF4"
MONGO_URI = "mongodb+srv://mrnahid8717_db_user:I5d9jPraCS58YCs9@cluster0.9lkdmju.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_filebot"
ADMIN_IDS = [6094591421]  # list of admin telegram user ids

# GPLinks
GPLINKS_API = "c80cfdbc1d6d6173408a9baa53ce8ad0ed8ebe68"
GPLINKS_API_BASE = "https://gplinks.in/api"
BOT_USERNAME = "Enjoyvideo_bot"  # your bot username (without @)

# Timers
VERIFICATION_TTL_HOURS = 3
AUTO_DELETE_AFTER_SECONDS = 20 * 60  # 20 minutes

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
UPLOADING_FILE, UPLOADING_TITLE = range(2)

# -------------------------
# MongoDB setup
# -------------------------
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]

files_coll = db.files          # file metadata
links_coll = db.links          # token -> file_ids
users_coll = db.users          # user verification per token
sent_messages_coll = db.sent_messages  # track sent messages to delete
gridfs = AsyncIOMotorGridFSBucket(db)

# Global application reference (set in main)
application: Application = None


# -------------------------
# Utility helpers
# -------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def now() -> datetime:
    return datetime.utcnow()


def generate_token(nbytes: int = 12) -> str:
    return secrets.token_urlsafe(nbytes)


async def ensure_indexes():
    """Ensure DB indexes for performance / uniqueness."""
    await links_coll.create_index([("token", 1)], unique=True)
    await files_coll.create_index([("created_at", 1)])
    await users_coll.create_index([("user_id", 1), ("token", 1)], unique=True)
    await sent_messages_coll.create_index([("expires_at", 1)])


async def generate_gplinks(original_url: str) -> str:
    """
    Call GPLinks API to shorten the given URL.
    Returns shortened URL on success, otherwise returns original_url.
    """
    api_url = f"{GPLINKS_API_BASE}?api={GPLINKS_API}&url={original_url}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # expected format: {"status":"success","shortenedUrl":"..."} (as assumed)
                    if isinstance(data, dict) and data.get("status") == "success" and data.get("shortenedUrl"):
                        return data["shortenedUrl"]
    except Exception as e:
        logger.warning("GPLinks API call failed: %s", e)
    return original_url


# -------------------------
# Start & deep-link handling
# -------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Normal /start - no args."""
    args = context.args
    if args:
        token = args[0]
        return await handle_start_with_token(update, context, token)
    else:
        await update.message.reply_text(
            "স্বাগতম! চ্যানেল থেকে দেওয়া লিংকে ক্লিক করলে তুমি এখানে আসবে এবং ফাইল পাবে (যদি ভেরিফাইড থাকো)।"
        )


async def handle_start_with_token(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    """
    Called when user opens bot via deep link: /start <token>
    We're using the assumption: if user returned via GPLinks (short link redirect to our deep link),
    then they have seen the ad. So we auto-mark them verified for this token.
    """
    user = update.effective_user
    link_doc = await links_coll.find_one({"token": token, "active": True})
    if not link_doc:
        await update.message.reply_text("❌ এই লিঙ্কটি অবৈধ বা ডিঅ্যাক্টিভ।")
        return

    # Auto-verify the user for this token for VERIFICATION_TTL_HOURS
    verified_until = now() + timedelta(hours=VERIFICATION_TTL_HOURS)
    await users_coll.update_one(
        {"user_id": user.id, "token": token},
        {"$set": {"verified_until": verified_until, "last_verified_at": now()}},
        upsert=True,
    )

    kb = [[InlineKeyboardButton("📂 ফাইল নাও", callback_data=f"get_files|{token}")]]
    await update.message.reply_text(
        f"✅ তুমি ভেরিফাইড হয়েছো!\n\nপরবর্তী {VERIFICATION_TTL_HOURS} ঘণ্টা তোমার ভেরিফিকেশন সক্রিয় থাকবে।",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# -------------------------
# Get files callback
# -------------------------
async def get_files_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Send all files for the link token to the user if verified.
    Each sent message recorded in DB with expiry for auto-deletion.
    """
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    # parse token
    try:
        _prefix, token = query.data.split("|", 1)
    except Exception:
        await query.edit_message_text("অনিচ্ছাকৃত অনুরোধ।")
        return

    link_doc = await links_coll.find_one({"token": token, "active": True})
    if not link_doc:
        await query.edit_message_text("❌ এই লিঙ্কটি আর সক্রিয় নেই।")
        return

    # Check verification
    user_ver = await users_coll.find_one({"user_id": user.id, "token": token})
    if not (user_ver and user_ver.get("verified_until") and user_ver["verified_until"] > now()):
        await query.edit_message_text("⏳ তোমার ভেরিফিকেশন মেয়াদ শেষ বা তুমি ভেরিফাইড নাও। আগে লিংক থেকে GPLinks দেখো।")
        return

    file_ids = link_doc.get("file_ids", [])
    if not file_ids:
        await query.edit_message_text("⚠️ এই লিংকে কোনো ফাইল নেই।")
        return

    sent_count = 0
    for fid in file_ids:
        # ensure ObjectId type if stored as such
        try:
            if isinstance(fid, str) and ObjectId.is_valid(fid):
                fid_obj = ObjectId(fid)
            else:
                fid_obj = fid
        except Exception:
            fid_obj = fid

        fdoc = await files_coll.find_one({"_id": fid_obj})
        if not fdoc:
            continue

        try:
            gridfs_id = fdoc.get("gridfs_id")
            filename = fdoc.get("filename", "file")
            title = fdoc.get("title", "")
            if gridfs_id:
                grid_out = await gridfs.open_download_stream(gridfs_id)
                data_bytes = await grid_out.read()
                bio = InputFile(data_bytes, filename=filename)
                await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_DOCUMENT)
                sent_msg = await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=bio,
                    caption=title or None,
                )
            else:
                # fallback to tg_file_id
                tg_file_id = fdoc.get("tg_file_id")
                if tg_file_id:
                    sent_msg = await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=tg_file_id,
                        caption=title or None,
                    )
                else:
                    continue
        except Exception as e:
            logger.exception("Failed to send file: %s", e)
            continue

        # record sent message for auto-delete
        expires_at = now() + timedelta(seconds=AUTO_DELETE_AFTER_SECONDS)
        entry = {
            "chat_id": sent_msg.chat_id,
            "message_id": sent_msg.message_id,
            "expires_at": expires_at,
            "link_token": token,
            "user_id": user.id,
            "file_id": fdoc.get("_id"),
            "created_at": now(),
        }
        res = await sent_messages_coll.insert_one(entry)
        # schedule delete task
        application.create_task(schedule_delete_message(res.inserted_id))
        sent_count += 1

    await query.edit_message_text(f"📤 {sent_count} ফাইল পাঠানো হয়েছে। ২০ মিনিট পরে মেসেজগুলো অটো-ডিলিট হবে।")


# -------------------------
# Schedule delete tasks & background cleaner
# -------------------------
async def schedule_delete_message(db_id: Any):
    """
    Load db entry, wait until expiry, then delete message from Telegram and DB.
    """
    try:
        doc = await sent_messages_coll.find_one({"_id": db_id})
        if not doc:
            return
        expires_at = doc.get("expires_at")
        if not expires_at:
            await sent_messages_coll.delete_one({"_id": db_id})
            return
        delay = (expires_at - now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        # delete message
        try:
            await application.bot.delete_message(chat_id=doc["chat_id"], message_id=doc["message_id"])
        except Exception as e:
            logger.debug("Could not delete message (may be already deleted): %s", e)
        # cleanup
        await sent_messages_coll.delete_one({"_id": db_id})
    except Exception as e:
        logger.exception("Error in schedule_delete_message: %s", e)


async def expire_check_loop():
    """
    Runs periodically and:
    - deletes any already-expired messages (in case of restart)
    - schedules near-future deletes
    """
    while True:
        try:
            tnow = now()
            # delete already expired
            cursor = sent_messages_coll.find({"expires_at": {"$lte": tnow}})
            async for doc in cursor:
                try:
                    await application.bot.delete_message(chat_id=doc["chat_id"], message_id=doc["message_id"])
                except Exception:
                    pass
                await sent_messages_coll.delete_one({"_id": doc["_id"]})

            # schedule upcoming (next 10 minutes)
            upcoming = sent_messages_coll.find({"expires_at": {"$gt": tnow, "$lte": tnow + timedelta(minutes=10)}})
            async for doc in upcoming:
                application.create_task(schedule_delete_message(doc["_id"]))

        except Exception as e:
            logger.exception("expire_check_loop error: %s", e)

        await asyncio.sleep(60)


# -------------------------
# Admin upload / generate flows
# -------------------------
async def admin_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ তুমি অ্যাডমিন না।")
        return ConversationHandler.END
    await update.message.reply_text("📤 ফাইল পাঠাও (ডকুমেন্ট/ফটো/ভিডিও)।")
    return UPLOADING_FILE


async def admin_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ তুমি অ্যাডমিন না।")
        return ConversationHandler.END

    filename = None
    mime_type = None
    tg_file_id = None
    file_bytes = None

    if update.message.document:
        d = update.message.document
        tg_file_id = d.file_id
        filename = d.file_name or "document"
        mime_type = d.mime_type
        file_obj = await application.bot.get_file(d.file_id)
        file_bytes = await file_obj.download_as_bytearray()
    elif update.message.photo:
        p = update.message.photo[-1]
        tg_file_id = p.file_id
        filename = f"photo_{p.file_unique_id}.jpg"
        mime_type = "image/jpeg"
        file_obj = await application.bot.get_file(p.file_id)
        file_bytes = await file_obj.download_as_bytearray()
    elif update.message.video:
        v = update.message.video
        tg_file_id = v.file_id
        filename = v.file_name or f"video_{v.file_unique_id}.mp4"
        mime_type = v.mime_type or "video/mp4"
        file_obj = await application.bot.get_file(v.file_id)
        file_bytes = await file_obj.download_as_bytearray()
    else:
        await update.message.reply_text("❗️ দয়া করে ডকুমেন্ট/ফটো/ভিডিও আপলোড করো।")
        return UPLOADING_FILE

    gridfs_id = None
    if file_bytes:
        gridfs_id = await gridfs.upload_from_stream(filename, bytes(file_bytes))

    file_doc = {
        "filename": filename,
        "mime_type": mime_type,
        "tg_file_id": tg_file_id,
        "gridfs_id": gridfs_id,
        "uploader_id": update.effective_user.id,
        "created_at": now(),
    }
    res = await files_coll.insert_one(file_doc)
    context.user_data["last_uploaded_file_id"] = res.inserted_id

    await update.message.reply_text(
        f"✅ আপলোড সম্পন্ন। ID: `{res.inserted_id}`\nএখন টাইটেল পাঠাও অথবা /skip লেখো।",
        parse_mode="Markdown",
    )
    return UPLOADING_TITLE


async def admin_set_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    fid = context.user_data.get("last_uploaded_file_id")
    if not fid:
        await update.message.reply_text("কোনো আপলোড পাওয়া যায়নি।")
        return ConversationHandler.END
    await files_coll.update_one({"_id": fid}, {"$set": {"title": update.message.text}})
    await update.message.reply_text("✅ টাইটেল সেভ হলো। `/generate` দিয়ে লিংক বানাও।")
    return ConversationHandler.END


async def admin_skip_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ℹ️ টাইটেল স্কিপ করা হলো। `/generate` চালাও।")
    return ConversationHandler.END


async def admin_generate_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ তুমি অ্যাডমিন নও।")
        return

    args = context.args
    file_ids: List[Any] = []
    if args:
        # assume args are string form of ObjectId or direct ObjectId string
        for a in args:
            if ObjectId.is_valid(a):
                file_ids.append(ObjectId(a))
            else:
                # try to find by string id stored as string
                doc = await files_coll.find_one({"_id": a})
                if doc:
                    file_ids.append(doc["_id"])
    else:
        last = context.user_data.get("last_uploaded_file_id")
        if last:
            file_ids = [last]
        else:
            await update.message.reply_text("⚠️ আগে একটি ফাইল আপলোড করো ( /upload )।")
            return

    # verify file_ids exist
    valid_ids = []
    for fid in file_ids:
        doc = await files_coll.find_one({"_id": fid})
        if doc:
            valid_ids.append(doc["_id"])

    if not valid_ids:
        await update.message.reply_text("⚠️ কোনো বৈধ ফাইল আইডি পাওয়া যায়নি।")
        return

    token = generate_token()
    link_doc = {
        "token": token,
        "file_ids": valid_ids,
        "created_by": update.effective_user.id,
        "created_at": now(),
        "active": True,
    }
    await links_coll.insert_one(link_doc)

    deep_link = f"https://t.me/{BOT_USERNAME}?start={token}"
    short_link = await generate_gplinks(deep_link)

    await update.message.reply_text(
        f"🔗 লিংক তৈরি হয়েছে:\n\n{short_link}\n\nToken: `{token}`\n(লিংকটি যতক্ষণ অ্যাডমিন ডিলিট করবেন না ততক্ষণ সক্রিয় থাকবে)",
        parse_mode="Markdown",
    )


# -------------------------
# Admin management commands (list / deactivate / delete file)
# -------------------------
async def admin_list_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ তুমি অ্যাডমিন নও।")
        return
    docs = links_coll.find({})
    out = []
    async for d in docs:
        out.append(f"- `{d.get('token')}` files: {d.get('file_ids', [])} active: {d.get('active', True)}")
    if not out:
        await update.message.reply_text("কোনো লিংক নেই।")
    else:
        await update.message.reply_text("\n".join(out), parse_mode="Markdown")


async def admin_deactivate_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ তুমি অ্যাডমিন নও।")
        return
    args = context.args
    if not args:
        await update.message.reply_text("ব্যবহার: /deactivate <token>")
        return
    token = args[0]
    res = await links_coll.update_one({"token": token}, {"$set": {"active": False}})
    if res.modified_count:
        await update.message.reply_text(f"Link `{token}` deactivated.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Link not found or already inactive.")


async def admin_delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ তুমি অ্যাডমিন নও।")
        return
    args = context.args
    if not args:
        await update.message.reply_text("ব্যবহার: /delfile <file_id>")
        return
    raw = args[0]
    doc = None
    if ObjectId.is_valid(raw):
        doc = await files_coll.find_one({"_id": ObjectId(raw)})
    if not doc:
        doc = await files_coll.find_one({"_id": raw})
    if not doc:
        await update.message.reply_text("File not found.")
        return
    gridfs_id = doc.get("gridfs_id")
    if gridfs_id:
        try:
            await gridfs.delete(gridfs_id)
        except Exception:
            logger.exception("GridFS delete failed")
    await files_coll.delete_one({"_id": doc["_id"]})
    await update.message.reply_text(f"File `{doc['_id']}` deleted.", parse_mode="Markdown")


# -------------------------
# Application startup
# -------------------------
def build_application() -> Application:
    global application
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(get_files_callback, pattern=r"^get_files\|"))
    # Admin conversation for upload
    conv = ConversationHandler(
        entry_points=[CommandHandler("upload", admin_upload_start)],
        states={
            UPLOADING_FILE: [MessageHandler(filters.ALL & ~filters.COMMAND, admin_receive_file)],
            UPLOADING_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_title),
                CommandHandler("skip", admin_skip_title),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        per_user=True,
    )
    application.add_handler(conv)

    # Admin commands
    application.add_handler(CommandHandler("generate", admin_generate_link))
    application.add_handler(CommandHandler("links", admin_list_links))
    application.add_handler(CommandHandler("deactivate", admin_deactivate_link))
    application.add_handler(CommandHandler("delfile", admin_delete_file))

    return application


async def _startup_tasks():
    # ensure indexes
    await ensure_indexes()
    # start background 
