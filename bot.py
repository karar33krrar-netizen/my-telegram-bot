import asyncio
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)


# ---------- الإعدادات (من ملف .env) ----------
def load_env():
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "5473961476"))

TG_URL = "https://t.me/n8ndd"
IG_URL = "https://instagram.com/_k09ll"

BIRTH_MONTH, BIRTH_DAY = 5, 2
TZ = timezone(timedelta(hours=3))  # توقيت بغداد

# الترحيب ثابت باللغتين دائماً
WELCOME = "مرحباً بك في عالمي\nWelcome to my world"

TEXT = {
    "ar": {
        "tg": "تليكرام", "ig": "انستغرام",
        "secret": "رسالة سرية", "bday": "ميلادي",
        "switch": "English", "back": "رجوع", "refresh": "تحديث",
        "ask": "اكتب رسالتك وراح توصلني بدون ما أعرف منو أنت.\nتكدر ترسل نص أو صورة أو فويس أو فيديو أو ملف.",
        "sent": "وصلت رسالتك بشكل سري.",
        "wait": "انتظر شوي قبل ما ترسل رسالة ثانية.",
        "error": "صار خطأ، حاول مرة ثانية.",
        "reply_from": "رد من روي:",
        "bday_today": "🎂 اليوم عيد ميلادي!\nشكراً لوجودك معي اليوم.",
        "greet": "اليوم عيد ميلادي، شكراً لوجودك معي.",
    },
    "en": {
        "tg": "Telegram", "ig": "Instagram",
        "secret": "Secret message", "bday": "My birthday",
        "switch": "عربي", "back": "Back", "refresh": "Refresh",
        "ask": "Write your message. It reaches me without revealing who you are.\nYou can send text, photos, voice, video or files.",
        "sent": "Your message was delivered anonymously.",
        "wait": "Please wait a moment before sending another.",
        "error": "Something went wrong, please try again.",
        "reply_from": "Reply from Roy:",
        "bday_today": "🎂 Today is my birthday!\nThanks for being here today.",
        "greet": "Today is my birthday, thank you for being here.",
    },
}


# ---------- قاعدة البيانات (SQLite) ----------
DB = sqlite3.connect(Path(__file__).with_name("bot.db"), check_same_thread=False)
DB.executescript("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    lang TEXT DEFAULT 'ar',
    joined INTEGER,
    blocked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
    owner_msg_id INTEGER PRIMARY KEY,
    user_id INTEGER,
    created INTEGER
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
""")
DB.commit()


def q(sql: str, args: tuple = ()):
    cur = DB.execute(sql, args)
    DB.commit()
    return cur


def add_user(uid: int) -> bool:
    cur = q("INSERT OR IGNORE INTO users(id, joined) VALUES(?, ?)", (uid, int(time.time())))
    return cur.rowcount == 1


def lang_of(uid: int) -> str:
    row = q("SELECT lang FROM users WHERE id=?", (uid,)).fetchone()
    return row[0] if row else "ar"


def set_lang(uid: int, lang: str):
    add_user(uid)
    q("UPDATE users SET lang=? WHERE id=?", (lang, uid))


def is_blocked(uid: int) -> bool:
    row = q("SELECT blocked FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row[0])


def get_meta(key: str):
    row = q("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str):
    q("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


LAST_SENT: dict[int, float] = {}
LIVE: dict[int, asyncio.Task] = {}


class Secret(StatesGroup):
    waiting = State()


# ---------- الأزرار ----------
def main_kb(lang: str) -> InlineKeyboardMarkup:
    t = TEXT[lang]
    other = "en" if lang == "ar" else "ar"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t["tg"], url=TG_URL),
            InlineKeyboardButton(text=t["ig"], url=IG_URL),
        ],
        [InlineKeyboardButton(text=t["secret"], callback_data="secret")],
        [
            InlineKeyboardButton(text=t["bday"], callback_data="bday"),
            InlineKeyboardButton(text=t["switch"], callback_data=f"lang:{other}"),
        ],
    ])


def back_kb(lang: str, refresh: bool = False) -> InlineKeyboardMarkup:
    t = TEXT[lang]
    row = [InlineKeyboardButton(text=t["back"], callback_data="home")]
    if refresh:
        row.insert(0, InlineKeyboardButton(text=t["refresh"], callback_data="bday"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def block_kb(owner_msg_id: int, blocked: bool = False) -> InlineKeyboardMarkup:
    text = "إلغاء الحظر" if blocked else "حظر"
    action = "unblock" if blocked else "block"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=f"{action}:{owner_msg_id}")]
    ])


# ---------- عداد عيد الميلاد ----------
def next_birthday(now: datetime) -> datetime:
    def make(year: int):
        return datetime(year, BIRTH_MONTH, BIRTH_DAY, tzinfo=TZ)
    this_year = make(now.year)
    # اليوم نفسه يُحسب عيد ميلاد
    if now.date() <= this_year.date():
        return this_year
    return make(now.year + 1)


def progress_bar(now: datetime, target: datetime, size: int = 12) -> tuple[str, int]:
    prev = datetime(target.year - 1, BIRTH_MONTH, BIRTH_DAY, tzinfo=TZ)
    total = (target - prev).total_seconds()
    done = (now - prev).total_seconds()
    pct = max(0, min(100, int(done / total * 100)))
    filled = round(size * pct / 100)
    return "▓" * filled + "░" * (size - filled), pct


def birthday_text(lang: str) -> str:
    now = datetime.now(TZ)
    target = next_birthday(now)

    if now.date() == target.date():
        return TEXT[lang]["bday_today"]

    diff = target - now
    days = diff.days
    hours, rem = divmod(diff.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    bar, pct = progress_bar(now, target)

    if lang == "ar":
        return (
            f"عيد ميلادي: 2 مايو {target.year}\n\n"
            f"{days} يوم\n{hours} ساعة\n{minutes} دقيقة\n{seconds} ثانية\n\n"
            f"{bar} {pct}%"
        )
    return (
        f"My birthday: May 2, {target.year}\n\n"
        f"{days} days\n{hours} hours\n{minutes} minutes\n{seconds} seconds\n\n"
        f"{bar} {pct}%"
    )


def stop_live(uid: int):
    task = LIVE.pop(uid, None)
    if task:
        task.cancel()


async def live_countdown(msg: Message, lang: str):
    """يحدّث العداد تلقائياً كل 5 ثواني لمدة دقيقة."""
    try:
        for _ in range(12):
            await asyncio.sleep(5)
            await msg.edit_text(birthday_text(lang), reply_markup=back_kb(lang, True))
    except Exception:
        return


# ---------- أدوات الإرسال للجميع ----------
async def for_each_user(send) -> tuple[int, int]:
    ids = [r[0] for r in q(
        "SELECT id FROM users WHERE blocked=0 AND id!=?", (OWNER_ID,)
    ).fetchall()]
    ok = fail = 0
    for uid in ids:
        try:
            await send(uid)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await send(uid)
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


# ---------- البوت ----------
dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    await state.clear()
    stop_live(uid)
    is_new = add_user(uid)
    lang = lang_of(uid)
    await message.answer(WELCOME, reply_markup=main_kb(lang))

    if is_new and uid != OWNER_ID:
        total = q("SELECT COUNT(*) FROM users").fetchone()[0]
        try:
            await bot.send_message(OWNER_ID, f"مستخدم جديد، العدد الكلي: {total}")
        except Exception:
            pass


# ----- أوامر المالك -----
@dp.message(Command("stats"), F.from_user.id == OWNER_ID)
async def stats(message: Message):
    total = q("SELECT COUNT(*) FROM users").fetchone()[0]
    blocked = q("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
    midnight = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today = q("SELECT COUNT(*) FROM users WHERE joined>=?", (midnight,)).fetchone()[0]
    msgs = int(get_meta("msg_count") or 0)
    await message.answer(
        f"الإحصائيات\n\n"
        f"المستخدمين: {total}\n"
        f"جدد اليوم: {today}\n"
        f"المحظورين: {blocked}\n"
        f"الرسائل السرية: {msgs}"
    )


@dp.message(Command("broadcast"), F.from_user.id == OWNER_ID)
async def broadcast(message: Message, bot: Bot, command: CommandObject):
    src = message.reply_to_message
    text = command.args
    if not src and not text:
        await message.answer("الاستخدام:\n/broadcast نص الرسالة\nأو ارسل رسالة ثم اعمل Reply عليها بـ /broadcast")
        return

    async def send(uid: int):
        if src:
            await bot.copy_message(uid, OWNER_ID, src.message_id)
        else:
            await bot.send_message(uid, text)

    await message.answer("جاري الإرسال...")
    ok, fail = await for_each_user(send)
    await message.answer(f"تم الإرسال إلى {ok} مستخدم، وفشل {fail}.")


# ----- أزرار الزائر -----
@dp.callback_query(F.data.startswith("lang:"))
async def switch_lang(call: CallbackQuery):
    uid = call.from_user.id
    lang = call.data.split(":")[1]
    stop_live(uid)
    set_lang(uid, lang)
    await call.message.edit_text(WELCOME, reply_markup=main_kb(lang))
    await call.answer()


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    await state.clear()
    stop_live(uid)
    lang = lang_of(uid)
    await call.message.edit_text(WELCOME, reply_markup=main_kb(lang))
    await call.answer()


@dp.callback_query(F.data == "bday")
async def bday(call: CallbackQuery):
    uid = call.from_user.id
    lang = lang_of(uid)
    stop_live(uid)
    try:
        await call.message.edit_text(birthday_text(lang), reply_markup=back_kb(lang, True))
    except TelegramBadRequest:
        pass  # نفس النص لم يتغير
    LIVE[uid] = asyncio.create_task(live_countdown(call.message, lang))
    await call.answer()


@dp.callback_query(F.data == "secret")
async def secret(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    stop_live(uid)
    lang = lang_of(uid)
    await state.set_state(Secret.waiting)
    await call.message.edit_text(TEXT[lang]["ask"], reply_markup=back_kb(lang))
    await call.answer()


# ----- حظر وإلغاء حظر (للمالك فقط) -----
@dp.callback_query(F.data.startswith(("block:", "unblock:")))
async def block_cb(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer()
        return
    action, mid = call.data.split(":")
    row = q("SELECT user_id FROM messages WHERE owner_msg_id=?", (int(mid),)).fetchone()
    if not row:
        await call.answer("غير موجود")
        return
    blocked = 1 if action == "block" else 0
    q("UPDATE users SET blocked=? WHERE id=?", (blocked, row[0]))
    try:
        await call.message.edit_reply_markup(reply_markup=block_kb(int(mid), bool(blocked)))
    except TelegramBadRequest:
        pass
    await call.answer("تم الحظر" if blocked else "تم إلغاء الحظر")


# ----- ردك على رسالة سرية (Reply) -> يوصل للزائر بدون كشفك -----
@dp.message(F.from_user.id == OWNER_ID, F.reply_to_message)
async def owner_reply(message: Message, bot: Bot):
    row = q(
        "SELECT user_id FROM messages WHERE owner_msg_id=?",
        (message.reply_to_message.message_id,),
    ).fetchone()
    if not row:
        return
    target = row[0]
    prefix = TEXT[lang_of(target)]["reply_from"]
    try:
        if message.text:
            await bot.send_message(target, f"{prefix}\n\n{message.text}")
        else:
            await bot.send_message(target, prefix)
            await bot.copy_message(target, message.chat.id, message.message_id)
    except TelegramForbiddenError:
        await message.answer("الزائر أوقف البوت، ما كدرت أوصله الرد.")
    except Exception:
        await message.answer("ما كدرت أرسل الرد.")


# ----- رسالة الزائر السرية (نص، صورة، فويس، فيديو، ملف...) -----
@dp.message(Secret.waiting)
async def receive_secret(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    lang = lang_of(uid)

    if time.time() - LAST_SENT.get(uid, 0) < 30:
        await message.answer(TEXT[lang]["wait"])
        return
    LAST_SENT[uid] = time.time()

    # المحظور يظهر له أنها وصلت، لكن ما توصلك
    if is_blocked(uid):
        await state.clear()
        await message.answer(TEXT[lang]["sent"], reply_markup=main_kb(lang))
        return

    try:
        n = int(get_meta("msg_count") or 0) + 1
        header = await bot.send_message(OWNER_ID, f"رسالة سرية #{n}")
        copied = await bot.copy_message(
            OWNER_ID, message.chat.id, message.message_id,
            reply_to_message_id=header.message_id,
        )
        await bot.edit_message_reply_markup(
            chat_id=OWNER_ID, message_id=copied.message_id,
            reply_markup=block_kb(copied.message_id),
        )
    except Exception:
        await message.answer(TEXT[lang]["error"])
        return

    now = int(time.time())
    q("INSERT OR REPLACE INTO messages VALUES(?, ?, ?)", (header.message_id, uid, now))
    q("INSERT OR REPLACE INTO messages VALUES(?, ?, ?)", (copied.message_id, uid, now))
    set_meta("msg_count", str(n))

    await state.clear()
    await message.answer(TEXT[lang]["sent"], reply_markup=main_kb(lang))


# ---------- تنبيهات عيد الميلاد ----------
async def scheduler(bot: Bot):
    while True:
        try:
            now = datetime.now(TZ)
            target = next_birthday(now)
            days_left = (target.date() - now.date()).days

            if days_left in (7, 1):
                key = f"remind{days_left}_{target.year}"
                if not get_meta(key):
                    set_meta(key, "1")
                    await bot.send_message(OWNER_ID, f"باقي {days_left} يوم على عيد ميلادك.")

            if days_left == 0:
                key = f"greet_{target.year}"
                if not get_meta(key):
                    set_meta(key, "1")
                    await bot.send_message(OWNER_ID, "اليوم عيد ميلادك، كل عام وأنت بخير.")

                    async def send(uid: int):
                        await bot.send_message(uid, TEXT[lang_of(uid)]["greet"])

                    await for_each_user(send)
        except Exception as e:
            print("scheduler error:", e)
        await asyncio.sleep(60)


async def main():
    if not TOKEN:
        raise SystemExit("ضع BOT_TOKEN داخل ملف .env بجانب bot.py")
    bot = Bot(TOKEN)
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())