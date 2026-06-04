#!/usr/bin/env python3
"""
Telegram Support Ticket Bot
Тикетная система поддержки через Telegram с форум-темами.
Конфигурация загружается из /opt/support-bot/config.env
"""

import os
import sys
import asyncio
import aiosqlite
import logging
import html
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# === ЗАГРУЗКА КОНФИГУРАЦИИ ===
CONFIG_PATH = os.environ.get("BOT_CONFIG", "/opt/support-bot/config.env")


def load_config(path: str) -> dict:
    """Читает config.env и возвращает dict."""
    cfg = {}
    p = Path(path)
    if not p.exists():
        print(f"FATAL: Config not found: {path}")
        sys.exit(1)
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


config = load_config(CONFIG_PATH)

BOT_TOKEN = config.get("BOT_TOKEN", "")
GROUP_ID = int(config.get("GROUP_ID", "0"))
ADMIN_IDS = [int(x.strip()) for x in config.get("ADMIN_IDS", "").split(",") if x.strip()]
PROJECT_NAME = config.get("PROJECT_NAME", "Support Bot")
MAX_FILE_SIZE = int(config.get("MAX_FILE_SIZE", str(20 * 1024 * 1024)))
MAX_OPEN_TICKETS = int(config.get("MAX_OPEN_TICKETS", "1"))
MAX_MESSAGES_PER_MINUTE = int(config.get("MAX_MESSAGES_PER_MINUTE", "10"))

DATA_DIR = config.get("DATA_DIR", "/opt/support-bot/data")
DB_PATH = os.path.join(DATA_DIR, "tickets.db")
LOG_PATH = os.path.join(DATA_DIR, "bot.log")
MAX_LOG_SIZE = int(config.get("MAX_LOG_SIZE_MB", "50")) * 1024 * 1024
LOG_BACKUP_COUNT = int(config.get("LOG_BACKUP_COUNT", "5"))

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN is empty in config")
    sys.exit(1)
if not GROUP_ID:
    print("FATAL: GROUP_ID is empty in config")
    sys.exit(1)
if not ADMIN_IDS:
    print("FATAL: ADMIN_IDS is empty in config")
    sys.exit(1)

# === ЛОГИРОВАНИЕ ===
os.makedirs(DATA_DIR, exist_ok=True)

logger = logging.getLogger("support_bot")
logger.setLevel(logging.INFO)

fh = RotatingFileHandler(
    LOG_PATH,
    maxBytes=MAX_LOG_SIZE,
    backupCount=LOG_BACKUP_COUNT,
)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)

sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(sh)

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# === БАЗА ДАННЫХ ===
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            status TEXT DEFAULT 'open',
            topic_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS rate_limits (
            user_id INTEGER,
            message_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            reason TEXT,
            banned_by INTEGER,
            banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rl ON rate_limits(user_id, message_time)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id, status)"
        )
        await db.commit()
    logger.info("Database initialized")


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,))
        return await cur.fetchone() is not None


async def check_rate_limit(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        one_min_ago = datetime.now() - timedelta(minutes=1)
        await db.execute(
            "DELETE FROM rate_limits WHERE message_time < ?", (one_min_ago,)
        )
        cur = await db.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE user_id = ? AND message_time >= ?",
            (user_id, one_min_ago),
        )
        count = (await cur.fetchone())[0]
        if count >= MAX_MESSAGES_PER_MINUTE:
            return False
        await db.execute("INSERT INTO rate_limits (user_id) VALUES (?)", (user_id,))
        await db.commit()
        return True


async def get_open_ticket(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, topic_id FROM tickets "
            "WHERE user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        return await cur.fetchone()


async def count_open_tickets(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE user_id = ? AND status = 'open'",
            (user_id,),
        )
        return (await cur.fetchone())[0]


def mention(user) -> str:
    name = user.username or f"ID: {user.id}"
    return f"<a href='tg://user?id={user.id}'>{html.escape(name)}</a>"


# === КЛАВИАТУРЫ ===
def user_kb(has_open_ticket: bool = False):
    buttons = [
        [InlineKeyboardButton(text="🆕 Создать тикет", callback_data="create_ticket")],
        [InlineKeyboardButton(text="📋 Мои тикеты", callback_data="my_tickets")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ]
    if has_open_ticket:
        buttons.insert(
            0,
            [InlineKeyboardButton(
                text="🔴 Закрыть текущий тикет", callback_data="close_my_ticket"
            )],
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [
            InlineKeyboardButton(text="🚫 Бан", callback_data="admin_ban"),
            InlineKeyboardButton(text="✅ Разбан", callback_data="admin_unban"),
        ],
        [
            InlineKeyboardButton(text="📋 Тикеты", callback_data="admin_tickets"),
            InlineKeyboardButton(text="📜 Баны", callback_data="admin_bans"),
        ],
        [InlineKeyboardButton(text="⬅️ Меню юзера", callback_data="back_user")],
    ])


def back_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Админ-меню", callback_data="admin_menu")]
    ])


# ==========================================
# 1. КОМАНДЫ (регистрируются первыми)
# ==========================================
@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        await m.answer(
            f"{PROJECT_NAME}\n🔧 <b>Панель администратора</b>",
            reply_markup=admin_kb(), parse_mode="HTML",
        )
    else:
        if await is_banned(m.from_user.id):
            await m.answer("🚫 Вы заблокированы.")
            return
        has_ticket = await count_open_tickets(m.from_user.id) > 0
        await m.answer(
            f"{PROJECT_NAME}\n🎫 <b>Меню поддержки</b>",
            reply_markup=user_kb(has_ticket), parse_mode="HTML",
        )


@dp.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        await m.answer(
            f"{PROJECT_NAME}\n🔧 <b>Панель администратора</b>",
            reply_markup=admin_kb(), parse_mode="HTML",
        )
    else:
        await m.answer("⛔ Нет доступа")


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        await m.answer("❌ Отменено", reply_markup=admin_kb(), parse_mode="HTML")
    else:
        has_ticket = await count_open_tickets(m.from_user.id) > 0
        await m.answer("❌ Отменено", reply_markup=user_kb(has_ticket), parse_mode="HTML")


@dp.message(F.chat.type == "private", Command("close"))
async def cmd_close_user(m: Message):
    if await is_banned(m.from_user.id):
        return
    ticket = await get_open_ticket(m.from_user.id)
    if not ticket:
        await m.answer("⚠️ Нет открытых тикетов")
        return
    tid, topic_id = ticket
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET status='closed', closed_at=CURRENT_TIMESTAMP WHERE id=?",
            (tid,),
        )
        await db.commit()
    await m.answer(
        f"🔴 Тикет #{tid} закрыт.",
        reply_markup=user_kb(False), parse_mode="HTML",
    )
    try:
        await bot.send_message(
            GROUP_ID, f"🔴 Юзер закрыл тикет #{tid}", message_thread_id=topic_id
        )
        await bot.edit_forum_topic(GROUP_ID, topic_id, name=f"🔴 Закрыт #{tid}")
        await bot.close_forum_topic(GROUP_ID, topic_id)
    except Exception as e:
        logger.error(f"Close topic err: {e}")


# ==========================================
# 2. CALLBACKS (кнопки)
# ==========================================
@dp.callback_query(F.data == "admin_menu")
async def cb_admin_menu(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    await state.clear()
    await c.message.edit_text(
        f"{PROJECT_NAME}\n🔧 <b>Панель администратора</b>",
        reply_markup=admin_kb(), parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data == "admin_stats")
async def cb_stats(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        t = (await (await db.execute("SELECT COUNT(*) FROM tickets")).fetchone())[0]
        o = (await (await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='open'"
        )).fetchone())[0]
        cl = (await (await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='closed'"
        )).fetchone())[0]
        b = (await (await db.execute("SELECT COUNT(*) FROM bans")).fetchone())[0]
        u = (await (await db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM tickets"
        )).fetchone())[0]
    txt = (
        f"{PROJECT_NAME}\n📊 <b>Статистика</b>\n"
        f"👥 Юзеров: <b>{u}</b>\n🎫 Тикетов: <b>{t}</b>\n"
        f"🟢 Открытых: <b>{o}</b>\n🔴 Закрытых: <b>{cl}</b>\n"
        f"🚫 Банов: <b>{b}</b>"
    )
    await c.message.edit_text(txt, reply_markup=back_admin_kb(), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "admin_ban")
async def cb_ban_menu(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT user_id, username FROM tickets "
            "WHERE status='open' ORDER BY id DESC LIMIT 20"
        )
        rows = await cur.fetchall()
    if not rows:
        await c.message.edit_text(
            f"{PROJECT_NAME}\n🚫 <b>Нет пользователей для бана</b>\n(все тикеты закрыты)",
            reply_markup=back_admin_kb(), parse_mode="HTML",
        )
    else:
        buttons = []
        for uid, uname in rows:
            display = f"@{uname}" if uname and uname not in ("hidden", str(uid)) else f"ID: {uid}"
            buttons.append([InlineKeyboardButton(
                text=f"🚫 {display}", callback_data=f"ban_user:{uid}"
            )])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_menu")])
        await c.message.edit_text(
            f"{PROJECT_NAME}\n🚫 <b>Выберите пользователя для бана:</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )
    await c.answer()


@dp.callback_query(F.data.startswith("ban_user:"))
async def cb_ban_user(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    try:
        uid = int(c.data.split(":")[1])
    except (IndexError, ValueError):
        return await c.answer("❌ Ошибка ID", show_alert=True)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT username FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (uid,),
        )
        row = await cur.fetchone()
        username = row[0] if row else "unknown"
        await db.execute(
            "INSERT OR REPLACE INTO bans (user_id, username, reason, banned_by) "
            "VALUES (?, ?, ?, ?)",
            (uid, username, "admin", c.from_user.id),
        )
        await db.commit()
    logger.info(f"User {uid} banned by admin {c.from_user.id}")
    await c.message.edit_text(
        f"{PROJECT_NAME}\n✅ <b>ID {uid} заблокирован</b>",
        reply_markup=back_admin_kb(), parse_mode="HTML",
    )
    try:
        await bot.send_message(
            uid, f"{PROJECT_NAME}\n🚫 Вы заблокированы в поддержке.", parse_mode="HTML"
        )
    except Exception:
        pass
    await c.answer("Забанен")


@dp.callback_query(F.data == "admin_unban")
async def cb_unban_menu(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username FROM bans ORDER BY banned_at DESC LIMIT 20"
        )
        rows = await cur.fetchall()
    if not rows:
        await c.message.edit_text(
            f"{PROJECT_NAME}\n✅ <b>Список банов пуст</b>",
            reply_markup=back_admin_kb(), parse_mode="HTML",
        )
    else:
        buttons = []
        for uid, uname in rows:
            display = f"@{uname}" if uname and uname not in ("unknown", "hidden") else f"ID: {uid}"
            buttons.append([InlineKeyboardButton(
                text=f"✅ {display}", callback_data=f"unban_user:{uid}"
            )])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_menu")])
        await c.message.edit_text(
            f"{PROJECT_NAME}\n✅ <b>Выберите пользователя для разбана:</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )
    await c.answer()


@dp.callback_query(F.data.startswith("unban_user:"))
async def cb_unban_user(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    try:
        uid = int(c.data.split(":")[1])
    except (IndexError, ValueError):
        return await c.answer("❌ Ошибка ID", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bans WHERE user_id=?", (uid,))
        await db.commit()
    logger.info(f"User {uid} unbanned by admin {c.from_user.id}")
    await c.message.edit_text(
        f"{PROJECT_NAME}\n✅ <b>ID {uid} разблокирован</b>",
        reply_markup=back_admin_kb(), parse_mode="HTML",
    )
    try:
        await bot.send_message(
            uid, f"{PROJECT_NAME}\n✅ Вы разблокированы.", parse_mode="HTML"
        )
    except Exception:
        pass
    await c.answer("Разбанен")


@dp.callback_query(F.data == "admin_bans")
async def cb_bans_list(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username, banned_at FROM bans "
            "ORDER BY banned_at DESC LIMIT 15"
        )
        rows = await cur.fetchall()
    if not rows:
        await c.message.edit_text(
            f"{PROJECT_NAME}\n📜 <b>Банов нет</b>",
            reply_markup=back_admin_kb(), parse_mode="HTML",
        )
    else:
        txt = f"{PROJECT_NAME}\n📜 <b>Список банов:</b>\n"
        for uid, uname, date in rows:
            txt += f"🚫 <code>{uid}</code> ({uname or '-'}) — {date[:16]}\n"
        await c.message.edit_text(txt, reply_markup=back_admin_kb(), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "admin_tickets")
async def cb_tickets_list(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, username, created_at FROM tickets "
            "WHERE status='open' ORDER BY id DESC LIMIT 15"
        )
        rows = await cur.fetchall()
    if not rows:
        await c.message.edit_text(
            f"{PROJECT_NAME}\n📋 <b>Открытых тикетов нет</b>",
            reply_markup=back_admin_kb(), parse_mode="HTML",
        )
    else:
        txt = f"{PROJECT_NAME}\n📋 <b>Активные тикеты:</b>\n"
        for tid, uid, uname, date in rows:
            txt += f"🟢 <b>#{tid}</b> — {uname or uid} ({date[:16]})\n"
        await c.message.edit_text(txt, reply_markup=back_admin_kb(), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "back_user")
async def cb_back_user(c: CallbackQuery, state: FSMContext):
    await state.clear()
    has_ticket = await count_open_tickets(c.from_user.id) > 0
    await c.message.edit_text(
        f"{PROJECT_NAME}\n🎫 <b>Меню поддержки</b>",
        reply_markup=user_kb(has_ticket), parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data == "create_ticket")
async def cb_create(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS and await is_banned(c.from_user.id):
        return await c.answer("🚫 Вы забанены", show_alert=True)
    if not await check_rate_limit(c.from_user.id):
        return await c.answer("⚠️ Лимит запросов", show_alert=True)
    if await count_open_tickets(c.from_user.id) >= MAX_OPEN_TICKETS:
        return await c.answer("⚠️ У вас уже есть открытый тикет", show_alert=True)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tickets (user_id, username) VALUES (?, ?)",
            (c.from_user.id, c.from_user.username or str(c.from_user.id)),
        )
        await db.commit()
        tid = cur.lastrowid

    try:
        topic = await bot.create_forum_topic(
            GROUP_ID,
            name=f"🎫 #{tid} | {c.from_user.username or c.from_user.id}",
        )
    except Exception as e:
        logger.error(f"Topic creation err: {e}")
        return await c.answer(
            "❌ Ошибка группы (Включите Темы в настройках группы!)", show_alert=True
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET topic_id=? WHERE id=?",
            (topic.message_thread_id, tid),
        )
        await db.commit()

    logger.info(f"Ticket #{tid} created by user {c.from_user.id}")
    await c.message.edit_text(
        f"{PROJECT_NAME}\n✅ <b>Тикет #{tid} создан!</b>\n"
        f"Отправьте текст, фото, файл или кружок.\n"
        f"🔴 Закрыть: /close",
        parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data == "my_tickets")
async def cb_my_tickets(c: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, status, created_at FROM tickets "
            "WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (c.from_user.id,),
        )
        rows = await cur.fetchall()
    if not rows:
        await c.answer("Тикетов нет", show_alert=True)
        return
    txt = f"{PROJECT_NAME}\n📋 <b>Ваши тикеты:</b>\n"
    for tid, st, date in rows:
        e = "🟢" if st == "open" else "🔴"
        txt += f"{e} <b>#{tid}</b> ({date[:16]})\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_user")]
    ])
    await c.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    txt = (
        f"{PROJECT_NAME}\nℹ️ <b>Как это работает:</b>\n"
        f"1. Создаёте тикет\n2. Пишете суть (можно файлы)\n"
        f"3. Отвечаем вам здесь.\n"
        f"⚙️ <b>Лимиты:</b>\n"
        f"• {MAX_OPEN_TICKETS} тикет\n"
        f"• {MAX_MESSAGES_PER_MINUTE} сообщ/мин\n"
        f"• Файлы до {MAX_FILE_SIZE // (1024*1024)} МБ"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_user")]
    ])
    await c.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "close_my_ticket")
async def cb_close_my_ticket(c: CallbackQuery):
    if await is_banned(c.from_user.id):
        return await c.answer("🚫 Вы забанены", show_alert=True)
    ticket = await get_open_ticket(c.from_user.id)
    if not ticket:
        return await c.answer("⚠️ Нет открытых тикетов", show_alert=True)
    tid, topic_id = ticket
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET status='closed', closed_at=CURRENT_TIMESTAMP WHERE id=?",
            (tid,),
        )
        await db.commit()
    logger.info(f"Ticket #{tid} closed by user {c.from_user.id}")
    await c.message.edit_text(
        f"{PROJECT_NAME}\n🔴 <b>Тикет #{tid} закрыт вами.</b>",
        reply_markup=user_kb(False), parse_mode="HTML",
    )
    try:
        await bot.send_message(
            GROUP_ID, f"🔴 Юзер закрыл тикет #{tid}", message_thread_id=topic_id
        )
        await bot.edit_forum_topic(GROUP_ID, topic_id, name=f"🔴 Закрыт #{tid}")
        await bot.close_forum_topic(GROUP_ID, topic_id)
    except Exception as e:
        logger.error(f"Close topic err: {e}")
    await c.answer("Закрыт")


@dp.callback_query(F.data.startswith("close:"))
async def cb_close_ticket(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔", show_alert=True)
    tid = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, topic_id FROM tickets WHERE id=?", (tid,)
        )
        row = await cur.fetchone()
        if not row:
            return await c.answer("Не найден", show_alert=True)
        uid, topic_id = row
        await db.execute(
            "UPDATE tickets SET status='closed', closed_at=CURRENT_TIMESTAMP WHERE id=?",
            (tid,),
        )
        await db.commit()
    logger.info(f"Ticket #{tid} closed by admin {c.from_user.id}")
    try:
        await bot.send_message(
            uid, f"{PROJECT_NAME}\n🔴 <b>Тикет #{tid} закрыт.</b>", parse_mode="HTML"
        )
    except Exception:
        pass
    try:
        await bot.edit_forum_topic(GROUP_ID, topic_id, name=f"🔴 Закрыт #{tid}")
        await bot.close_forum_topic(GROUP_ID, topic_id)
        await c.message.edit_text(
            c.message.text + f"\n🔴 <b>Закрыт админом</b>",
            parse_mode="HTML", reply_markup=None,
        )
    except Exception as e:
        logger.error(f"Close err: {e}")
    await c.answer("Закрыт")


# ==========================================
# 3. СООБЩЕНИЯ ЮЗЕРА В ТИКЕТ (catch-all ЛС)
# ==========================================
@dp.message(F.chat.type == "private")
async def user_message_catchall(m: Message):
    if m.text and m.text.startswith("/"):
        return
    if await is_banned(m.from_user.id):
        return
    if not await check_rate_limit(m.from_user.id):
        await m.answer("⚠️ Слишком много сообщений. Подождите.")
        return

    file_size = 0
    if m.document:
        file_size = m.document.file_size or 0
    elif m.video:
        file_size = m.video.file_size or 0
    elif m.photo:
        file_size = m.photo[-1].file_size or 0
    elif m.animation:
        file_size = m.animation.file_size or 0

    if file_size > MAX_FILE_SIZE:
        await m.answer(
            f"⚠️ Файл слишком большой (макс. {MAX_FILE_SIZE // (1024 * 1024)} МБ)"
        )
        return

    ticket = await get_open_ticket(m.from_user.id)
    if not ticket:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🆕 Создать тикет", callback_data="create_ticket")]
        ])
        await m.answer(
            f"{PROJECT_NAME}\n⚠️ У вас нет открытого тикета.",
            reply_markup=kb, parse_mode="HTML",
        )
        return

    tid, topic_id = ticket
    header = f"🎫 <b>Тикет #{tid}</b>\n👤 {mention(m.from_user)}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Закрыть тикет", callback_data=f"close:{tid}")]
    ])

    safe_text = html.escape(m.text) if m.text else ""
    safe_caption = html.escape(m.caption) if m.caption else ""

    try:
        if m.content_type == "text":
            await bot.send_message(
                GROUP_ID, header + safe_text,
                message_thread_id=topic_id, reply_markup=kb, parse_mode="HTML",
            )
        elif m.content_type == "photo":
            await bot.send_photo(
                GROUP_ID, m.photo[-1].file_id, caption=header + safe_caption,
                message_thread_id=topic_id, reply_markup=kb, parse_mode="HTML",
            )
        elif m.content_type == "document":
            await bot.send_document(
                GROUP_ID, m.document.file_id, caption=header + safe_caption,
                message_thread_id=topic_id, reply_markup=kb, parse_mode="HTML",
            )
        elif m.content_type == "video":
            await bot.send_video(
                GROUP_ID, m.video.file_id, caption=header + safe_caption,
                message_thread_id=topic_id, reply_markup=kb, parse_mode="HTML",
            )
        elif m.content_type == "voice":
            await bot.send_voice(
                GROUP_ID, m.voice.file_id, caption=header + safe_caption,
                message_thread_id=topic_id, reply_markup=kb, parse_mode="HTML",
            )
        elif m.content_type == "video_note":
            await bot.send_video_note(
                GROUP_ID, m.video_note.file_id, message_thread_id=topic_id,
            )
            await bot.send_message(
                GROUP_ID, header,
                message_thread_id=topic_id, reply_markup=kb, parse_mode="HTML",
            )
        elif m.content_type == "animation":
            await bot.send_animation(
                GROUP_ID, m.animation.file_id, caption=header + safe_caption,
                message_thread_id=topic_id, reply_markup=kb, parse_mode="HTML",
            )
        elif m.content_type == "sticker":
            await bot.send_sticker(
                GROUP_ID, m.sticker.file_id, message_thread_id=topic_id,
            )
            await bot.send_message(
                GROUP_ID, header,
                message_thread_id=topic_id, reply_markup=kb, parse_mode="HTML",
            )
        else:
            await m.answer("⚠️ Этот тип сообщения не поддерживается.")
            return
        await m.answer(f"✅ Отправлено в тикет #{tid}")
    except Exception as e:
        logger.error(f"Send to group err: {e}")
        await m.answer("❌ Ошибка отправки в группу")


# ==========================================
# 4. ОТВЕТЫ АДМИНА В ГРУППЕ
# ==========================================
@dp.message(F.chat.id == GROUP_ID, F.is_topic_message)
async def admin_reply_in_topic(m: Message):
    if m.from_user.is_bot:
        return
    if m.from_user.id not in ADMIN_IDS:
        return
    if m.text and m.text.startswith("/"):
        await m.reply(
            "⚠️ Команды администратора работают только в личных сообщениях с ботом."
        )
        return

    topic_id = m.message_thread_id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id FROM tickets WHERE topic_id=? AND status='open'",
            (topic_id,),
        )
        row = await cur.fetchone()
    if not row:
        await m.reply("⚠️ Тикет не найден или уже закрыт.")
        return
    tid, uid = row

    header = f"{PROJECT_NAME}\n💬 <b>Ответ поддержки (#{tid}):</b>\n"
    safe_text = html.escape(m.text) if m.text else ""
    safe_caption = html.escape(m.caption) if m.caption else ""

    try:
        if m.content_type == "text":
            await bot.send_message(uid, header + safe_text, parse_mode="HTML")
        elif m.content_type == "photo":
            await bot.send_photo(
                uid, m.photo[-1].file_id, caption=header + safe_caption, parse_mode="HTML"
            )
        elif m.content_type == "document":
            await bot.send_document(
                uid, m.document.file_id, caption=header + safe_caption, parse_mode="HTML"
            )
        elif m.content_type == "video":
            await bot.send_video(
                uid, m.video.file_id, caption=header + safe_caption, parse_mode="HTML"
            )
        elif m.content_type == "voice":
            await bot.send_voice(
                uid, m.voice.file_id, caption=header + safe_caption, parse_mode="HTML"
            )
        elif m.content_type == "video_note":
            await bot.send_video_note(uid, m.video_note.file_id)
        elif m.content_type == "animation":
            await bot.send_animation(
                uid, m.animation.file_id, caption=header + safe_caption, parse_mode="HTML"
            )
        elif m.content_type == "sticker":
            await bot.send_sticker(uid, m.sticker.file_id)
        else:
            await m.reply("⚠️ Неподдерживаемый тип")
            return
        await m.reply("✅ Отправлено клиенту")
    except Exception as e:
        logger.error(f"Reply to user err: {e}")
        await m.reply("❌ Ошибка (клиент заблокировал бота?)")


# ==========================================
# MAIN
# ==========================================
async def main():
    await init_db()
    logger.info(f"Bot started | Project: {PROJECT_NAME}")
    logger.info(f"Admin IDs: {ADMIN_IDS} | Group: {GROUP_ID}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
