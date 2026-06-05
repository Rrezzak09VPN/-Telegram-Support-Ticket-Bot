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
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
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
# Жёсткий антиспам на создание тикетов: не больше N тикетов от одного юзера за сутки
MAX_TICKETS_PER_DAY = int(config.get("MAX_TICKETS_PER_DAY", "5"))
# Cooldown между созданием тикетов одним пользователем (в минутах).
# 0 = выключено. Защищает от очереди создания «сразу подряд» даже в пределах суточного лимита.
TICKET_CREATE_COOLDOWN_MINUTES = int(config.get("TICKET_CREATE_COOLDOWN_MINUTES", "15"))

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
            closed_at TIMESTAMP,
            closed_by TEXT,
            closed_by_user_id INTEGER
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_created "
            "ON tickets(user_id, created_at)"
        )

        # --- МИГРАЦИИ для уже существующих БД ---
        # Безопасно добавляем новые колонки, если их ещё нет.
        cur = await db.execute("PRAGMA table_info(tickets)")
        existing_cols = {row[1] for row in await cur.fetchall()}
        if "closed_by" not in existing_cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN closed_by TEXT")
            logger.info("Migration: tickets.closed_by added")
        if "closed_by_user_id" not in existing_cols:
            await db.execute(
                "ALTER TABLE tickets ADD COLUMN closed_by_user_id INTEGER"
            )
            logger.info("Migration: tickets.closed_by_user_id added")

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


async def check_create_cooldown(user_id: int):
    """
    Возвращает (allowed: bool, retry_after_seconds: int).
    Проверяет, что с момента последнего созданного тикета пользователя прошло
    не меньше TICKET_CREATE_COOLDOWN_MINUTES минут. Если cooldown = 0 — всегда True.
    """
    if TICKET_CREATE_COOLDOWN_MINUTES <= 0:
        return True, 0
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT MAX(created_at) FROM tickets WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
    last = row[0] if row else None
    if not last:
        return True, 0
    try:
        last_dt = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        last_dt = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
    free_at = last_dt + timedelta(minutes=TICKET_CREATE_COOLDOWN_MINUTES)
    delta = (free_at - datetime.now()).total_seconds()
    if delta <= 0:
        return True, 0
    return False, int(delta) + 1


def _fmt_seconds_ru(seconds: int) -> str:
    """Человеко-читаемое 'через 12 мин 30 сек' / 'через 45 сек'."""
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} сек"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} мин {s} сек" if s else f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


async def close_ticket_in_db(
    tid: int,
    closed_by: str,
    closed_by_user_id: int,
) -> None:
    """
    Единая точка закрытия тикета в БД с логированием инициатора.
    closed_by: 'user' | 'admin' | 'system' (reconcile удалил/закрыл тему вручную)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET status='closed', "
            "closed_at=CURRENT_TIMESTAMP, closed_by=?, closed_by_user_id=? "
            "WHERE id=?",
            (closed_by, closed_by_user_id, tid),
        )
        await db.commit()
    logger.info(
        f"Ticket #{tid} closed | by={closed_by} | by_user_id={closed_by_user_id}"
    )


async def check_daily_ticket_limit(user_id: int):
    """
    Возвращает (allowed: bool, used: int, retry_after_minutes: int).
    Считает ВСЕ тикеты пользователя за последние 24 часа (включая закрытые),
    чтобы цикл «создать-закрыть-создать» не обходил лимит.
    """
    day_ago = datetime.now() - timedelta(days=1)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*), MIN(created_at) FROM tickets "
            "WHERE user_id = ? AND created_at >= ?",
            (user_id, day_ago),
        )
        row = await cur.fetchone()
        used = row[0] or 0
        oldest_in_window = row[1]

    if used < MAX_TICKETS_PER_DAY:
        return True, used, 0

    # Когда «освободится слот» = когда самому старому тикету в окне исполнится 24ч
    retry_after_minutes = 60 * 24
    if oldest_in_window:
        try:
            dt = datetime.fromisoformat(oldest_in_window)
        except (TypeError, ValueError):
            dt = datetime.strptime(oldest_in_window[:19], "%Y-%m-%d %H:%M:%S")
        free_at = dt + timedelta(days=1)
        delta = free_at - datetime.now()
        retry_after_minutes = max(1, int(delta.total_seconds() // 60) + 1)
    return False, used, retry_after_minutes


def _user_label(username: str | None, user_id: int) -> str:
    """
    Единый формат подписи юзера для имён тем.
    Telegram режет имя темы до 128 символов, так что укладываемся без проблем.
    Если есть username  -> "@username (123456789)"
    Если нет username   -> "123456789"
    """
    if (
        username
        and username.strip()
        and username.lower() not in ("hidden", "unknown", str(user_id))
    ):
        return f"@{username} ({user_id})"
    return str(user_id)


def topic_name_open(tid: int, username: str | None, user_id: int) -> str:
    # 🎫 #26 | @username (123456789)  /  🎫 #26 | 123456789
    return f"🎫 #{tid} | {_user_label(username, user_id)}"


# Маркер «кто закрыл» прямо в названии темы, чтобы было видно в списке тем.
_CLOSED_BY_MARKER = {
    "user": "👤",                              # сам пользователь
    "admin": "🛠",                              # админ
    "system:no_topic": "⚙️",                   # не было topic_id, авто-чистка
    "system:topic_deleted": "🗑",               # тема удалена вручную
    "system:topic_closed_manually": "🛠",       # тему закрыли руками в группе
}


def closed_by_label(closed_by: str | None) -> str:
    """Человеко-читаемая подпись 'кто закрыл'."""
    return {
        "user": "пользователем",
        "admin": "администратором",
        "system:no_topic": "автоматически",
        "system:topic_deleted": "автоматически (тема удалена)",
        "system:topic_closed_manually": "администратором (вручную в группе)",
    }.get(closed_by or "", "—")


def topic_name_closed(
    tid: int,
    username: str | None,
    user_id: int,
    closed_by: str = "user",
) -> str:
    # 🔴 🛠 #26 | @username (123456789)
    marker = _CLOSED_BY_MARKER.get(closed_by, "")
    prefix = f"🔴 {marker}".rstrip()
    return f"{prefix} #{tid} | {_user_label(username, user_id)}"


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


# ---- ReplyKeyboard (постоянное меню снизу) ----
# Эти кнопки висят возле поля ввода всегда, чтобы не приходилось писать /start.
# Текст кнопок одновременно — это команды для обработчиков ниже.
BTN_MENU = "🎫 Меню"
BTN_NEW = "🆕 Новый тикет"
BTN_MY = "📋 Мои тикеты"
BTN_CLOSE = "🔴 Закрыть тикет"
BTN_HELP = "ℹ️ Помощь"

BTN_ADMIN = "🔧 Админка"
BTN_STATS = "📊 Статистика"
BTN_TICKETS = "📋 Тикеты"
BTN_BANS = "📜 Баны"


def user_reply_kb(has_open_ticket: bool = False) -> ReplyKeyboardMarkup:
    """Постоянное нижнее меню для пользователя."""
    rows = [[KeyboardButton(text=BTN_MENU), KeyboardButton(text=BTN_HELP)]]
    if has_open_ticket:
        rows.insert(0, [KeyboardButton(text=BTN_CLOSE), KeyboardButton(text=BTN_MY)])
    else:
        rows.insert(0, [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_MY)])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Напишите сообщение или выберите действие…",
    )


def admin_reply_kb() -> ReplyKeyboardMarkup:
    """Постоянное нижнее меню для администратора."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADMIN), KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_TICKETS), KeyboardButton(text=BTN_BANS)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Админ-меню…",
    )


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
async def _send_user_menu(m: Message):
    """Открыть пользовательское меню: inline + закрепить нижнюю reply-клавиатуру."""
    has_ticket = await count_open_tickets(m.from_user.id) > 0
    # 1) нижняя постоянная клавиатура
    await m.answer(
        f"{PROJECT_NAME}\n🎫 <b>Меню поддержки</b>",
        reply_markup=user_reply_kb(has_open_ticket=has_ticket),
        parse_mode="HTML",
    )
    # 2) inline-меню действий
    await m.answer(
        "Выберите действие:",
        reply_markup=user_kb(has_ticket), parse_mode="HTML",
    )


async def _send_admin_menu(m: Message):
    """Открыть админ-меню: inline + закрепить нижнюю reply-клавиатуру."""
    await m.answer(
        f"{PROJECT_NAME}\n🔧 <b>Панель администратора</b>",
        reply_markup=admin_reply_kb(), parse_mode="HTML",
    )
    await m.answer(
        "Выберите действие:",
        reply_markup=admin_kb(), parse_mode="HTML",
    )


@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        await _send_admin_menu(m)
        return
    if await is_banned(m.from_user.id):
        await m.answer("🚫 Вы заблокированы.", reply_markup=ReplyKeyboardRemove())
        return
    await _send_user_menu(m)


@dp.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext):
    """Алиас для /start — открывает меню в любой момент."""
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        await _send_admin_menu(m)
    else:
        if await is_banned(m.from_user.id):
            await m.answer("🚫 Вы заблокированы.", reply_markup=ReplyKeyboardRemove())
            return
        await _send_user_menu(m)


@dp.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        await _send_admin_menu(m)
    else:
        await m.answer("⛔ Нет доступа")


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        await m.answer("❌ Отменено", reply_markup=admin_reply_kb())
    else:
        has_ticket = await count_open_tickets(m.from_user.id) > 0
        await m.answer(
            "❌ Отменено",
            reply_markup=user_reply_kb(has_open_ticket=has_ticket),
        )


@dp.message(F.chat.type == "private", Command("close"))
async def cmd_close_user(m: Message):
    if await is_banned(m.from_user.id):
        return
    ticket = await get_open_ticket(m.from_user.id)
    if not ticket:
        await m.answer("⚠️ Нет открытых тикетов")
        return
    tid, topic_id = ticket
    await close_ticket_in_db(tid, closed_by="user", closed_by_user_id=m.from_user.id)
    await m.answer(
        f"🔴 Тикет #{tid} закрыт <b>вами</b>.",
        reply_markup=user_reply_kb(has_open_ticket=False), parse_mode="HTML",
    )
    try:
        await bot.send_message(
            GROUP_ID,
            f"🔴 Тикет #{tid} закрыт <b>пользователем</b> {mention(m.from_user)}",
            message_thread_id=topic_id, parse_mode="HTML",
        )
        await bot.edit_forum_topic(
            GROUP_ID, topic_id,
            name=topic_name_closed(
                tid, m.from_user.username, m.from_user.id, closed_by="user"
            ),
        )
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
        # Разбивка закрытий по инициатору
        by_user = (await (await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='closed' AND closed_by='user'"
        )).fetchone())[0]
        by_admin = (await (await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='closed' AND closed_by='admin'"
        )).fetchone())[0]
        by_system = (await (await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='closed' "
            "AND closed_by LIKE 'system%'"
        )).fetchone())[0]
        by_unknown = (await (await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='closed' AND closed_by IS NULL"
        )).fetchone())[0]
    txt = (
        f"{PROJECT_NAME}\n📊 <b>Статистика</b>\n"
        f"👥 Юзеров: <b>{u}</b>\n🎫 Тикетов: <b>{t}</b>\n"
        f"🟢 Открытых: <b>{o}</b>\n🔴 Закрытых: <b>{cl}</b>\n"
        f"   • юзером: <b>{by_user}</b>\n"
        f"   • админом: <b>{by_admin}</b>\n"
        f"   • системой: <b>{by_system}</b>"
        + (f"\n   • без метки: <b>{by_unknown}</b>" if by_unknown else "")
        + f"\n🚫 Банов: <b>{b}</b>"
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


async def create_ticket_for(user) -> tuple[bool, str, int | None]:
    """
    Универсальная попытка создать тикет.
    Возвращает (ok, message_for_user, tid_or_None).
    Используется и из inline-callback'а, и из reply-кнопки.
    """
    if user.id not in ADMIN_IDS and await is_banned(user.id):
        return False, "🚫 Вы заблокированы.", None
    if not await check_rate_limit(user.id):
        return False, "⚠️ Слишком много запросов. Подождите.", None
    if await count_open_tickets(user.id) >= MAX_OPEN_TICKETS:
        return False, "⚠️ У вас уже есть открытый тикет.", None

    cd_ok, cd_retry = await check_create_cooldown(user.id)
    if not cd_ok:
        logger.warning(
            f"Ticket creation blocked by cooldown: user={user.id}, retry_in={cd_retry}s"
        )
        return (
            False,
            f"⚠️ Подождите {_fmt_seconds_ru(cd_retry)} перед созданием "
            f"следующего тикета.",
            None,
        )

    allowed, used, retry_after = await check_daily_ticket_limit(user.id)
    if not allowed:
        if retry_after >= 60:
            wait_str = f"{retry_after // 60} ч {retry_after % 60} мин"
        else:
            wait_str = f"{retry_after} мин"
        logger.warning(
            f"Ticket creation blocked by daily limit: user={user.id}, used={used}"
        )
        return (
            False,
            f"⚠️ Лимит {MAX_TICKETS_PER_DAY} тикетов в сутки исчерпан "
            f"({used}/{MAX_TICKETS_PER_DAY}). Попробуйте через {wait_str}.",
            None,
        )

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tickets (user_id, username) VALUES (?, ?)",
            (user.id, user.username or str(user.id)),
        )
        await db.commit()
        tid = cur.lastrowid

    try:
        topic = await bot.create_forum_topic(
            GROUP_ID,
            name=topic_name_open(tid, user.username, user.id),
        )
    except Exception as e:
        logger.error(f"Topic creation err: {e}")
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM tickets WHERE id=?", (tid,))
                await db.commit()
        except Exception as e2:
            logger.error(f"Rollback ticket {tid} err: {e2}")
        return False, "❌ Ошибка группы (Включите Темы в настройках группы!)", None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET topic_id=? WHERE id=?",
            (topic.message_thread_id, tid),
        )
        await db.commit()

    logger.info(f"Ticket #{tid} created by user {user.id}")
    return (
        True,
        f"{PROJECT_NAME}\n✅ <b>Тикет #{tid} создан!</b>\n"
        f"Отправьте текст, фото, файл или кружок.\n"
        f"🔴 Закрыть: кнопка ниже или /close",
        tid,
    )


@dp.callback_query(F.data == "create_ticket")
async def cb_create(c: CallbackQuery):
    ok, text, _ = await create_ticket_for(c.from_user)
    if not ok:
        return await c.answer(text, show_alert=True)
    try:
        await c.message.edit_text(text, parse_mode="HTML")
    except Exception:
        await c.message.answer(text, parse_mode="HTML")
    # обновляем нижнюю клавиатуру: теперь есть открытый тикет
    await bot.send_message(
        c.from_user.id, "Меню обновлено.",
        reply_markup=user_reply_kb(has_open_ticket=True),
    )
    await c.answer()


@dp.callback_query(F.data == "my_tickets")
async def cb_my_tickets(c: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, status, created_at, closed_by FROM tickets "
            "WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (c.from_user.id,),
        )
        rows = await cur.fetchall()
    if not rows:
        await c.answer("Тикетов нет", show_alert=True)
        return
    by_label = {
        "user": "вами",
        "admin": "админом",
        "system:no_topic": "системой",
        "system:topic_deleted": "автоматически (тема удалена)",
        "system:topic_closed_manually": "админом (вручную)",
    }
    txt = f"{PROJECT_NAME}\n📋 <b>Ваши тикеты:</b>\n"
    for tid, st, date, cb in rows:
        e = "🟢" if st == "open" else "🔴"
        suffix = ""
        if st == "closed" and cb:
            suffix = f" — закрыт {by_label.get(cb, cb)}"
        txt += f"{e} <b>#{tid}</b> ({date[:16]}){suffix}\n"
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
        f"• {MAX_OPEN_TICKETS} открытый тикет одновременно\n"
        f"• {MAX_TICKETS_PER_DAY} тикетов в сутки\n"
        + (
            f"• кулдаун между тикетами: {TICKET_CREATE_COOLDOWN_MINUTES} мин\n"
            if TICKET_CREATE_COOLDOWN_MINUTES > 0 else ""
        )
        + f"• {MAX_MESSAGES_PER_MINUTE} сообщ/мин\n"
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
    await close_ticket_in_db(tid, closed_by="user", closed_by_user_id=c.from_user.id)
    # 1) обновляем сообщение, на котором нажали кнопку
    await c.message.edit_text(
        f"{PROJECT_NAME}\n🔴 <b>Тикет #{tid} закрыт вами.</b>",
        reply_markup=user_kb(False), parse_mode="HTML",
    )
    # 2) обновляем нижнюю reply-клавиатуру (убираем «Закрыть тикет»)
    await bot.send_message(
        c.from_user.id, "Меню обновлено.",
        reply_markup=user_reply_kb(has_open_ticket=False),
    )
    # 3) уведомляем тему в группе
    try:
        await bot.send_message(
            GROUP_ID,
            f"🔴 Тикет #{tid} закрыт <b>пользователем</b> {mention(c.from_user)}",
            message_thread_id=topic_id, parse_mode="HTML",
        )
        await bot.edit_forum_topic(
            GROUP_ID, topic_id,
            name=topic_name_closed(
                tid, c.from_user.username, c.from_user.id, closed_by="user"
            ),
        )
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
            "SELECT user_id, topic_id, username, status FROM tickets WHERE id=?",
            (tid,),
        )
        row = await cur.fetchone()
    if not row:
        return await c.answer("Не найден", show_alert=True)
    uid, topic_id, uname, status = row
    if status != "open":
        return await c.answer("⚠️ Уже закрыт", show_alert=True)
    await close_ticket_in_db(tid, closed_by="admin", closed_by_user_id=c.from_user.id)

    admin_label = (
        f"@{c.from_user.username}" if c.from_user.username else f"ID {c.from_user.id}"
    )

    # 1) уведомляем пользователя в ЛС — явно «администратор»
    try:
        await bot.send_message(
            uid,
            f"{PROJECT_NAME}\n🔴 <b>Тикет #{tid} закрыт администратором.</b>",
            parse_mode="HTML",
            reply_markup=user_reply_kb(has_open_ticket=False),
        )
    except Exception:
        pass

    # 2) переименовываем и закрываем тему, оставляя видимый след в теме
    try:
        await bot.send_message(
            GROUP_ID,
            f"🔴 Тикет #{tid} закрыт <b>администратором</b> {admin_label}",
            message_thread_id=topic_id, parse_mode="HTML",
        )
        await bot.edit_forum_topic(
            GROUP_ID, topic_id,
            name=topic_name_closed(tid, uname, uid, closed_by="admin"),
        )
        await bot.close_forum_topic(GROUP_ID, topic_id)
        await c.message.edit_text(
            c.message.text + f"\n🔴 <b>Закрыт админом {admin_label}</b>",
            parse_mode="HTML", reply_markup=None,
        )
    except Exception as e:
        logger.error(f"Close err: {e}")
    await c.answer("Закрыт")


# ==========================================
# 2.5. ОБРАБОТЧИКИ ТЕКСТОВЫХ КНОПОК НИЖНЕГО МЕНЮ (reply keyboard)
# Регистрируем ДО catch-all, иначе нажатие кнопки уйдёт в открытый тикет.
# ==========================================
@dp.message(F.chat.type == "private", F.text == BTN_MENU)
async def btn_user_menu(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        await _send_admin_menu(m)
        return
    if await is_banned(m.from_user.id):
        return await m.answer("🚫 Вы заблокированы.", reply_markup=ReplyKeyboardRemove())
    await _send_user_menu(m)


@dp.message(F.chat.type == "private", F.text == BTN_HELP)
async def btn_help(m: Message):
    cooldown_line = (
        f"• кулдаун между тикетами: {TICKET_CREATE_COOLDOWN_MINUTES} мин\n"
        if TICKET_CREATE_COOLDOWN_MINUTES > 0 else ""
    )
    txt = (
        f"{PROJECT_NAME}\nℹ️ <b>Как это работает:</b>\n"
        f"1. Нажмите «🆕 Новый тикет»\n"
        f"2. Опишите проблему (текст, фото, файл)\n"
        f"3. Поддержка ответит вам прямо здесь.\n\n"
        f"⚙️ <b>Лимиты:</b>\n"
        f"• {MAX_OPEN_TICKETS} открытый тикет одновременно\n"
        f"• {MAX_TICKETS_PER_DAY} тикетов в сутки\n"
        f"{cooldown_line}"
        f"• {MAX_MESSAGES_PER_MINUTE} сообщ/мин\n"
        f"• Файлы до {MAX_FILE_SIZE // (1024*1024)} МБ"
    )
    has_ticket = await count_open_tickets(m.from_user.id) > 0
    await m.answer(
        txt, parse_mode="HTML",
        reply_markup=user_reply_kb(has_open_ticket=has_ticket),
    )


@dp.message(F.chat.type == "private", F.text == BTN_NEW)
async def btn_new_ticket(m: Message):
    if await is_banned(m.from_user.id):
        return await m.answer("🚫 Вы заблокированы.")
    ok, text, _tid = await create_ticket_for(m.from_user)
    await m.answer(
        text, parse_mode="HTML",
        reply_markup=user_reply_kb(has_open_ticket=ok),
    )


@dp.message(F.chat.type == "private", F.text == BTN_MY)
async def btn_my_tickets(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, status, created_at, closed_by FROM tickets "
            "WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (m.from_user.id,),
        )
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("📋 У вас ещё нет тикетов.")
    by_label = {
        "user": "вами",
        "admin": "администратором",
        "system:no_topic": "системой",
        "system:topic_deleted": "автоматически (тема удалена)",
        "system:topic_closed_manually": "администратором (вручную)",
    }
    txt = f"{PROJECT_NAME}\n📋 <b>Ваши тикеты:</b>\n"
    for tid, st, date, cb in rows:
        e = "🟢" if st == "open" else "🔴"
        suffix = f" — закрыт {by_label.get(cb, cb)}" if (st == "closed" and cb) else ""
        txt += f"{e} <b>#{tid}</b> ({date[:16]}){suffix}\n"
    has_ticket = await count_open_tickets(m.from_user.id) > 0
    await m.answer(
        txt, parse_mode="HTML",
        reply_markup=user_reply_kb(has_open_ticket=has_ticket),
    )


@dp.message(F.chat.type == "private", F.text == BTN_CLOSE)
async def btn_close_ticket(m: Message):
    if await is_banned(m.from_user.id):
        return
    ticket = await get_open_ticket(m.from_user.id)
    if not ticket:
        return await m.answer(
            "⚠️ Нет открытых тикетов.",
            reply_markup=user_reply_kb(has_open_ticket=False),
        )
    tid, topic_id = ticket
    await close_ticket_in_db(tid, closed_by="user", closed_by_user_id=m.from_user.id)
    await m.answer(
        f"🔴 Тикет #{tid} закрыт <b>вами</b>.",
        parse_mode="HTML",
        reply_markup=user_reply_kb(has_open_ticket=False),
    )
    try:
        await bot.send_message(
            GROUP_ID,
            f"🔴 Тикет #{tid} закрыт <b>пользователем</b> {mention(m.from_user)}",
            message_thread_id=topic_id, parse_mode="HTML",
        )
        await bot.edit_forum_topic(
            GROUP_ID, topic_id,
            name=topic_name_closed(
                tid, m.from_user.username, m.from_user.id, closed_by="user"
            ),
        )
        await bot.close_forum_topic(GROUP_ID, topic_id)
    except Exception as e:
        logger.error(f"Close topic err: {e}")


# --- Кнопки админ-меню ---
@dp.message(F.chat.type == "private", F.text == BTN_ADMIN)
async def btn_admin_menu(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return await m.answer("⛔ Нет доступа")
    await state.clear()
    await _send_admin_menu(m)


@dp.message(F.chat.type == "private", F.text == BTN_STATS)
async def btn_admin_stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async def _scalar(q, args=()):
            cur = await db.execute(q, args)
            return (await cur.fetchone())[0]
        t = await _scalar("SELECT COUNT(*) FROM tickets")
        o = await _scalar("SELECT COUNT(*) FROM tickets WHERE status='open'")
        cl = await _scalar("SELECT COUNT(*) FROM tickets WHERE status='closed'")
        b = await _scalar("SELECT COUNT(*) FROM bans")
        u = await _scalar("SELECT COUNT(DISTINCT user_id) FROM tickets")
        by_user = await _scalar(
            "SELECT COUNT(*) FROM tickets WHERE status='closed' AND closed_by='user'"
        )
        by_admin = await _scalar(
            "SELECT COUNT(*) FROM tickets WHERE status='closed' AND closed_by='admin'"
        )
        by_system = await _scalar(
            "SELECT COUNT(*) FROM tickets WHERE status='closed' "
            "AND closed_by LIKE 'system%'"
        )
        by_unknown = await _scalar(
            "SELECT COUNT(*) FROM tickets WHERE status='closed' AND closed_by IS NULL"
        )
    txt = (
        f"{PROJECT_NAME}\n📊 <b>Статистика</b>\n"
        f"👥 Юзеров: <b>{u}</b>\n🎫 Тикетов: <b>{t}</b>\n"
        f"🟢 Открытых: <b>{o}</b>\n🔴 Закрытых: <b>{cl}</b>\n"
        f"   • юзером: <b>{by_user}</b>\n"
        f"   • админом: <b>{by_admin}</b>\n"
        f"   • системой: <b>{by_system}</b>"
        + (f"\n   • без метки: <b>{by_unknown}</b>" if by_unknown else "")
        + f"\n🚫 Банов: <b>{b}</b>"
    )
    await m.answer(txt, parse_mode="HTML", reply_markup=admin_reply_kb())


@dp.message(F.chat.type == "private", F.text == BTN_TICKETS)
async def btn_admin_tickets(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, username, created_at FROM tickets "
            "WHERE status='open' ORDER BY id DESC LIMIT 15"
        )
        rows = await cur.fetchall()
    if not rows:
        return await m.answer(
            f"{PROJECT_NAME}\n📋 <b>Открытых тикетов нет</b>",
            parse_mode="HTML", reply_markup=admin_reply_kb(),
        )
    txt = f"{PROJECT_NAME}\n📋 <b>Активные тикеты:</b>\n"
    for tid, uid, uname, date in rows:
        txt += f"🟢 <b>#{tid}</b> — {uname or uid} ({date[:16]})\n"
    await m.answer(txt, parse_mode="HTML", reply_markup=admin_reply_kb())


@dp.message(F.chat.type == "private", F.text == BTN_BANS)
async def btn_admin_bans(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username, banned_at FROM bans "
            "ORDER BY banned_at DESC LIMIT 15"
        )
        rows = await cur.fetchall()
    if not rows:
        return await m.answer(
            f"{PROJECT_NAME}\n📜 <b>Банов нет</b>",
            parse_mode="HTML", reply_markup=admin_reply_kb(),
        )
    txt = f"{PROJECT_NAME}\n📜 <b>Список банов:</b>\n"
    for uid, uname, date in rows:
        txt += f"🚫 <code>{uid}</code> ({uname or '-'}) — {date[:16]}\n"
    await m.answer(txt, parse_mode="HTML", reply_markup=admin_reply_kb())


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
# RECONCILE при старте
# ==========================================
async def reconcile_tickets():
    """
    Сверка состояния после рестарта бота:
      • тема в Telegram удалена  -> помечаем тикет closed в БД
      • тема в Telegram закрыта  -> помечаем тикет closed в БД
      • тикет открыт, но topic_id отсутствует -> помечаем closed
      • существующие открытые темы переименовываются в актуальный формат
        "🎫 #id | @username" (на случай если ранее были без подписи)
    Юзеру при «потерянном» тикете отправляем уведомление, если возможно.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, username, topic_id FROM tickets WHERE status='open'"
        )
        open_rows = await cur.fetchall()

    if not open_rows:
        logger.info("Reconcile: no open tickets")
        return

    logger.info(f"Reconcile: checking {len(open_rows)} open tickets...")
    fixed_deleted = 0
    fixed_closed = 0
    renamed = 0

    for tid, uid, uname, topic_id in open_rows:
        # 1) тикет без topic_id (создавался, но тема не создалась) — закрываем в БД
        if not topic_id:
            await close_ticket_in_db(
                tid, closed_by="system:no_topic", closed_by_user_id=0
            )
            fixed_deleted += 1
            logger.warning(f"Reconcile: ticket #{tid} had no topic_id -> closed")
            continue

        # 2) безопасный «пинг» темы: переименовываем в актуальное имя.
        #    Если темы нет -> Bad Request: message thread not found / TOPIC_DELETED
        #    Если тема закрыта -> Bad Request: TOPIC_CLOSED
        target_name = topic_name_open(tid, uname, uid)
        try:
            await bot.edit_forum_topic(GROUP_ID, topic_id, name=target_name)
            renamed += 1
        except Exception as e:
            err = str(e).lower()
            if (
                "topic_deleted" in err
                or "message thread not found" in err
                or "thread not found" in err
                or "topic not found" in err
            ):
                await close_ticket_in_db(
                    tid, closed_by="system:topic_deleted", closed_by_user_id=0
                )
                fixed_deleted += 1
                logger.warning(
                    f"Reconcile: topic for ticket #{tid} deleted -> closed in DB"
                )
                try:
                    await bot.send_message(
                        uid,
                        f"{PROJECT_NAME}\n⚠️ Тикет #{tid} был удалён администратором. "
                        f"Если вопрос ещё актуален — создайте новый тикет.",
                    )
                except Exception:
                    pass
            elif "topic_closed" in err:
                await close_ticket_in_db(
                    tid,
                    closed_by="system:topic_closed_manually",
                    closed_by_user_id=0,
                )
                fixed_closed += 1
                logger.info(
                    f"Reconcile: topic for ticket #{tid} was closed manually -> "
                    f"closed in DB"
                )
                # попытаться поставить «закрытое» имя с маркером ручного закрытия
                try:
                    await bot.edit_forum_topic(
                        GROUP_ID, topic_id,
                        name=topic_name_closed(
                            tid, uname, uid,
                            closed_by="system:topic_closed_manually",
                        ),
                    )
                except Exception:
                    pass
                try:
                    await bot.send_message(
                        uid,
                        f"{PROJECT_NAME}\n🔴 Тикет #{tid} был закрыт администратором.",
                    )
                except Exception:
                    pass
            else:
                logger.error(f"Reconcile: unexpected error for ticket #{tid}: {e}")

    logger.info(
        f"Reconcile done: renamed={renamed}, closed_as_deleted={fixed_deleted}, "
        f"closed_as_manually_closed={fixed_closed}"
    )


# ==========================================
# MAIN
# ==========================================
async def setup_bot_commands():
    """
    Регистрирует команды в синем меню '/' возле скрепки.
    Юзеры видят базовый набор, админы — расширенный.
    """
    user_cmds = [
        BotCommand(command="start", description="🎫 Открыть меню"),
        BotCommand(command="menu", description="📋 Меню поддержки"),
        BotCommand(command="close", description="🔴 Закрыть текущий тикет"),
        BotCommand(command="cancel", description="❌ Отменить действие"),
    ]
    admin_cmds = user_cmds + [
        BotCommand(command="admin", description="🔧 Панель администратора"),
    ]
    try:
        await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        for admin_id in ADMIN_IDS:
            try:
                await bot.set_my_commands(
                    admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id)
                )
            except Exception as e:
                logger.warning(f"set_my_commands for admin {admin_id} failed: {e}")
        logger.info("Bot commands registered")
    except Exception as e:
        logger.error(f"set_my_commands failed: {e}")


async def main():
    await init_db()
    logger.info(f"Bot started | Project: {PROJECT_NAME}")
    logger.info(f"Admin IDs: {ADMIN_IDS} | Group: {GROUP_ID}")
    try:
        await setup_bot_commands()
    except Exception as e:
        logger.error(f"setup_bot_commands failed: {e}")
    try:
        await reconcile_tickets()
    except Exception as e:
        logger.error(f"Reconcile failed (continuing anyway): {e}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
