import os
import re
import random
import asyncio
import sqlite3
import traceback
from datetime import datetime
import pytz
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ساخت Event Loop واحد پیش از ساخت کلاینت‌های Pyrofork و PyTgCalls (رفع قطعی پاسخ ندادن ربات)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ChatMemberStatus
from pyrogram.raw import functions, types as raw_types
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedVoice,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality

# ============================================================
# تنظیمات از متغیرهای محیطی (حذف خودکار فاصله‌های اضافه)
# ============================================================
API_ID = int(os.environ["API_ID"].strip())
API_HASH = os.environ["API_HASH"].strip()
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
SESSION_STRING = os.environ["SESSION_STRING"].strip()
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

IRAN_TZ = pytz.timezone("Asia/Tehran")
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# وضعیت زنده سیستم برای نمایش در مرورگر و لاگ
BOT_STATE = {
    "bot": "starting...",
    "assistant": "starting...",
    "scheduler": "starting...",
    "last_msg": "هنوز پیامی دریافت نشده",
}

def log(msg: str):
    print(f"[{datetime.now(IRAN_TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================
# دیتابیس SQLite (آرشیو صوت‌ها و زمان‌بندی‌ها)
# ============================================================
conn = sqlite3.connect("audio_bot.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    file_id TEXT NOT NULL,
    duration INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    run_time TEXT NOT NULL,
    daily INTEGER DEFAULT 0,
    last_run_date TEXT DEFAULT ''
)
""")
conn.commit()

pending_uploads = {}
active_streams = {}

# ============================================================
# کلاینت ربات اصلی و کلاینت دستیار ویس‌چت روی همان Loop واحد
# ============================================================
bot = Client("main_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
assistant = Client("vc_assistant", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, in_memory=True)
call_py = PyTgCalls(assistant)
scheduler = AsyncIOScheduler(timezone=IRAN_TZ, event_loop=loop)

BOT_USERNAME = ""
ASSISTANT_ID = 0


# ============================================================
# توابع کمکی
# ============================================================
def en_num(text: str) -> str:
    persian = "۰۱۲۳۴۵۶۷۸۹"
    arabic = "٠١٢٣٤٥٦٧٨٩"
    for i in range(10):
        text = text.replace(persian[i], str(i)).replace(arabic[i], str(i))
    return text


def fa_num(val) -> str:
    persian = "۰۱۲۳۴۵۶۷۸۹"
    return "".join(persian[int(c)] if c.isdigit() else c for c in str(val))


def can_manage(user_id: int) -> bool:
    return not ADMIN_ID or str(user_id) == ADMIN_ID


def find_track(query: str):
    q = en_num(query).strip()
    if q.isdigit():
        cur.execute("SELECT id, title, source, file_id, duration FROM tracks WHERE id = ?", (int(q),))
        row = cur.fetchone()
        if row:
            return row
    cur.execute(
        "SELECT id, title, source, file_id, duration FROM tracks WHERE lower(title) LIKE ? LIMIT 1",
        (f"%{q.lower()}%",),
    )
    return cur.fetchone()


def get_all_tracks(limit=25):
    cur.execute("SELECT id, title, source, file_id, duration FROM tracks ORDER BY id DESC LIMIT ?", (limit,))
    return cur.fetchall()


# ============================================================
# هسته پخش زنده داخل ویس‌چت گروه (فقط شنیداری)
# ============================================================
async def ensure_listen_only_call(chat_id: int, title: str):
    try:
        peer = await assistant.resolve_peer(chat_id)
        if isinstance(peer, raw_types.InputPeerChannel):
            full = await assistant.invoke(
                functions.channels.GetFullChannel(
                    channel=raw_types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
                )
            )
            call = full.full_chat.call
        else:
            full = await assistant.invoke(functions.messages.GetFullChat(chat_id=peer.chat_id))
            call = full.full_chat.call

        if not call:
            res = await assistant.invoke(
                functions.phone.CreateGroupCall(
                    peer=peer,
                    random_id=random.randint(10000, 999999999),
                    title=f"🎧 {title}"[:64],
                )
            )
            for upd in getattr(res, "updates", []):
                if hasattr(upd, "call") and hasattr(upd.call, "id"):
                    call = raw_types.InputGroupCall(id=upd.call.id, access_hash=upd.call.access_hash)
                    break

        if call:
            await assistant.invoke(
                functions.phone.ToggleGroupCallSettings(
                    call=call,
                    join_muted=True,
                )
            )
    except Exception as e:
        log(f"[VoiceChat Setup Warning] chat={chat_id}: {e}")


async def ensure_assistant_in_group(chat_id: int):
    if ASSISTANT_ID == 0:
        return False, f"❌ اکانت دستیار ویس‌چت متصل نیست!\nوضعیت فعلی: `{BOT_STATE['assistant']}`"

    try:
        await assistant.get_chat_member(chat_id, ASSISTANT_ID)
        return True, ""
    except Exception:
        try:
            invite_link = await bot.export_chat_invite_link(chat_id)
            await assistant.join_chat(invite_link)
            await asyncio.sleep(1)
            return True, ""
        except Exception:
            return False, (
                "⚠️ اکانت دستیار پخش زنده هنوز عضو این گروه نیست!\n"
                "لطفاً یا ربات را ادمین (با دسترسی لینک دعوت) کنید، یا اکانت دستیار را دستی به گروه اضافه و ادمین ویس‌چت کنید."
            )


async def play_in_group_vc(chat_id: int, track, scheduled_note: str = ""):
    track_id, title, source, file_id, duration = track

    ok, err_msg = await ensure_assistant_in_group(chat_id)
    if not ok:
        await bot.send_message(chat_id, err_msg)
        return

    status_msg = await bot.send_message(chat_id, f"⏳ در حال آماده‌سازی پخش زنده **«{title}»** در ویس‌چت گروه...")

    try:
        ext = "ogg" if source == "voice" else "mp3"
        local_path = os.path.join(DOWNLOAD_DIR, f"track_{track_id}.{ext}")
        if not os.path.exists(local_path):
            local_path = await bot.download_media(file_id, file_name=local_path)

        await ensure_listen_only_call(chat_id, title)

        stream = MediaStream(
            local_path,
            audio_parameters=AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
        )
        await call_py.play(chat_id, stream)

        active_streams[chat_id] = {"title": title, "paused": False, "file_path": local_path}

        header = f"{scheduled_note}\n\n" if scheduled_note else ""
        text = (
            f"{header}🔴 **پخش زنده در ویس‌چت گروه شروع شد!**\n\n"
            f"🎵 عنوان: **{title}**\n"
            f"🔇 حالت تماس: **فقط شنیداری (میکروفون اعضا بسته است)**\n"
            f"🎧 برای شنیدن هم‌زمان، بالای صفحه گروه روی **Join / پیوستن به ویس‌چت** بزنید."
        )
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸ مکث", callback_data="vc_pause"),
                InlineKeyboardButton("▶️ ادامه", callback_data="vc_resume"),
                InlineKeyboardButton("⏹ توقف", callback_data="vc_stop"),
            ],
            [InlineKeyboardButton("📩 دریافت فایل صوتی در چت", callback_data=f"sendfile_{track_id}")],
        ])

        await status_msg.edit_text(text, reply_markup=buttons)
        try:
            await bot.pin_chat_message(chat_id, status_msg.id, disable_notification=True)
        except Exception:
            pass

    except Exception as e:
        log(f"[Play Error] chat={chat_id}: {e}")
        await status_msg.edit_text(
            f"❌ خطا در شروع پخش زنده در ویس‌چت:\n`{e}`\n\n"
            "💡 مطمئن شوید **اکانت دستیار** عضو گروه است و دسترسی **مدیریت ویس‌چت (Manage Video Chats)** دارد."
        )


async def control_vc(chat_id: int, action: str) -> str:
    if chat_id not in active_streams:
        return "⚠️ در حال حاضر پخش زنده‌ای در ویس‌چت این گروه فعال نیست."

    try:
        if action == "pause":
            if hasattr(call_py, "pause"):
                await call_py.pause(chat_id)
            else:
                await call_py.pause_stream(chat_id)
            active_streams[chat_id]["paused"] = True
            return "⏸ پخش زنده در ویس‌چت مکث شد."

        if action == "resume":
            if hasattr(call_py, "resume"):
                await call_py.resume(chat_id)
            else:
                await call_py.resume_stream(chat_id)
            active_streams[chat_id]["paused"] = False
            return "▶️ پخش زنده در ویس‌چت ادامه یافت."

        if action == "stop":
            if hasattr(call_py, "leave_call"):
                await call_py.leave_call(chat_id)
            else:
                await call_py.leave_group_call(chat_id)
            active_streams.pop(chat_id, None)
            return "⏹ پخش زنده متوقف شد و ربات از ویس‌چت خارج شد."
    except Exception as e:
        return f"❌ خطا در کنترل ویس‌چت: {e}"
    return ""


# ============================================================
# زمان‌بندی خودکار هر دقیقه (به وقت ایران)
# ============================================================
async def check_schedules():
    now_ir = datetime.now(IRAN_TZ)
    hhmm = now_ir.strftime("%H:%M")
    today = now_ir.strftime("%Y-%m-%d")

    cur.execute(
        "SELECT id, chat_id, track_id, run_time, daily, last_run_date FROM schedules WHERE run_time = ?",
        (hhmm,),
    )
    rows = cur.fetchall()

    for sched_id, chat_id, track_id, run_time, daily, last_run_date in rows:
        if last_run_date == today:
            continue

        track = find_track(str(track_id))
        if track:
            note = (
                f"⏰ **پخش زمان‌بندی‌شده روزانه (ساعت {fa_num(run_time)})**"
                if daily
                else f"⏰ **پخش زمان‌بندی‌شده (ساعت {fa_num(run_time)})**"
            )
            if chat_id < 0:
                await play_in_group_vc(chat_id, track, scheduled_note=note)
            else:
                _, title, source, file_id, _ = track
                await bot.send_message(chat_id, f"{note}\n🎵 **{title}**")
                if source == "voice":
                    await bot.send_voice(chat_id, file_id, caption=f"🎙 {title}")
                else:
                    await bot.send_audio(chat_id, file_id, caption=f"🎵 {title}")

        if daily:
            cur.execute("UPDATE schedules SET last_run_date = ? WHERE id = ?", (today, sched_id))
        else:
            cur.execute("DELETE FROM schedules WHERE id = ?", (sched_id,))
        conn.commit()


# ============================================================
# خوش‌آمدگویی هنگام اضافه شدن ربات به گروه
# ============================================================
@bot.on_chat_member_updated()
async def on_bot_added(client, event):
    if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not event.new_chat_member or not event.new_chat_member.user.is_self:
        return

    old_st = event.old_chat_member.status if event.old_chat_member else ChatMemberStatus.LEFT
    new_st = event.new_chat_member.status

    if old_st in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new_st in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    ):
        text = (
            f"✅ **ربات پخش زنده ویس‌چت در گروه «{event.chat.title}» فعال شد!** 🎧\n\n"
            "با این ربات می‌توانید مداحی، قرآن، دعا و هر صوتی را **مستقیم داخل ویس‌چت گروه** به‌صورت **فقط شنیداری (بدون میکروفون اعضا)** پخش یا زمان‌بندی کنید.\n\n"
            "📖 **دستورات فارسی گروه:**\n"
            "• `لیست` : نمایش آرشیو صوت‌ها\n"
            "• `پخش [کد یا نام]` : شروع پخش زنده در ویس‌چت\n"
            "• `مکث` | `ادامه` | `توقف` : کنترل ویس‌چت\n"
            "• `زمانبندی [کد] 21:30` : پخش خودکار یک‌باره\n"
            "• `زمانبندی روزانه [کد] 07:00` : پخش خودکار هر روز\n"
            "• `وضعیت` : تست آنلاین بودن ربات\n\n"
            "⚠️ **نکته ضروری:** برای باز کردن خودکار ویس‌چت و بستن میکروفون اعضا، هم ربات و هم **اکانت دستیار** باید ادمین (با دسترسی مدیریت ویس‌چت) باشند."
        )
        await bot.send_message(event.chat.id, text)


# ============================================================
# حالت Inline برای ارسال سریع صوت در پی‌وی شخص دیگر
# ============================================================
@bot.on_inline_query()
async def on_inline(client, iq):
    q = (iq.query or "").strip().lower()
    tracks = get_all_tracks(30)
    if q:
        tracks = [t for t in tracks if q in t[1].lower() or q == str(t[0])]

    results = [
        InlineQueryResultArticle(
            title="📖 راهنمای ربات پخش زنده صوت و ویس‌چت",
            description="ارسال توضیح امکانات ربات در این پی‌وی",
            input_message_content=InputTextMessageContent(
                f"🎧 **ربات پخش زنده ویس‌چت و آرشیو صوتی**\n\n"
                f"• در گروه‌ها: پخش زنده مستقیم داخل ویس‌چت تلگرام (فقط شنیداری و بدون نیاز به فیلترشکن)\n"
                f"• در پی‌وی: ارسال فوری صوت‌ها با تایپ `@{BOT_USERNAME} نام_صوت`"
            ),
        )
    ]

    for _, title, source, file_id, _ in tracks[:20]:
        if source == "voice":
            results.append(InlineQueryResultCachedVoice(voice_file_id=file_id, title=f"🎙 {title}", caption=f"🎙 **{title}**"))
        else:
            results.append(InlineQueryResultCachedAudio(audio_file_id=file_id, caption=f"🎵 **{title}**"))

    await iq.answer(results, cache_time=2)


# ============================================================
# مدیریت پیام‌های پی‌وی (ذخیره فایل)
# ============================================================
@bot.on_message(filters.private & (filters.voice | filters.audio | filters.document))
async def on_pv_media(client, msg):
    log(f"[MEDIA] from={msg.from_user.id}")
    if not can_manage(msg.from_user.id):
        await msg.reply_text("⛔ فقط مدیر ربات اجازه افزودن فایل به آرشیو را دارد.")
        return

    if msg.document and not (msg.document.mime_type or "").startswith("audio/"):
        return

    media = msg.voice or msg.audio or msg.document
    source = "voice" if msg.voice else "audio"
    duration = getattr(media, "duration", 0) or 0
    caption = (msg.caption or "").strip()

    if caption:
        cur.execute(
            "INSERT INTO tracks (title, source, file_id, duration) VALUES (?, ?, ?, ?)",
            (caption, source, media.file_id, duration),
        )
        conn.commit()
        tid = cur.lastrowid
        await msg.reply_text(
            f"✅ **صوت در آرشیو ذخیره شد!**\n\n"
            f"📌 نام: **{caption}**\n"
            f"🔢 کد پخش: `{tid}`\n\n"
            f"حالا توی گروه بنویس: `پخش {tid}` تا زنده تو ویس‌چت پخش بشه!"
        )
        return

    pending_uploads[msg.from_user.id] = {
        "source": source,
        "file_id": media.file_id,
        "duration": duration,
        "fallback": getattr(msg.audio, "title", "") if msg.audio else "",
    }
    fb = pending_uploads[msg.from_user.id]["fallback"]
    hint = f"\n(یا بنویس `تایید` برای ذخیره با نام «{fb}»)" if fb else ""
    await msg.reply_text(f"🎙 فایل دریافت شد! حالا یک **اسم فارسی** براش بفرست:{hint}")


# ============================================================
# مدیریت پیام‌های متنی (پی‌وی و گروه)
# ============================================================
@bot.on_message(filters.text)
async def on_text_message(client, msg):
    chat_id = msg.chat.id
    is_pv = msg.chat.type == ChatType.PRIVATE
    text = (msg.text or "").strip()
    user_id = msg.from_user.id if msg.from_user else 0

    BOT_STATE["last_msg"] = f"chat={chat_id} | text={text[:25]}"
    log(f"[MSG] chat={chat_id} pv={is_pv} text={text[:40]}")

    # تکمیل اسم فایل در انتظار ذخیره در پی‌وی
    if is_pv and user_id in pending_uploads and not text.startswith("/") and text not in ("لیست", "راهنما", "وضعیت", "تست", "شروع"):
        info = pending_uploads.pop(user_id)
        title = info["fallback"] if (text == "تایید" and info["fallback"]) else text
        cur.execute(
            "INSERT INTO tracks (title, source, file_id, duration) VALUES (?, ?, ?, ?)",
            (title, info["source"], info["file_id"], info["duration"]),
        )
        conn.commit()
        tid = cur.lastrowid
        await msg.reply_text(
            f"✅ **صوت با موفقیت ثبت شد!**\n\n"
            f"📌 نام: **{title}**\n"
            f"🔢 کد: `{tid}`\n\n"
            f"• پخش زنده در ویس‌چت گروه: `پخش {tid}`\n"
            f"• ارسال در پی‌وی دیگران: `@{BOT_USERNAME} {title}`"
        )
        return

    # شروع و منوی پی‌وی
    if is_pv and (text.startswith("/start") or text in ("شروع", "منو")):
        cur.execute("SELECT COUNT(*) FROM tracks")
        cnt = cur.fetchone()[0]
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎼 لیست صوت‌ها", callback_data="show_list"),
                InlineKeyboardButton("📡 وضعیت ربات", callback_data="show_status"),
            ],
            [InlineKeyboardButton("🔍 ارسال صوت در پی‌وی دیگران", switch_inline_query="")],
            [InlineKeyboardButton("➕ افزودن ربات به گروه", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")],
        ])
        ast_note = "✅ متصل و آماده پخش زنده" if ASSISTANT_ID else f"⚠️ خطا در اکانت دستیار: `{BOT_STATE['assistant']}`"
        await msg.reply_text(
            f"✅ **ربات پخش زنده ویس‌چت فعال و آنلاین است!** 🎧\n\n"
            f"📡 وضعیت اکانت دستیار ویس‌چت: {ast_note}\n"
            f"📂 تعداد صوت‌های آرشیو: **{fa_num(cnt)} مورد**\n\n"
            "📥 **برای افزودن صوت جدید:** همین‌جا یک ویس، مداحی یا تلاوت قرآن بفرست و اسمش رو تعیین کن.\n"
            "🔴 **پخش زنده در گروه:** در گروه بنویس `پخش [نام یا کد]` تا ویس‌چت به‌صورت فقط شنیداری باز بشه و صوت زنده پخش بشه.\n"
            "⏰ **زمان‌بندی (وقت ایران):** `زمانبندی [کد] 21:30` یا `زمانبندی روزانه [کد] 07:00`",
            reply_markup=kb,
        )
        return

    # وضعیت ربات
    if text in ("وضعیت", "تست") or text.startswith("/status"):
        cur.execute("SELECT COUNT(*) FROM tracks")
        t_cnt = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM schedules WHERE chat_id = ?", (chat_id,))
        s_cnt = cur.fetchone()[0]
        now_ir = datetime.now(IRAN_TZ).strftime("%H:%M")
        vc_st = f"🔴 در حال پخش ({active_streams[chat_id]['title']})" if chat_id in active_streams else "⚪️ خاموش"
        await msg.reply_text(
            f"✅ **ربات فعال و آنلاین است!**\n\n"
            f"🤖 ربات اصلی: `{BOT_STATE['bot']}`\n"
            f"🎙 اکانت دستیار ویس‌چت: `{BOT_STATE['assistant']}`\n"
            f"🕒 ساعت فعلی ایران: **{fa_num(now_ir)}**\n"
            f"🎼 صوت‌های آرشیو: **{fa_num(t_cnt)}**\n"
            f"🎧 وضعیت ویس‌چت این چت: {vc_st}\n"
            f"⏰ زمان‌بندی‌های این چت: **{fa_num(s_cnt)}**"
        )
        return

    # راهنما
    if text == "راهنما" or text.startswith("/help"):
        await msg.reply_text(
            "📖 **راهنمای کامل ربات پخش زنده ویس‌چت:**\n\n"
            "• `لیست` : نمایش آرشیو صوت‌ها با کد پخش\n"
            "• `پخش [نام یا کد]` : باز کردن ویس‌چت گروه (حالت فقط شنیداری) و پخش زنده\n"
            "• `مکث` | `ادامه` | `توقف` : کنترل پخش زنده در ویس‌چت\n"
            "• `زمانبندی [کد] 21:30` : پخش خودکار یک‌باره به وقت ایران\n"
            "• `زمانبندی روزانه [کد] 07:00` : پخش خودکار هر روز\n"
            "• `زمانبندی ها` : دیدن و حذف زمان‌بندی‌ها\n"
            "• `حذف [کد]` : حذف صوت از آرشیو (در پی‌وی)\n"
            f"• ارسال در پی‌وی شخص دیگر: تایپ `@{BOT_USERNAME} نام_صوت`"
        )
        return

    # حذف صوت در پی‌وی
    if is_pv and text.startswith("حذف "):
        if not can_manage(user_id):
            return
        code = en_num(text[4:].strip())
        if code.isdigit():
            cur.execute("DELETE FROM tracks WHERE id = ?", (int(code),))
            conn.commit()
            await msg.reply_text(f"🗑 صوت کد `{code}` حذف شد." if cur.rowcount else "❌ صوتی با این کد پیدا نشد.")
        return

    # لیست آرشیو
    if text in ("لیست", "لیست صوت ها") or text.startswith("/list"):
        await send_track_list(chat_id, is_pv)
        return

    # پخش در گروه یا پی‌وی
    if text.startswith("پخش "):
        query = text[4:].strip()
        track = find_track(query)
        if not track:
            await msg.reply_text(f"❌ صوتی با نام یا کد «{query}» پیدا نشد. بنویس: `لیست`")
            return
        if is_pv:
            _, title, source, file_id, _ = track
            if source == "voice":
                await bot.send_voice(chat_id, file_id, caption=f"🎙 **{title}**\n(پخش زنده ویس‌چت مخصوص گروه‌هاست)")
            else:
                await bot.send_audio(chat_id, file_id, caption=f"🎵 **{title}**\n(پخش زنده ویس‌چت مخصوص گروه‌هاست)")
        else:
            await play_in_group_vc(chat_id, track)
        return

    # کنترل‌های ویس‌چت در گروه
    if not is_pv and text in ("مکث", "ادامه", "توقف", "پایان پخش"):
        act = {"مکث": "pause", "ادامه": "resume", "توقف": "stop", "پایان پخش": "stop"}[text]
        res = await control_vc(chat_id, act)
        await msg.reply_text(res)
        return

    # ثبت زمان‌بندی
    if text.startswith("زمانبندی ") or text.startswith("زمان‌بندی "):
        body = re.sub(r"^زمان‌?بندی\s+", "", en_num(text)).strip()
        daily = 1 if body.startswith("روزانه ") else 0
        if daily:
            body = re.sub(r"^روزانه\s+", "", body).strip()

        m = re.match(r"^(.+?)\s+(\d{1,2}):(\d{2})$", body)
        if not m:
            await msg.reply_text("❌ فرمت صحیح:\n`زمانبندی 1 21:30`\n`زمانبندی روزانه 1 07:00`")
            return

        q, hh, mm = m.group(1).strip(), int(m.group(2)), int(m.group(3))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            await msg.reply_text("❌ ساعت باید بین 00:00 تا 23:59 باشد.")
            return

        track = find_track(q)
        if not track:
            await msg.reply_text(f"❌ صوت «{q}» پیدا نشد.")
            return

        run_time = f"{hh:02d}:{mm:02d}"
        cur.execute(
            "INSERT INTO schedules (chat_id, track_id, run_time, daily) VALUES (?, ?, ?, ?)",
            (chat_id, track[0], run_time, daily),
        )
        conn.commit()
        await msg.reply_text(
            f"✅ **زمان‌بندی ثبت شد!**\n\n"
            f"🎵 صوت: **{track[1]}**\n"
            f"⏰ ساعت: **{fa_num(run_time)}** (وقت ایران)\n"
            f"🔁 نوع: **{'روزانه' if daily else 'یک‌باره'}**"
        )
        return

    # لیست زمان‌بندی‌ها
    if text in ("زمانبندی ها", "زمان‌بندی ها", "زمانبندی‌ها"):
        cur.execute(
            "SELECT s.id, t.title, s.run_time, s.daily FROM schedules s JOIN tracks t ON s.track_id = t.id WHERE s.chat_id = ?",
            (chat_id,),
        )
        rows = cur.fetchall()
        if not rows:
            await msg.reply_text("⏰ زمان‌بندی فعالی برای این چت وجود ندارد.")
            return
        lines = ["⏰ **زمان‌بندی‌های فعال (وقت ایران):**\n"]
        btns = []
        for sid, title, rtime, daily in rows:
            lines.append(f"• **{title}** — ساعت `{rtime}` ({'روزانه' if daily else 'یک‌باره'})")
            btns.append([InlineKeyboardButton(f"🗑 حذف: {title} ({rtime})", callback_data=f"delsched_{sid}")])
        await msg.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def send_track_list(chat_id: int, is_pv: bool):
    tracks = get_all_tracks(20)
    if not tracks:
        await bot.send_message(chat_id, "📂 آرشیو خالی است! در پی‌وی ربات یک صوت یا ویس بفرستید.")
        return
    lines = ["🎼 **آرشیو صوت‌ها:**\n"]
    btns = []
    for tid, title, _, _, _ in tracks:
        lines.append(f"• کد `{tid}` : **{title}**")
        if not is_pv:
            btns.append([InlineKeyboardButton(f"🔴 پخش در ویس‌چت: {title}", callback_data=f"vcplay_{tid}")])
        else:
            btns.append([InlineKeyboardButton(f"🎵 دریافت فایل: {title}", callback_data=f"sendfile_{tid}")])
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=InlineKeyboardMarkup(btns[:10]))


# ============================================================
# دکمه‌های شیشه‌ای
# ============================================================
@bot.on_callback_query()
async def on_callback(client, cb):
    chat_id = cb.message.chat.id
    is_pv = cb.message.chat.type == ChatType.PRIVATE
    data = cb.data

    if data == "show_list":
        await cb.answer()
        await send_track_list(chat_id, is_pv)
    elif data == "show_status":
        await cb.answer("✅ ربات آنلاین است!")
        now_ir = datetime.now(IRAN_TZ).strftime("%H:%M")
        await bot.send_message(
            chat_id,
            f"✅ ربات فعال است!\n🤖 ربات: `{BOT_STATE['bot']}`\n🎙 دستیار: `{BOT_STATE['assistant']}`\n🕒 ساعت ایران: {fa_num(now_ir)}",
        )
    elif data.startswith("vcplay_"):
        tid = data.replace("vcplay_", "")
        track = find_track(tid)
        if not track:
            return await cb.answer("❌ صوت پیدا نشد.", show_alert=True)
        await cb.answer("🔴 شروع پخش زنده در ویس‌چت...")
        await play_in_group_vc(chat_id, track)
    elif data in ("vc_pause", "vc_resume", "vc_stop"):
        act = data.replace("vc_", "")
        res = await control_vc(chat_id, act)
        await cb.answer(res, show_alert=False)
    elif data.startswith("sendfile_"):
        tid = data.replace("sendfile_", "")
        track = find_track(tid)
        if not track:
            return await cb.answer("فایل پیدا نشد.", show_alert=True)
        await cb.answer("📩 در حال ارسال فایل...")
        _, title, source, file_id, _ = track
        if source == "voice":
            await bot.send_voice(chat_id, file_id, caption=f"🎙 **{title}**")
        else:
            await bot.send_audio(chat_id, file_id, caption=f"🎵 **{title}**")
    elif data.startswith("delsched_"):
        sid = int(data.replace("delsched_", ""))
        cur.execute("DELETE FROM schedules WHERE id = ? AND chat_id = ?", (sid, chat_id))
        conn.commit()
        await cb.answer("🗑 زمان‌بندی حذف شد.")
        await cb.message.edit_reply_markup(None)


# ============================================================
# وب‌سرور وضعیت و زنده نگه داشتن روی Render
# ============================================================
async def health_handler(request):
    body = (
        f"Bot: {BOT_STATE['bot']}\n"
        f"Assistant: {BOT_STATE['assistant']}\n"
        f"Scheduler: {BOT_STATE['scheduler']}\n"
        f"Last Message: {BOT_STATE['last_msg']}\n"
    )
    return web.Response(text=body, content_type="text/plain", charset="utf-8")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"Health server started on port {PORT}")


async def main():
    global BOT_USERNAME, ASSISTANT_ID
    log("=== Starting Telegram VoiceChat Bot ===")
    await start_health_server()

    # ۱. اتصال ربات اصلی (مستقل از دستیار تا همیشه پاسخ بدهد)
    try:
        log("Connecting main bot...")
        await bot.start()
        me_bot = await bot.get_me()
        BOT_USERNAME = me_bot.username
        BOT_STATE["bot"] = f"OK (@{BOT_USERNAME})"
        log(f"Main bot connected: @{BOT_USERNAME}")
    except Exception as e:
        BOT_STATE["bot"] = f"ERROR: {e}"
        log(f"[FATAL] Main bot failed: {e}")
        traceback.print_exc()

    # ۲. اتصال اکانت دستیار و موتور ویس‌چت
    try:
        log("Connecting assistant & PyTgCalls...")
        await call_py.start()
        if not assistant.is_connected:
            await assistant.start()
        me_ast = await assistant.get_me()
        ASSISTANT_ID = me_ast.id
        BOT_STATE["assistant"] = f"OK (ID: {ASSISTANT_ID})"
        log(f"Assistant & PyTgCalls connected: {ASSISTANT_ID}")
    except Exception as e:
        BOT_STATE["assistant"] = f"ERROR: {e}"
        log(f"[ERROR] Assistant/PyTgCalls failed: {e}")
        traceback.print_exc()

    # ۳. شروع زمان‌بند
    scheduler.add_job(check_schedules, "cron", second=0)
    scheduler.start()
    BOT_STATE["scheduler"] = "OK"
    log("=== System Ready ===")

    await asyncio.Event().wait()


if __name__ == "__main__":
    loop.run_until_complete(main())
