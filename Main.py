import asyncio
import csv
import io
import logging
import logging.handlers
import sqlite3
import time
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, MessageEntity, BufferedInputFile
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("giftbot")
logger.setLevel(logging.INFO)
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
_file = logging.handlers.RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.addHandler(_console)
logger.addHandler(_file)

TOKEN = "8844019371:AAHhtamYw6JRwhLmKpcTcyFha5KajTsYHjE"
OWNER_ID = 906824039
GIFT_PRICE_STARS = 60
DB_PATH = "bot.db"
FRIEND_LINK_TTL_HOURS = 24 * 365 * 10  # ссылка практически бессрочная
USERNAME_REFRESH_INTERVAL_SEC = 120   # как часто запускать обновление username (раз в 2 мин)
USERNAME_REFRESH_DELAY_SEC = 0.1      # пауза между запросами к Telegram внутри обновления, чтобы не поймать лимиты и не грузить бота
BOT_USERNAME = "GiftsBearBot"  # перезаписывается реальным значением в main() через bot.get_me()
CHANNEL_USERNAME = "vocmaq"
CHANNEL_URL = "https://t.me/vocmaq"

# ─── REMOTE GIFTS (emoji_id: gift_id) ───────────────────────────────────────
GIFTS = {
    "5379850840691476775": {"gift_id": "5956217000635139069", "name": "Мишка"},
    "5345935030143196497": {"gift_id": "5922558454332916696", "name": "Ёлка"},
    "5226661632259691727": {"gift_id": "5800655655995968830", "name": "Мишка"},
    "5224628072619216265": {"gift_id": "5801108895304779062", "name": "Сердце"},
    "5289761157173775507": {"gift_id": "5866352046986232958", "name": "Мишка"},
    "5317000922096769303": {"gift_id": "5893356958802511476", "name": "Мишка"},
    "5359736160224586485": {"gift_id": "5935895822435615975", "name": "Мишка"},
    "5393309541620291208": {"gift_id": "5969796561943660080", "name": "Мишка"},
    "5397971251878732060": {"gift_id": "5974210632977745012", "name": "Мишка"},
}

EMO = {
    "bear":       ("🧸", "6041921818896372382"),
    "quote1":     ("🎁", "6028435952299413210"),
    "send_gift":  ("🎁", "6039573425268201570"),
    "how_works":  ("❓", "6030848053177486888"),
    "support":    ("🗣", "6030329749409108167"),
    "profile":    ("👤", "6035084557378654059"),
    "choose_gift":("⭐", "5470092785094765546"),

    "thumbsup":   ("👍", "6041720006973067267"),
    "thumbsdown": ("👎", "6041716699848249286"),
    "point":      ("👆", "5886676966102274844"),
    "stop":       ("🚫", "5938215362473496448"),
    "wave":       ("👋", "5985478698722136468"),
    "check":      ("✅", "6041919344995209164"),
    "star":       ("⭐️", "5886685105065300941"),
    "sparkles":   ("✨", "5778226250149532337"),
    "gift_box":   ("🎁", "5805298713211447980"),
    "back_arrow": ("↩️", "5778432163766604235"),
    "back_cancel":("↩️", "5938537205847822613"),
    "reload":     ("🔄", "5769248574499983619"),
    "edit":       ("✏️", "5771847914477326786"),
    "note":       ("📝", "5778299625370817409"),
    "money":      ("💰", "5778421276024509124"),
    "coin":       ("🪙", "5778613750688911681"),
    "diamond":    ("💎", "5776023601941582822"),
    "crown":      ("👑", "5805553606635559688"),
    "user":       ("👤", "5884366771913233289"),
    "link":       ("🌐", "5776233299424843260"),
    "chart":      ("📈", "5938539885907415367"),
    "stats":      ("📊", "5936143551854285132"),
    "folder":     ("📁", "5805550320985578625"),
    "puzzle":     ("🧩", "5837069325034331827"),
    "new":        ("🆕", "5895669571058142797"),
    "clock":      ("⏲", "6030537810509828330"),
    "megaphone":  ("📢", "6044117517847236354"),
    "warn":       ("🏳️", "6041923781696426657"),
    "export":     ("📁", "5805550320985578625"),
}

def E(key):
    return EMO[key]


class GiftStates(StatesGroup):
    choosing_gift = State()
    choosing_recipient = State()
    choosing_comment = State()
    entering_comment = State()
    waiting_payment = State()
    admin_give_stars_user = State()
    admin_give_stars_amount = State()
    admin_broadcast = State()
    admin_broadcast_confirm = State()
    admin_set_price = State()


bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ─── DB LAYER (sqlite3, вызовы через asyncio.to_thread) ─────────────────────

def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _init_db_sync():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            balance INTEGER NOT NULL DEFAULT 0,
            gifts_received INTEGER NOT NULL DEFAULT 0,
            gifts_sent INTEGER NOT NULL DEFAULT 0,
            friends_invited INTEGER NOT NULL DEFAULT 0,
            start_count INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS pending_friends (
            inviter_id INTEGER PRIMARY KEY,
            gift_emoji_id TEXT NOT NULL,
            friend_id INTEGER,
            friend_label TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            emoji_id TEXT NOT NULL,
            gift_id TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            telegram_charge_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    # мягкая миграция для баз, созданных до появления новых колонок
    for col, coldef in (
        ("friends_invited", "INTEGER NOT NULL DEFAULT 0"),
        ("start_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_seen", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

async def init_db():
    await asyncio.to_thread(_init_db_sync)

async def db_exec(query, params=()):
    def _run():
        conn = _connect()
        cur = conn.execute(query, params)
        conn.commit()
        last_id = cur.lastrowid
        conn.close()
        return last_id
    return await asyncio.to_thread(_run)

async def db_fetchone(query, params=()):
    def _run():
        conn = _connect()
        row = conn.execute(query, params).fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_run)

async def db_fetchall(query, params=()):
    def _run():
        conn = _connect()
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_run)


async def ensure_user(user_id, username=None, full_name=None):
    await db_exec(
        "INSERT INTO users (user_id, username, full_name) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
        (user_id, username, full_name),
    )

async def get_user_row(user_id):
    row = await db_fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))
    if not row:
        await ensure_user(user_id)
        row = await db_fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))
    return row

async def add_stars(user_id, amount):
    await ensure_user(user_id)
    await db_exec("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))

async def inc_gifts_sent(user_id):
    await db_exec("UPDATE users SET gifts_sent = gifts_sent + 1 WHERE user_id=?", (user_id,))

async def inc_gifts_received(user_id):
    await db_exec("UPDATE users SET gifts_received = gifts_received + 1 WHERE user_id=?", (user_id,))

async def set_user_active(user_id, active: bool):
    await db_exec("UPDATE users SET is_active=? WHERE user_id=?", (1 if active else 0, user_id))

async def update_username(user_id, username):
    await db_exec("UPDATE users SET username=? WHERE user_id=?", (username, user_id))

async def count_users():
    row = await db_fetchone("SELECT COUNT(*) AS c FROM users")
    return row["c"] if row else 0

async def sum_gifts_sent():
    row = await db_fetchone("SELECT COALESCE(SUM(gifts_sent),0) AS s FROM users")
    return row["s"] if row else 0

async def count_new_users_today():
    row = await db_fetchone("SELECT COUNT(*) AS c FROM users WHERE date(created_at) = date('now')")
    return row["c"] if row else 0

async def count_gifts_completed_today():
    row = await db_fetchone(
        "SELECT COUNT(*) AS c FROM orders WHERE status='completed' AND date(completed_at) = date('now')"
    )
    return row["c"] if row else 0

async def touch_start(user_id):
    await db_exec(
        "UPDATE users SET start_count = start_count + 1, last_seen = datetime('now') WHERE user_id=?",
        (user_id,),
    )

async def inc_friends_invited(user_id):
    await db_exec("UPDATE users SET friends_invited = friends_invited + 1 WHERE user_id=?", (user_id,))

async def get_top_buyers(limit=10):
    return await db_fetchall(
        "SELECT user_id, username, gifts_sent FROM users WHERE gifts_sent > 0 "
        "ORDER BY gifts_sent DESC, user_id ASC LIMIT ?",
        (limit,),
    )

async def list_users_page(offset, limit):
    return await db_fetchall(
        "SELECT * FROM users ORDER BY user_id LIMIT ? OFFSET ?", (limit, offset)
    )

async def active_user_ids():
    rows = await db_fetchall("SELECT user_id FROM users WHERE is_active=1")
    return [r["user_id"] for r in rows]

async def get_display_name(user_id):
    row = await db_fetchone("SELECT username FROM users WHERE user_id=?", (user_id,))
    if row and row["username"]:
        return f"@{row['username']}"
    return f"ID {user_id}"

async def all_users_for_export():
    return await db_fetchall("SELECT * FROM users ORDER BY user_id")

async def all_orders_for_export():
    return await db_fetchall("SELECT * FROM orders ORDER BY order_id")


# ─── SETTINGS (key/value, для изменяемых в рантайме параметров) ─────────────

async def get_setting(key, default=None):
    row = await db_fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default

async def set_setting(key, value):
    await db_exec(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ─── PENDING FRIENDS ─────────────────────────────────────────────────────────

async def create_pending_friend(inviter_id, gift_emoji_id):
    expires_at = (datetime.utcnow() + timedelta(hours=FRIEND_LINK_TTL_HOURS)).isoformat()
    await db_exec(
        "INSERT INTO pending_friends (inviter_id, gift_emoji_id, friend_id, friend_label, expires_at) "
        "VALUES (?, ?, NULL, NULL, ?) "
        "ON CONFLICT(inviter_id) DO UPDATE SET gift_emoji_id=excluded.gift_emoji_id, "
        "friend_id=NULL, friend_label=NULL, expires_at=excluded.expires_at, created_at=datetime('now')",
        (inviter_id, gift_emoji_id, expires_at),
    )

async def get_pending_friend(inviter_id):
    row = await db_fetchone("SELECT * FROM pending_friends WHERE inviter_id=?", (inviter_id,))
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
        await delete_pending_friend(inviter_id)
        return None
    return row

async def set_friend_joined(inviter_id, friend_id, friend_label):
    await db_exec(
        "UPDATE pending_friends SET friend_id=?, friend_label=? WHERE inviter_id=?",
        (friend_id, friend_label, inviter_id),
    )

async def delete_pending_friend(inviter_id):
    await db_exec("DELETE FROM pending_friends WHERE inviter_id=?", (inviter_id,))


# ─── ORDERS / PAYMENTS ───────────────────────────────────────────────────────

async def create_order(sender_id, recipient_id, emoji_id, gift_id, comment, amount, currency):
    return await db_exec(
        "INSERT INTO orders (sender_id, recipient_id, emoji_id, gift_id, comment, amount, currency) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sender_id, recipient_id, emoji_id, gift_id, comment, amount, currency),
    )

async def get_order(order_id):
    return await db_fetchone("SELECT * FROM orders WHERE order_id=?", (order_id,))

async def set_order_status(order_id, status, telegram_charge_id=None, completed=False):
    if completed:
        await db_exec(
            "UPDATE orders SET status=?, telegram_charge_id=COALESCE(?, telegram_charge_id), "
            "completed_at=datetime('now') WHERE order_id=?",
            (status, telegram_charge_id, order_id),
        )
    else:
        await db_exec(
            "UPDATE orders SET status=?, telegram_charge_id=COALESCE(?, telegram_charge_id) WHERE order_id=?",
            (status, telegram_charge_id, order_id),
        )


# ─── MESSAGE BUILDER: корректные UTF-16 offsets ─────────────────────────────
class MsgBuilder:
    def __init__(self):
        self.text = ""
        self.entities = []

    def _off(self):
        return len(self.text.encode("utf-16-le")) // 2

    def add(self, s: str):
        self.text += s
        return self

    def nl(self, n=1):
        self.text += "\n" * n
        return self

    def bold(self, s: str):
        o = self._off()
        self.text += s
        self.entities.append(MessageEntity(type="bold", offset=o, length=self._off() - o))
        return self

    def italic(self, s: str):
        o = self._off()
        self.text += s
        self.entities.append(MessageEntity(type="italic", offset=o, length=self._off() - o))
        return self

    def quote(self, s: str):
        o = self._off()
        self.text += s
        self.entities.append(MessageEntity(type="blockquote", offset=o, length=self._off() - o))
        return self

    def code(self, s: str):
        o = self._off()
        self.text += s
        self.entities.append(MessageEntity(type="code", offset=o, length=self._off() - o))
        return self

    def emoji(self, key: str):
        placeholder, emoji_id = E(key)
        o = self._off()
        self.text += placeholder
        self.entities.append(MessageEntity(
            type="custom_emoji", offset=o, length=self._off() - o, custom_emoji_id=emoji_id
        ))
        return self

    def emoji_raw(self, emoji_id: str, placeholder="⭐"):
        o = self._off()
        self.text += placeholder
        self.entities.append(MessageEntity(
            type="custom_emoji", offset=o, length=self._off() - o, custom_emoji_id=emoji_id
        ))
        return self

    def quote_emoji(self, key: str, text: str):
        placeholder, emoji_id = E(key)
        start = self._off()
        eo = self._off()
        self.text += placeholder
        self.entities.append(MessageEntity(
            type="custom_emoji", offset=eo, length=self._off() - eo, custom_emoji_id=emoji_id
        ))
        self.text += " " + text
        end = self._off()
        self.entities.append(MessageEntity(type="blockquote", offset=start, length=end - start))
        return self

    def build(self):
        return self.text, self.entities


def strip_icons(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            data = b.model_dump(exclude_none=True)
            data.pop("icon_custom_emoji_id", None)
            new_row.append(InlineKeyboardButton(**data))
        new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


# ─── premium-icon fallback, кэш по чату (не глобально) ──────────────────────
_PREMIUM_ICONS_CHATS = {}

def icons_enabled(chat_id):
    return _PREMIUM_ICONS_CHATS.get(chat_id, True)

def disable_icons(chat_id):
    _PREMIUM_ICONS_CHATS[chat_id] = False


async def send_msg(chat_id, builder: MsgBuilder, reply_markup=None):
    text, entities = builder.build()
    try:
        return await bot.send_message(chat_id, text=text, entities=entities, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if reply_markup is not None and icons_enabled(chat_id) and "icon" in str(e).lower():
            logger.warning(f"icon_custom_emoji_id отклонён Telegram для chat_id={chat_id}: {e}")
            disable_icons(chat_id)
            return await bot.send_message(chat_id, text=text, entities=entities, reply_markup=strip_icons(reply_markup))
        raise

async def edit_msg(message: Message, builder: MsgBuilder, reply_markup=None):
    """Удаляет старое сообщение и отправляет новое (вместо редактирования на месте)."""
    text, entities = builder.build()
    try:
        await message.delete()
    except TelegramBadRequest as e:
        logger.warning(f"Не удалось удалить сообщение chat_id={message.chat.id}: {e}")

    try:
        return await bot.send_message(message.chat.id, text=text, entities=entities, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if reply_markup is not None and icons_enabled(message.chat.id) and "icon" in str(e).lower():
            logger.warning(f"icon_custom_emoji_id отклонён Telegram для chat_id={message.chat.id}: {e}")
            disable_icons(message.chat.id)
            return await bot.send_message(message.chat.id, text=text, entities=entities, reply_markup=strip_icons(reply_markup))
        raise


async def notify_owner(text: str):
    try:
        await bot.send_message(OWNER_ID, f"⚠️ {text}")
    except Exception as e:
        logger.error(f"Не удалось уведомить владельца: {e}")


_SUB_CACHE = {}          # user_id -> (is_subscribed: bool, checked_at: float)
SUB_CACHE_TTL_SEC = 60   # не проверяем подписку у Telegram чаще, чем раз в минуту на юзера

async def is_subscribed(user_id: int, force: bool = False) -> bool:
    """Проверка подписки на обязательный канал, с коротким кэшем (SUB_CACHE_TTL_SEC),
    чтобы не дёргать Telegram API на каждое сообщение/нажатие кнопки и не тормозить бота.
    При недоступности API не блокируем пользователя. force=True — игнорировать кэш
    (используется, когда юзер явно нажал «Я подписался»)."""
    now = time.monotonic()
    if not force:
        cached = _SUB_CACHE.get(user_id)
        if cached and now - cached[1] < SUB_CACHE_TTL_SEC:
            return cached[0]
    try:
        member = await bot.get_chat_member(chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id)
        result = member.status not in ("left", "kicked")
    except Exception as e:
        logger.warning(f"Не удалось проверить подписку для {user_id}: {e}")
        result = True
    _SUB_CACHE[user_id] = (result, now)
    return result


# ─── KEYBOARDS ───────────────────────────────────────────────────────────────

def btn(text, icon_key=None, **kwargs):
    icon_id = E(icon_key)[1] if icon_key else None
    return InlineKeyboardButton(text=text, icon_custom_emoji_id=icon_id, **kwargs)

def subscribe_kb():
    kb = InlineKeyboardBuilder()
    kb.row(btn("Подписаться", "megaphone", url=CHANNEL_URL))
    kb.row(btn("Я подписался", "check", callback_data="check_sub"))
    return kb.as_markup()


def subscribe_required_msg() -> MsgBuilder:
    return (
        MsgBuilder()
        .emoji("warn").add(" ").bold("Нужна подписка на канал").nl(2)
        .add(f"Чтобы пользоваться ботом, подпишись на @{CHANNEL_USERNAME} и нажми «Я подписался».")
    )


# ─── ПРОВЕРКА ПОДПИСКИ НА ЛЮБОЕ ВЗАИМОДЕЙСТВИЕ ───────────────────────────────
# Если пользователь отписался от канала, при следующем сообщении/нажатии любой
# кнопки бот прервёт обработку и снова попросит подписаться. /start и кнопка
# "Я подписался" пропускаются - они проверяют подписку сами (со свежим force=True).
class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if user.id == OWNER_ID:
            return await handler(event, data)

        if isinstance(event, Message):
            if event.text and event.text.startswith("/start"):
                return await handler(event, data)
            chat_id = event.chat.id
        elif isinstance(event, CallbackQuery):
            if event.data == "check_sub":
                return await handler(event, data)
            chat_id = event.message.chat.id if event.message else user.id
        else:
            return await handler(event, data)

        if not await is_subscribed(user.id):
            await send_msg(chat_id, subscribe_required_msg(), reply_markup=subscribe_kb())
            if isinstance(event, CallbackQuery):
                await event.answer()
            return

        return await handler(event, data)


dp.message.outer_middleware(SubscriptionMiddleware())
dp.callback_query.outer_middleware(SubscriptionMiddleware())

def welcome_text() -> MsgBuilder:
    return (
        MsgBuilder()
        .bold("Добро пожаловать!").nl(2)
        .quote_emoji("quote1", "Здесь ты можешь купить удалённый подарок в телеграмм.").nl(2)
        .add("Выбери раздел ниже ⌵")
    )

def main_menu_kb(is_owner: bool = False):
    kb = InlineKeyboardBuilder()
    kb.row(btn("Отправить подарок", "send_gift", callback_data="send_gift"))
    kb.row(
        btn("Как работает бот", "how_works", callback_data="how_it_works"),
        btn("Поддержка", "support", url="https://t.me/vocma"),
    )
    kb.row(btn("Топ покупателей", "crown", callback_data="top_buyers"))
    if is_owner:
        kb.row(btn("Админ-панель", "crown", callback_data="admin_open"))
    return kb.as_markup()

def gifts_kb():
    kb = InlineKeyboardBuilder()
    for emoji_id, info in GIFTS.items():
        kb.row(InlineKeyboardButton(text=info["name"], callback_data=f"gift_{emoji_id}", icon_custom_emoji_id=emoji_id))
    kb.row(btn("Назад", "back_cancel", callback_data="back_main"))
    return kb.as_markup()

def recipient_kb():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="Себе", callback_data="recipient_self"),
        InlineKeyboardButton(text="Другу", callback_data="recipient_friend"),
    )
    kb.row(btn("Назад", "back_cancel", callback_data="send_gift"))
    return kb.as_markup()

def comment_kb():
    kb = InlineKeyboardBuilder()
    kb.row(btn("Добавить комментарий", "edit", callback_data="comment_yes"))
    kb.row(btn("Пропустить", "stop", callback_data="comment_no"))
    kb.row(btn("Назад", "back_cancel", callback_data="send_gift"))
    return kb.as_markup()

def friend_ready_kb(gift_emoji_id):
    kb = InlineKeyboardBuilder()
    kb.row(btn("Продолжить", "check", callback_data=f"friend_ready_{gift_emoji_id}"))
    kb.row(btn("Отменить приглашение", "stop", callback_data="friend_cancel"))
    kb.row(btn("Назад", "back_cancel", callback_data="send_gift"))
    return kb.as_markup()

def admin_kb():
    kb = InlineKeyboardBuilder()
    kb.row(btn("Выдать звёзды", "star", callback_data="admin_give_stars"))
    kb.row(btn("Изменить цену подарка", "money", callback_data="admin_set_price"))
    kb.row(btn("Статистика", "stats", callback_data="admin_stats"))
    kb.row(btn("Рассылка", "megaphone", callback_data="admin_broadcast"))
    kb.row(btn("Подарки (ID)", "gift_box", callback_data="admin_gifts_0"))
    kb.row(btn("Экспорт users.csv", "export", callback_data="admin_export_users"))
    kb.row(btn("Экспорт payments.csv", "export", callback_data="admin_export_orders"))
    kb.row(btn("Главное меню", "back_arrow", callback_data="back_main"))
    return kb.as_markup()


# ─── HELPERS ─────────────────────────────────────────────────────────────────

async def safe_send_gift(recipient_id: int, gift_id: str, text: str = "", max_retries=3):
    """Отправка подарка с обработкой FloodWait и заблокированных/деактивированных юзеров."""
    for attempt in range(max_retries + 1):
        try:
            await bot.send_gift(user_id=recipient_id, gift_id=gift_id, text=text or None)
            return True
        except TelegramRetryAfter as e:
            logger.warning(f"FloodWait при send_gift пользователю {recipient_id}: retry_after={e.retry_after}")
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            logger.warning(f"Пользователь {recipient_id} заблокировал бота или недоступен — помечаю неактивным.")
            await set_user_active(recipient_id, False)
            return False
        except Exception as e:
            logger.error(f"sendGift failed для {recipient_id}: {e}")
            return False
    logger.error(f"sendGift: превышено число повторов для {recipient_id}")
    return False

async def safe_send_message(user_id: int, text: str, max_retries=3):
    """Обёртка для рассылки: FloodWait -> пауза+повтор, Forbidden -> деактивация без спама."""
    for attempt in range(max_retries + 1):
        try:
            await bot.send_message(user_id, text)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            await set_user_active(user_id, False)
            return False
        except Exception as e:
            logger.error(f"send_message failed для {user_id}: {e}")
            return False
    return False

async def refund_order(order_id, sender_id, charge_id, reason=""):
    try:
        await bot.refund_star_payment(user_id=sender_id, telegram_payment_charge_id=charge_id)
        await set_order_status(order_id, "refunded", completed=True)
        logger.info(f"Рефанд выполнен: order={order_id} user={sender_id} reason={reason}")
        await notify_owner(f"Рефанд выполнен по заказу {order_id} пользователю {sender_id}. Причина: {reason}")
    except Exception as e:
        logger.error(f"Рефанд НЕ удался: order={order_id} user={sender_id} err={e}")
        await notify_owner(f"КРИТИЧНО: рефанд не удался по заказу {order_id} пользователю {sender_id}: {e}")


# ─── /START ──────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    await ensure_user(user_id, message.from_user.username, message.from_user.full_name)
    await set_user_active(user_id, True)
    await touch_start(user_id)

    args = message.text.split()
    if len(args) > 1 and args[1].startswith("friend_"):
        try:
            inviter_id = int(args[1].split("_")[1])
        except (ValueError, IndexError):
            inviter_id = None

        if inviter_id is not None and inviter_id != user_id:
            entry = await get_pending_friend(inviter_id)
            if entry:
                friend = message.from_user
                friend_label = f"@{friend.username}" if friend.username else f"ID {friend.id}"
                await set_friend_joined(inviter_id, user_id, friend_label)
                await inc_friends_invited(inviter_id)
                logger.info(f"friend join: inviter={inviter_id} friend={user_id} label={friend_label}")

                b = MsgBuilder().emoji("wave").add(" Привет! Тебя пригласил друг, чтобы отправить тебе подарок.")
                await send_msg(message.chat.id, b)
                try:
                    nb = MsgBuilder().emoji("wave").add(f" У вас новый друг: {friend_label}")
                    await send_msg(inviter_id, nb, reply_markup=friend_ready_kb(entry["gift_emoji_id"]))
                except Exception:
                    pass
                return
            else:
                b = MsgBuilder().emoji("stop").add(" Ссылка недействительна или уже была использована.")
                await send_msg(message.chat.id, b)

    b1 = MsgBuilder().emoji("bear")
    await send_msg(message.chat.id, b1)

    is_owner = user_id == OWNER_ID
    if not await is_subscribed(user_id, force=True):
        await send_msg(message.chat.id, subscribe_required_msg(), reply_markup=subscribe_kb())
        return

    await send_msg(message.chat.id, welcome_text(), reply_markup=main_menu_kb(is_owner=is_owner))


# ─── MAIN MENU ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery):
    user_id = call.from_user.id
    if not await is_subscribed(user_id, force=True):
        await call.answer("Пока не вижу подписки. Подпишись и попробуй снова.", show_alert=True)
        return
    is_owner = user_id == OWNER_ID
    await edit_msg(call.message, welcome_text(), reply_markup=main_menu_kb(is_owner=is_owner))
    await call.answer()

@dp.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    is_owner = call.from_user.id == OWNER_ID
    await edit_msg(call.message, welcome_text(), reply_markup=main_menu_kb(is_owner=is_owner))
    await call.answer()

@dp.callback_query(F.data == "top_buyers")
async def top_buyers(call: CallbackQuery):
    rows = await get_top_buyers(10)
    kb = InlineKeyboardBuilder()
    kb.row(btn("Назад", "back_cancel", callback_data="back_main"))

    b = MsgBuilder().emoji("crown").add(" ").bold("Топ покупателей").nl(3)

    if not rows:
        b.quote_emoji("sparkles", "Пока никто не покупал подарки. Стань первым в топе! 🎉")
    else:
        lines = []
        for i, row in enumerate(rows):
            name = f"@{row['username']}" if row["username"] else f"ID {row['user_id']}"
            lines.append(f"{i + 1} место: {name}, куплено: {row['gifts_sent']} подарков")
        b.quote("\n".join(lines))

    await edit_msg(call.message, b, reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data == "how_it_works")
async def how_it_works(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(btn("Отправить подарок", "send_gift", callback_data="send_gift"))
    kb.row(btn("Назад", "back_cancel", callback_data="back_main"))

    b = (
        MsgBuilder()
        .emoji("how_works").add(" ").bold("Как работает бот?").nl(2)
        .add(f"Вы выбираете подарок и получателя (себе или другу), вводите то, что просит бот, "
             f"оплачиваете {GIFT_PRICE_STARS} звёзд, и бот сразу отправляет подарок получателю.").nl(2)
        .quote_emoji("sparkles", "Наши подарки редкие и коллекционные, уже недоступны в магазине Telegram.")
    )
    await edit_msg(call.message, b, reply_markup=kb.as_markup())
    await call.answer()


# ─── SEND GIFT FLOW ───────────────────────────────────────────────────────────

@dp.callback_query(F.data == "send_gift")
async def send_gift_menu(call: CallbackQuery, state: FSMContext):
    await state.set_state(GiftStates.choosing_gift)
    b = (
        MsgBuilder()
        .emoji("choose_gift").add(" ").bold("Выбери желаемый подарок ⌵").nl(2)
        .quote_emoji("sparkles", "Все подарки эксклюзивные и недоступны в обычном магазине Telegram.").nl(2)
        .emoji("money").add(f" Цена: {GIFT_PRICE_STARS} ").emoji("star").add(" звёзд")
    )
    await edit_msg(call.message, b, reply_markup=gifts_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("gift_"))
async def gift_selected(call: CallbackQuery, state: FSMContext):
    emoji_id = call.data.split("_", 1)[1]
    if emoji_id not in GIFTS:
        await call.answer("Подарок не найден", show_alert=True)
        return
    gift_info = GIFTS[emoji_id]
    await state.update_data(selected_gift=emoji_id)
    await state.set_state(GiftStates.choosing_recipient)

    b = (
        MsgBuilder()
        .emoji("check").add(" Вы выбрали этот подарок,").nl(2)
        .emoji_raw(emoji_id).add(f" {gift_info['name']}").nl(2)
        .bold("Кому будем отправлять?")
    )
    await edit_msg(call.message, b, reply_markup=recipient_kb())
    await call.answer()

def comment_step_text() -> MsgBuilder:
    return (
        MsgBuilder()
        .emoji("note").add(" ").bold("Комментарий к подарку").nl(2)
        .add("Можешь добавить личное сообщение получателю или пропустить этот шаг.")
    )

@dp.callback_query(F.data == "recipient_self")
async def recipient_self(call: CallbackQuery, state: FSMContext):
    await state.update_data(recipient="self", recipient_id=call.from_user.id)
    await state.set_state(GiftStates.choosing_comment)
    await edit_msg(call.message, comment_step_text(), reply_markup=comment_kb())
    await call.answer()

@dp.callback_query(F.data == "recipient_friend")
async def recipient_friend(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    emoji_id = data.get("selected_gift")
    user_id = call.from_user.id
    await create_pending_friend(user_id, emoji_id)

    link = f"https://t.me/{BOT_USERNAME}?start=friend_{user_id}"
    b = (
        MsgBuilder()
        .emoji("warn").add(" ").bold("Отправьте эту ссылку другу:").nl(2)
        .code(link).nl(2)
        .emoji("note").add(" ").bold("Зачем это?").nl()
        .quote("Друг переходит по ссылке и нажимает /start в боте, чтобы бот смог "
               "отправить ему подарок. После этого вы сможете обмениваться подарками.").nl(2)
        .emoji("clock").add(" Ожидаю, пока друг перейдёт по ссылке...")
    )
    await edit_msg(call.message, b, reply_markup=friend_ready_kb(emoji_id))
    await call.answer()

@dp.callback_query(F.data.startswith("friend_ready_"))
async def friend_ready(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    emoji_id = call.data.split("friend_ready_")[1]
    entry = await get_pending_friend(user_id)
    if not entry or not entry.get("friend_id"):
        await call.answer("❌ Друг ещё не перешёл по ссылке (или срок истёк). Попроси его нажать /start.", show_alert=True)
        return
    friend_id = entry["friend_id"]
    await state.update_data(selected_gift=emoji_id, recipient="friend", recipient_id=friend_id)
    await state.set_state(GiftStates.choosing_comment)
    await edit_msg(call.message, comment_step_text(), reply_markup=comment_kb())
    await call.answer()

@dp.callback_query(F.data == "friend_cancel")
async def friend_cancel(call: CallbackQuery, state: FSMContext):
    await delete_pending_friend(call.from_user.id)
    await state.clear()
    b = MsgBuilder().emoji("stop").add(" Приглашение отменено.")
    await edit_msg(call.message, b, reply_markup=main_menu_kb(is_owner=call.from_user.id == OWNER_ID))
    await call.answer()

@dp.callback_query(F.data == "comment_yes")
async def comment_yes(call: CallbackQuery, state: FSMContext):
    await state.set_state(GiftStates.entering_comment)
    kb = InlineKeyboardBuilder()
    kb.row(btn("Назад", "back_cancel", callback_data="comment_cancel"))
    b = (
        MsgBuilder()
        .emoji("note").add(" ").bold("Введите комментарий").add(" (максимум 200 символов, без премиум эмодзи):")
    )
    await edit_msg(call.message, b, reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data == "comment_cancel")
async def comment_cancel(call: CallbackQuery, state: FSMContext):
    await state.set_state(GiftStates.choosing_comment)
    await edit_msg(call.message, comment_step_text(), reply_markup=comment_kb())
    await call.answer()

@dp.callback_query(F.data == "comment_no")
async def comment_no(call: CallbackQuery, state: FSMContext):
    await state.update_data(comment="")
    await show_payment(call.message, state, edit=True)
    await call.answer()

@dp.message(GiftStates.entering_comment)
async def enter_comment(message: Message, state: FSMContext):
    comment = message.text or ""
    if len(comment) > 200:
        await message.answer("❌ Комментарий слишком длинный! Максимум 200 символов.")
        return
    await state.update_data(comment=comment)
    await show_payment(message, state, edit=False)

async def show_payment(msg, state: FSMContext, edit=False):
    data = await state.get_data()
    emoji_id = data.get("selected_gift")
    comment = data.get("comment", "")
    gift_info = GIFTS.get(emoji_id, {})
    recipient_id = data.get("recipient_id")
    recipient_name = await get_display_name(recipient_id) if recipient_id else "не указан"

    b = (
        MsgBuilder()
        .emoji("money").add(" ").bold("Оплата подарка.").nl(2)
        .add("Подарок будет: ").emoji_raw(emoji_id).add(f" {gift_info.get('name', '')}").nl()
        .emoji("user").add(f" Получатель: {recipient_name}").nl()
    )
    if comment:
        b.emoji("note").add(f" Комментарий: {comment}").nl()
    b.emoji("star").add(f" Стоимость: {GIFT_PRICE_STARS} звёзд")

    kb = InlineKeyboardBuilder()
    kb.row(btn(f"Оплатить {GIFT_PRICE_STARS} звёзд", "star", callback_data=f"pay_{emoji_id}"))
    kb.row(btn("Отмена", "back_cancel", callback_data="back_main"))

    await state.set_state(GiftStates.waiting_payment)

    if edit and hasattr(msg, "edit_text"):
        await edit_msg(msg, b, reply_markup=kb.as_markup())
    else:
        await send_msg(msg.chat.id, b, reply_markup=kb.as_markup())


# ─── PAYMENT ─────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("pay_"))
async def pay_gift(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    emoji_id = data.get("selected_gift")
    comment = data.get("comment", "")
    recipient_id = data.get("recipient_id", call.from_user.id)
    gift_info = GIFTS.get(emoji_id, {})
    gift_id = gift_info.get("gift_id")

    if not gift_id:
        await call.answer("Подарок не найден", show_alert=True)
        return

    # Заказ фиксируется в БД ДО отправки инвойса — payload содержит только его id,
    # так что сумма/получатель/подарок берутся из БД, а не из клиентских данных.
    order_id = await create_order(
        sender_id=call.from_user.id,
        recipient_id=recipient_id,
        emoji_id=emoji_id,
        gift_id=gift_id,
        comment=comment,
        amount=GIFT_PRICE_STARS,
        currency="XTR",
    )

    recipient_name = await get_display_name(recipient_id)
    desc_lines = [
        f"Вы покупаете: {gift_info.get('name', 'Подарок')}",
        f"для: {recipient_name}",
    ]
    if comment:
        desc_lines.append(f"Комментарий: {comment}")

    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"{gift_info.get('name', 'Подарок')}",
        description="\n".join(desc_lines),
        payload=str(order_id),
        currency="XTR",
        prices=[LabeledPrice(label="Подарок", amount=GIFT_PRICE_STARS)],
        provider_token="",
    )
    await call.answer()

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    order = None
    try:
        order = await get_order(int(pre_checkout_query.invoice_payload))
    except (ValueError, TypeError):
        order = None
    if not order or order["status"] != "pending":
        await bot.answer_pre_checkout_query(
            pre_checkout_query.id, ok=False, error_message="Заказ не найден или уже обработан."
        )
        return
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message, state: FSMContext):
    sp = message.successful_payment
    charge_id = sp.telegram_payment_charge_id

    try:
        order_id = int(sp.invoice_payload)
    except (ValueError, TypeError):
        logger.error(f"Некорректный payload платежа: {sp.invoice_payload!r} от {message.from_user.id}")
        await message.answer("❌ Ошибка обработки платежа. Обратитесь в поддержку.")
        await notify_owner(f"Некорректный payload платежа от {message.from_user.id}: {sp.invoice_payload!r}")
        return

    order = await get_order(order_id)
    if not order:
        logger.error(f"Заказ {order_id} не найден, но платёж прошёл (charge={charge_id})")
        await notify_owner(f"КРИТИЧНО: заказ {order_id} не найден, но платёж прошёл! charge={charge_id}, user={message.from_user.id}")
        await message.answer("❌ Заказ не найден. Обратитесь в поддержку, платёж будет проверен вручную.")
        return

    if order["status"] != "pending":
        # Повторный successful_payment update (дубликат) — не обрабатываем второй раз.
        logger.warning(f"Повторный successful_payment для заказа {order_id}, статус={order['status']}")
        return

    if sp.total_amount != order["amount"] or sp.total_amount != GIFT_PRICE_STARS or sp.currency != "XTR" or order["currency"] != "XTR":
        logger.error(f"Несовпадение суммы/валюты по заказу {order_id}: total={sp.total_amount} cur={sp.currency}")
        await set_order_status(order_id, "failed", telegram_charge_id=charge_id, completed=True)
        await refund_order(order_id, order["sender_id"], charge_id, reason="сумма/валюта не совпадают")
        await message.answer("❌ Ошибка проверки платежа. Средства возвращены.")
        return

    await set_order_status(order_id, "paid", telegram_charge_id=charge_id)

    recipient_id = order["recipient_id"]
    sender_id = order["sender_id"]
    emoji_id = order["emoji_id"]
    gift_id = order["gift_id"]
    comment = order["comment"]
    gift_info = GIFTS.get(emoji_id, {})

    success = await safe_send_gift(recipient_id, gift_id, comment)

    await ensure_user(sender_id)
    await ensure_user(recipient_id)
    await inc_gifts_sent(sender_id)
    if recipient_id != sender_id:
        await inc_gifts_received(recipient_id)

    # если получатель пришёл по реферальной ссылке — приглашение выполнено, чистим
    friend_entry = await get_pending_friend(sender_id)
    if friend_entry and friend_entry.get("friend_id") == recipient_id:
        await delete_pending_friend(sender_id)

    if success:
        await set_order_status(order_id, "completed", completed=True)
        b = (
            MsgBuilder()
            .emoji("check").add(" ").bold("Подарок успешно отправлен!").nl(2)
            .emoji_raw(emoji_id).add(f" {gift_info.get('name', '')}").nl()
        )
        if comment:
            b.emoji("note").add(f" Комментарий: {comment}").nl()
        b.nl()
        b.quote_emoji("sparkles", "Спасибо за покупку! Получатель уже видит подарок в своём профиле.")
        await send_msg(message.chat.id, b, reply_markup=main_menu_kb())

        try:
            buyer_name = await get_display_name(sender_id)
            recipient_display = await get_display_name(recipient_id)
            nb = (
                MsgBuilder()
                .emoji("gift_box").add(" ").bold("Купили подарок!").nl(2)
                .emoji("user").add(f" Купил: {buyer_name} (").code(str(sender_id)).add(")").nl()
                .emoji_raw(emoji_id).add(f" Подарок: {gift_info.get('name', '')}").nl()
                .emoji("star").add(f" За: {GIFT_PRICE_STARS} звёзд").nl()
                .emoji("point").add(f" Получатель: {recipient_display} (").code(str(recipient_id)).add(")").nl(2)
                .emoji("check").add(" Подарок успешно отправлен пользователю.")
            )
            await send_msg(OWNER_ID, nb)
        except Exception:
            pass
    else:
        await refund_order(order_id, sender_id, charge_id, reason="send_gift не удался")
        b = (
            MsgBuilder()
            .emoji("stop").add(" ").bold("Не удалось отправить подарок.").nl(2)
            .add("Обратитесь в поддержку: @vocma").nl()
            .add("Ваш платёж возвращён.")
        )
        await send_msg(message.chat.id, b, reply_markup=main_menu_kb())


# ─── ADMIN PANEL ─────────────────────────────────────────────────────────────

GIFTS_PAGE_SIZE = 5

async def admin_panel_text() -> MsgBuilder:
    total = await count_users()
    return (
        MsgBuilder()
        .emoji("crown").add(" ").bold("Админ-панель").nl(2)
        .quote_emoji("sparkles", "Добро пожаловать в панель управления ботом.").nl(2)
        .emoji("stats").add(f" Всего пользователей: {total}").nl()
        .emoji("gift_box").add(f" Доступно подарков: {len(GIFTS)}")
    )

def _is_owner(uid):
    return uid == OWNER_ID

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not _is_owner(message.from_user.id):
        await send_msg(message.chat.id, MsgBuilder().emoji("stop").add(" Доступ запрещён."))
        return
    await send_msg(message.chat.id, await admin_panel_text(), reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_open")
async def admin_open(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await edit_msg(call.message, await admin_panel_text(), reply_markup=admin_kb())
    await call.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    total_users = await count_users()
    total_gifts = await sum_gifts_sent()
    new_today = await count_new_users_today()
    gifts_today = await count_gifts_completed_today()

    kb = InlineKeyboardBuilder()
    kb.row(btn("Назад", "back_cancel", callback_data="admin_back"))
    b = (
        MsgBuilder()
        .emoji("stats").add(" ").bold("Статистика бота").nl(2)
        .bold("Всего:").nl()
        .quote_emoji("user", f"Пользователей: {total_users}\nКупили подарков: {total_gifts}").nl(2)
        .bold("За сегодня:").nl()
        .quote_emoji("new", f"Новых пользователей: {new_today}\nКуплено подарков сегодня: {gifts_today}")
    )
    await edit_msg(call.message, b, reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("admin_gifts_"))
async def admin_gifts(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    page = int(call.data.split("_")[-1])
    items = list(GIFTS.items())
    start, end = page * GIFTS_PAGE_SIZE, page * GIFTS_PAGE_SIZE + GIFTS_PAGE_SIZE
    b = MsgBuilder().emoji("gift_box").add(" ").bold(f"Список подарков (стр. {page + 1})").nl(2)
    for emoji_id, info in items[start:end]:
        b.emoji_raw(emoji_id).add(f" {info['name']}").nl()
        b.add("  gift_id: ").code(info["gift_id"]).nl(2)

    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_gifts_{page - 1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_gifts_{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(btn("Назад", "back_cancel", callback_data="admin_back"))
    await edit_msg(call.message, b, reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data == "admin_give_stars")
async def admin_give_stars_start(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await state.set_state(GiftStates.admin_give_stars_user)
    kb = InlineKeyboardBuilder()
    kb.row(btn("Отмена", "back_cancel", callback_data="admin_back"))
    b = MsgBuilder().emoji("star").add(" ").bold("Выдача звёзд").nl(2).add("Введите ID пользователя:")
    await edit_msg(call.message, b, reply_markup=kb.as_markup())
    await call.answer()

@dp.message(GiftStates.admin_give_stars_user)
async def admin_give_stars_user(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await send_msg(message.chat.id, MsgBuilder().emoji("stop").add(" Неверный ID. Введите число."))
        return
    await state.update_data(target_user=uid)
    await state.set_state(GiftStates.admin_give_stars_amount)
    await send_msg(message.chat.id, MsgBuilder().emoji("star").add(" Введите количество звёзд (от 1 до 1 000 000):"))

@dp.message(GiftStates.admin_give_stars_amount)
async def admin_give_stars_amount(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    try:
        amount = int(message.text.strip())
    except ValueError:
        await send_msg(message.chat.id, MsgBuilder().emoji("stop").add(" Введите число."))
        return
    if amount <= 0 or amount > 1_000_000:
        await send_msg(message.chat.id, MsgBuilder().emoji("stop").add(" Сумма должна быть от 1 до 1 000 000 звёзд."))
        return

    data = await state.get_data()
    uid = data["target_user"]
    await add_stars(uid, amount)
    await state.clear()
    b = MsgBuilder().emoji("check").add(f" Выдано {amount} ").emoji("star").add(f" пользователю ").code(str(uid))
    await send_msg(message.chat.id, b, reply_markup=admin_kb())
    try:
        nb = MsgBuilder().emoji("star").add(f" Вам начислено {amount} звёзд от администратора!")
        await send_msg(uid, nb)
    except Exception:
        pass

@dp.callback_query(F.data == "admin_set_price")
async def admin_set_price_start(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await state.set_state(GiftStates.admin_set_price)
    kb = InlineKeyboardBuilder()
    kb.row(btn("Отмена", "back_cancel", callback_data="admin_back"))
    b = (
        MsgBuilder()
        .emoji("money").add(" ").bold("Изменение цены подарка").nl(2)
        .add(f"Текущая цена: {GIFT_PRICE_STARS} звёзд").nl(2)
        .add("Введите новую цену в звёздах (число):")
    )
    await edit_msg(call.message, b, reply_markup=kb.as_markup())
    await call.answer()

@dp.message(GiftStates.admin_set_price)
async def admin_set_price_apply(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    global GIFT_PRICE_STARS
    try:
        price = int(message.text.strip())
    except ValueError:
        await send_msg(message.chat.id, MsgBuilder().emoji("stop").add(" Введите целое число."))
        return
    if price <= 0 or price > 1_000_000:
        await send_msg(message.chat.id, MsgBuilder().emoji("stop").add(" Цена должна быть от 1 до 1 000 000 звёзд."))
        return

    GIFT_PRICE_STARS = price
    await set_setting("gift_price_stars", price)
    await state.clear()
    b = MsgBuilder().emoji("check").add(f" Новая цена подарка: {price} ").emoji("star")
    await send_msg(message.chat.id, b, reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await state.set_state(GiftStates.admin_broadcast)
    kb = InlineKeyboardBuilder()
    kb.row(btn("Отмена", "back_cancel", callback_data="admin_back"))
    b = MsgBuilder().emoji("megaphone").add(" ").bold("Рассылка").nl(2).add("Введите текст сообщения:")
    await edit_msg(call.message, b, reply_markup=kb.as_markup())
    await call.answer()

@dp.message(GiftStates.admin_broadcast)
async def admin_broadcast_preview(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    await state.update_data(broadcast_text=message.text)
    await state.set_state(GiftStates.admin_broadcast_confirm)
    total = await count_users()
    kb = InlineKeyboardBuilder()
    kb.row(btn("Отправить всем", "check", callback_data="admin_broadcast_confirm"))
    kb.row(btn("Отмена", "back_cancel", callback_data="admin_back"))
    b = (
        MsgBuilder()
        .emoji("megaphone").add(" ").bold("Подтвердите рассылку").nl(2)
        .add(f"Получателей: {total}").nl(2)
        .quote(message.text)
    )
    await send_msg(message.chat.id, b, reply_markup=kb.as_markup())

@dp.callback_query(F.data == "admin_broadcast_confirm")
async def admin_broadcast_send(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()
    await call.answer("Рассылка запущена…")

    ids = await active_user_ids()
    sent = failed = 0
    for uid in ids:
        ok = await safe_send_message(uid, text)
        if ok:
            sent += 1
        else:
            failed += 1
    logger.info(f"Рассылка завершена: sent={sent} failed={failed}")
    await send_msg(
        call.message.chat.id,
        MsgBuilder()
        .emoji("megaphone").add(" ").bold("Рассылка завершена!").nl()
        .emoji("check").add(f" Отправлено: {sent}").nl()
        .emoji("stop").add(f" Не доставлено: {failed}"),
        reply_markup=admin_kb(),
    )

@dp.callback_query(F.data == "admin_export_users")
async def admin_export_users(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    rows = await all_users_for_export()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "full_name", "balance", "gifts_received", "gifts_sent", "is_active", "created_at"])
    for u in rows:
        writer.writerow([u["user_id"], u["username"], u["full_name"], u["balance"], u["gifts_received"], u["gifts_sent"], u["is_active"], u["created_at"]])
    file = BufferedInputFile(buf.getvalue().encode("utf-8"), filename="users.csv")
    await bot.send_document(call.message.chat.id, file)
    await call.answer()

@dp.callback_query(F.data == "admin_export_orders")
async def admin_export_orders(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    rows = await all_orders_for_export()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["order_id", "sender_id", "recipient_id", "emoji_id", "gift_id", "comment", "amount", "currency", "status", "telegram_charge_id", "created_at", "completed_at"])
    for o in rows:
        writer.writerow([o["order_id"], o["sender_id"], o["recipient_id"], o["emoji_id"], o["gift_id"], o["comment"], o["amount"], o["currency"], o["status"], o["telegram_charge_id"], o["created_at"], o["completed_at"]])
    file = BufferedInputFile(buf.getvalue().encode("utf-8"), filename="payments.csv")
    await bot.send_document(call.message.chat.id, file)
    await call.answer()

@dp.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await edit_msg(call.message, await admin_panel_text(), reply_markup=admin_kb())
    await call.answer()


# ─── ФОНОВОЕ ОБНОВЛЕНИЕ USERNAME ────────────────────────────────────────────
# Раз в USERNAME_REFRESH_INTERVAL_SEC (2 мин) подтягиваем актуальные username
# у активных пользователей через bot.get_chat(). Работает как отдельная фоновая
# задача (asyncio.create_task) и не блокирует обработку сообщений/кнопок бота:
# между запросами есть пауза (USERNAME_REFRESH_DELAY_SEC), чтобы не упереться
# в лимиты Telegram API и не создавать нагрузку рывками.
async def refresh_usernames_loop():
    while True:
        await asyncio.sleep(USERNAME_REFRESH_INTERVAL_SEC)
        try:
            ids = await active_user_ids()
        except Exception as e:
            logger.error(f"Не удалось получить список пользователей для обновления username: {e}")
            continue

        updated = 0
        for uid in ids:
            try:
                chat = await bot.get_chat(uid)
                new_username = chat.username
                row = await db_fetchone("SELECT username FROM users WHERE user_id=?", (uid,))
                if row and row["username"] != new_username:
                    await update_username(uid, new_username)
                    updated += 1
            except TelegramRetryAfter as e:
                logger.warning(f"FloodWait при get_chat({uid}): retry_after={e.retry_after}")
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await set_user_active(uid, False)
            except Exception as e:
                logger.warning(f"Не удалось обновить username для {uid}: {e}")

            await asyncio.sleep(USERNAME_REFRESH_DELAY_SEC)

        if updated:
            logger.info(f"Username обновлены у {updated} пользователей")


# ─── RUN ─────────────────────────────────────────────────────────────────────

async def main():
    global BOT_USERNAME, GIFT_PRICE_STARS
    logger.info("Starting bot...")
    await init_db()
    saved_price = await get_setting("gift_price_stars")
    if saved_price is not None:
        try:
            GIFT_PRICE_STARS = int(saved_price)
        except ValueError:
            pass
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"BOT_USERNAME = @{BOT_USERNAME}")
    asyncio.create_task(refresh_usernames_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())