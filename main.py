import asyncio
import logging
import os
import random
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден! Проверьте файл .env или переменные окружения хостинга")

PRIMARY_ADMIN_ID = 5785237497
LOG_CHANNEL_ID = "@StarPays_Reviews"
BOT_USERNAME = "@StarPays24_bot"
REWARD_PER_SUB = 0.25
UNSUB_PENALTY = 1.0

DB_PATH = os.getenv("DB_PATH", "/app/data/bot_database.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

WITHDRAWS_PER_PAGE = 10
TOP_USERS_LIMIT = 10
DAILY_BONUS_MIN = 0.1
DAILY_BONUS_MAX = 1.0
DAILY_BONUS_STEP = 0.1
GIFT_TEXT_PRICE = 0.5
GIFT_TEXT_MAX_LEN = 100
CLICK_REWARD_DEFAULT = 0.1
CLICK_COOLDOWN_MIN_DEFAULT = 10
CAPTCHA_TTL_DAYS = 7

REQUIRED_SPONSORS = [
    {
        "chat": "@StarPays_Reviews",
        "title": "StarPays Reviews",
        "link": "https://t.me/StarPays_Reviews",
    },
    {
        "chat": "@StarPay_s",
        "title": "StarPay",
        "link": "https://t.me/StarPay_s",
    },
]

CAPTCHA_FRUITS = [
    ("🥭", "манго"),
    ("🍎", "яблоко"),
    ("🍋", "лимон"),
    ("🍑", "персик"),
    ("🍇", "виноград"),
    ("🍓", "клубнику"),
    ("🥝", "киви"),
    ("🍒", "вишню"),
]

GIFTS = [
    ("🧸", 15),
    ("💖", 15),
    ("🎁", 25),
    ("🌹", 25),
    ("🍾", 50),
    ("💐", 50),
    ("🚀", 50),
    ("🎂", 50),
    ("🏆", 100),
    ("💍", 100),
    ("💎", 100),
]

BTN_EARN = "⭐ Заработать Звёзды"
BTN_TASKS = "💎 Задания"
BTN_WITHDRAW = "🎁 Вывести Звёзды"
BTN_PROFILE = "👤 Профиль"
BTN_ADMIN = "👑 Админ-панель"

MENU_BUTTONS = [BTN_EARN, BTN_TASKS, BTN_WITHDRAW, BTN_PROFILE, BTN_ADMIN]

captcha_sessions: dict[int, dict] = {}


def escape_md(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))


def quote_block(text: str) -> str:
    lines = text.split("\n")
    return "\n".join(f">{line}" if line else ">" for line in lines)


def random_daily_bonus() -> float:
    steps = round((DAILY_BONUS_MAX - DAILY_BONUS_MIN) / DAILY_BONUS_STEP)
    chosen_step = random.randint(0, steps)
    return round(DAILY_BONUS_MIN + chosen_step * DAILY_BONUS_STEP, 1)


def escape_md_preserve_formatting(text: str) -> str:
    def esc_line(line: str) -> str:
        return re.sub(r"(?<!\\)([.\-!()])", r"\\\1", line)

    return "\n".join(esc_line(l) for l in text.split("\n"))


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2))
dp = Dispatcher(storage=MemoryStorage())


async def send_log(text: str, disable_preview: bool = False):
    if not LOG_CHANNEL_ID:
        return
    try:
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=disable_preview,
        )
    except Exception as e:
        logging.error(f"Ошибка отправки лога в канал: {e}")


async def log_balance_change(db: aiosqlite.Connection, user_id: int, amount: float, source: str):
    if amount <= 0:
        return
    await db.execute(
        "INSERT INTO balance_log (user_id, amount, source, created_at) VALUES (?, ?, ?, ?)",
        (user_id, amount, source, datetime.now().isoformat()),
    )


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


async def is_top_enabled() -> bool:
    return await get_setting("top_enabled", "1") == "1"


async def is_click_enabled() -> bool:
    return await get_setting("click_enabled", "1") == "1"


async def get_click_reward() -> float:
    try:
        return float(str(await get_setting("click_reward", str(CLICK_REWARD_DEFAULT))).replace(",", "."))
    except ValueError:
        return CLICK_REWARD_DEFAULT


async def get_click_cooldown_min() -> int:
    try:
        return int(float(str(await get_setting("click_cooldown_min", str(CLICK_COOLDOWN_MIN_DEFAULT))).replace(",", ".")))
    except ValueError:
        return CLICK_COOLDOWN_MIN_DEFAULT


async def get_ref_reward() -> float:
    return float(await get_setting("ref_reward", "5.0"))


async def backup_db_loop():
    backup_dir = Path(DB_PATH).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"bot_database_{timestamp}.db"
            if Path(DB_PATH).exists():
                shutil.copy(DB_PATH, backup_path)
            backups = sorted(backup_dir.glob("bot_database_*.db"))
            if len(backups) > 14:
                for old in backups[:-14]:
                    old.unlink()
        except Exception as e:
            logging.error(f"Ошибка бэкапа базы: {e}")
        await asyncio.sleep(24 * 60 * 60)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0.0,
                is_passed_captcha INTEGER DEFAULT 0,
                completed_tasks TEXT DEFAULT '',
                used_promo INTEGER DEFAULT 0,
                referrer_id INTEGER DEFAULT NULL,
                last_daily TIMESTAMP DEFAULT NULL,
                last_click TIMESTAMP DEFAULT NULL,
                is_banned INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        for col_def in [
            "referrer_id INTEGER DEFAULT NULL",
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "last_daily TIMESTAMP DEFAULT NULL",
            "last_click TIMESTAMP DEFAULT NULL",
            "is_banned INTEGER DEFAULT 0",
            "last_captcha_at TIMESTAMP DEFAULT NULL",
            "referral_paid INTEGER DEFAULT 0",
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
            except Exception:
                pass

        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (PRIMARY_ADMIN_ID,))
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT,
                title TEXT,
                link TEXT
            )
        """)
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ref_reward', '5.0')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('top_enabled', '1')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('click_enabled', '1')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('click_reward', ?)", (str(CLICK_REWARD_DEFAULT),))
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('click_cooldown_min', ?)", (str(CLICK_COOLDOWN_MIN_DEFAULT),))

        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdraws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col_def in [
            "gift_text TEXT DEFAULT NULL",
            "gift_emoji TEXT DEFAULT '⭐'",
        ]:
            try:
                await db.execute(f"ALTER TABLE withdraws ADD COLUMN {col_def}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS checks (
                code TEXT PRIMARY KEY,
                creator_id INTEGER,
                amount REAL,
                is_used INTEGER DEFAULT 0,
                used_by INTEGER,
                chat_id INTEGER,
                msg_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY,
                amount REAL,
                max_activations INTEGER,
                used_activations INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_activations (
                promo_code TEXT,
                user_id INTEGER,
                activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (promo_code, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS balance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_balance_log_user_time ON balance_log(user_id, created_at)")

        async with db.execute("SELECT value FROM settings WHERE key = 'ref_v2_migrated'") as cursor:
            migrated = await cursor.fetchone()
        if not migrated:
            await db.execute("UPDATE users SET referral_paid = 1 WHERE referrer_id IS NOT NULL")
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ref_v2_migrated', '1')")

        await db.commit()


async def is_admin(user_id: int) -> bool:
    if user_id == PRIMARY_ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None


async def is_user_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row and row[0])


async def needs_captcha(user_id: int) -> bool:
    if await is_admin(user_id):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_passed_captcha, last_captcha_at FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return True
    passed, last_at = row
    if not passed:
        return True
    if not last_at:
        return True
    try:
        last = datetime.fromisoformat(str(last_at))
    except ValueError:
        return True
    return datetime.now() - last >= timedelta(days=CAPTCHA_TTL_DAYS)


async def mark_captcha_passed(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_passed_captcha = 1, last_captcha_at = ? WHERE user_id = ?",
            (datetime.now().isoformat(), user_id),
        )
        await db.commit()


async def get_missing_sponsors(user_id: int) -> list[dict]:
    missing = []
    for sp in REQUIRED_SPONSORS:
        try:
            member = await bot.get_chat_member(chat_id=sp["chat"], user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                missing.append(sp)
        except Exception as e:
            logging.warning(f"Не удалось проверить подписку {sp['chat']} для {user_id}: {e}")
            missing.append(sp)
    return missing


async def maybe_pay_referrer(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT referrer_id, referral_paid, completed_tasks FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return
        referrer_id, referral_paid, completed_tasks = row
        if not referrer_id or referral_paid:
            return
        tasks = [x for x in (completed_tasks or "").split(",") if x]
        if not tasks:
            return

    if await get_missing_sponsors(user_id):
        return

    ref_reward = await get_ref_reward()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE users SET referral_paid = 1 WHERE user_id = ? AND referral_paid = 0 AND referrer_id IS NOT NULL",
            (user_id,),
        )
        if cur.rowcount == 0:
            await db.commit()
            return
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (ref_reward, referrer_id))
        await log_balance_change(db, referrer_id, ref_reward, "referral")
        await db.commit()

    try:
        rew = escape_md(f"{ref_reward}")
        await bot.send_message(
            referrer_id,
            f"🎉 Друг перешел \\. Тебе начислено \\+`{rew}` 💫",
        )
    except Exception:
        pass


class AdminStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_welcome_photo = State()
    waiting_for_new_admin = State()
    waiting_for_check_amount = State()
    waiting_for_ref_reward = State()
    waiting_for_broadcast = State()
    waiting_for_balance_user = State()
    waiting_for_balance_value = State()
    waiting_for_promo_code = State()
    waiting_for_promo_amount = State()
    waiting_for_promo_limit = State()
    waiting_for_click_reward = State()
    waiting_for_click_cooldown = State()
    waiting_for_ban_user = State()
    waiting_for_unban_user = State()
    waiting_for_find_user = State()


class UserStates(StatesGroup):
    waiting_for_promo = State()
    waiting_for_gift_text = State()


def back_admin_kb(*extra_rows: list[tuple[str, str]]):
    builder = InlineKeyboardBuilder()
    for row in extra_rows:
        for text, cb in row:
            builder.button(text=text, callback_data=cb)
        builder.adjust(len(row) if len(row) <= 3 else 2)
    builder.button(text="‹ Назад в админку", callback_data="admin_home")
    builder.adjust(1)
    return builder.as_markup()


async def main_keyboard(user_id: int):
    builder = ReplyKeyboardBuilder()
    builder.button(text=BTN_EARN)
    builder.button(text=BTN_TASKS)
    builder.button(text=BTN_WITHDRAW)
    builder.button(text=BTN_PROFILE)
    if await is_admin(user_id):
        builder.button(text=BTN_ADMIN)
        builder.adjust(2, 1, 1, 1)
    else:
        builder.adjust(2, 1, 1)
    return builder.as_markup(resize_keyboard=True)


def admin_home_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="🔎 Информация", callback_data="admin_people")
    builder.button(text="⏳ Выводы", callback_data="admin_pending_w_page:0")
    builder.button(text="📢 Контент", callback_data="admin_content")
    builder.button(text="⚙️ Настройки", callback_data="admin_settings")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def admin_people_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Баланс", callback_data="admin_change_balance")
    builder.button(text="🔎 Найти", callback_data="admin_find_user")
    builder.button(text="🏆 Топ", callback_data="admin_top_users")
    builder.button(text="🚫 Бан", callback_data="admin_ban_user")
    builder.button(text="✅ Разбан", callback_data="admin_unban_user")
    builder.button(text="➕ Админ", callback_data="admin_add_admin")
    builder.button(text="👥 Кто админ", callback_data="admin_list_admins")
    builder.button(text="‹ Назад", callback_data="admin_home")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def admin_content_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Канал", callback_data="admin_add_channel")
    builder.button(text="📋 Каналы", callback_data="admin_list_channels")
    builder.button(text="🎟 Промо", callback_data="admin_promo_menu")
    builder.button(text="🧸 Чек", callback_data="admin_create_check")
    builder.button(text="🖼 Баннер", callback_data="admin_set_photo")
    builder.button(text="📣 Рассылка", callback_data="admin_broadcast")
    builder.button(text="‹ Назад в админку", callback_data="admin_home")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def admin_settings_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Рефка", callback_data="admin_set_ref_reward")
    builder.button(text="🖱 Клик", callback_data="admin_click_menu")
    builder.button(text="🏆 Топы вкл/выкл", callback_data="admin_toggle_top")
    builder.button(text="‹ Назад в админку", callback_data="admin_home")
    builder.adjust(1)
    return builder.as_markup()


def click_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Награда", callback_data="click_set_reward")
    builder.button(text="Кулдаун", callback_data="click_set_cooldown")
    builder.button(text="Вкл / выкл", callback_data="click_toggle")
    builder.button(text="‹ К настройкам", callback_data="admin_settings")
    builder.adjust(1)
    return builder.as_markup()


async def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎟 Промокод", callback_data="activate_promo")
    if await is_top_enabled():
        builder.button(text="🏆 Топ", callback_data="show_top:all:0")
    builder.adjust(1)
    return builder.as_markup()


def withdraw_keyboard():
    builder = InlineKeyboardBuilder()
    for idx, (emoji, amount) in enumerate(GIFTS):
        builder.button(text=f"{emoji} {amount}⭐", callback_data=f"wdg:{idx}")
    builder.button(text="‹ Назад", callback_data="back_to_main")
    builder.adjust(2, 2, 2, 2, 2, 1, 1)
    return builder.as_markup()


def gift_text_ask_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Да, добавить (+{GIFT_TEXT_PRICE}⭐)", callback_data="gift_ask:yes")
    builder.button(text="Нет, без надписи", callback_data="gift_ask:no")
    builder.adjust(1)
    return builder.as_markup()


def gift_text_cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="‹ Отмена", callback_data="gift_ask_back")
    builder.adjust(1)
    return builder.as_markup()


def top_period_keyboard(active_period: str):
    labels = {"day": "За день", "week": "За неделю", "all": "Всё время"}
    builder = InlineKeyboardBuilder()
    for period, label in labels.items():
        text = f"· {label} ·" if period == active_period else label
        builder.button(text=text, callback_data=f"show_top:{period}:0")
    builder.button(text="‹ В профиль", callback_data="profile_back")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def promo_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Новый промокод", callback_data="promo_create_start")
    builder.button(text="📋 Список", callback_data="promo_list")
    builder.button(text="‹ Назад в админку", callback_data="admin_home")
    builder.adjust(1)
    return builder.as_markup()


def earn_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🖱 Клик", callback_data="earn_click")
    builder.button(text="📅 Бонус дня", callback_data="earn_daily")
    builder.button(text="👥 Друзья", callback_data="earn_friends")
    builder.adjust(1)
    return builder.as_markup()


async def render_admin_home(target) -> tuple[str, types.InlineKeyboardMarkup]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM withdraws WHERE status = 'pending'") as c:
            pending_withdraws = (await c.fetchone())[0]
    text = (
        "👑 *Админка*\n\n"
        f"Людей в боте: *{escape_md(str(total_users))}*\n"
        f"Заявок на вывод: *{escape_md(str(pending_withdraws))}*\n\n"
        "Куда лезем?"
    )
    return text, admin_home_keyboard()


async def edit_or_send(callback: types.CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


def math_captcha_keyboard(correct: int) -> types.InlineKeyboardMarkup:
    options = {correct}
    while len(options) < 6:
        delta = random.randint(-9, 9)
        val = correct + (delta if delta else 3)
        if val > 0:
            options.add(val)
    shuffled = list(options)
    random.shuffle(shuffled)
    builder = InlineKeyboardBuilder()
    for val in shuffled:
        builder.button(text=str(val), callback_data=f"capm:{val}")
    builder.adjust(3)
    return builder.as_markup()


def fruit_captcha_keyboard(correct_emoji: str) -> types.InlineKeyboardMarkup:
    others = [item for item in CAPTCHA_FRUITS if item[0] != correct_emoji]
    picked = random.sample(others, k=5)
    picked.append(next(item for item in CAPTCHA_FRUITS if item[0] == correct_emoji))
    random.shuffle(picked)
    builder = InlineKeyboardBuilder()
    for emoji_char, _ in picked:
        builder.button(text=emoji_char, callback_data=f"capf:{emoji_char}")
    builder.adjust(3)
    return builder.as_markup()


async def send_captcha(message: types.Message):
    a = random.randint(8, 24)
    b = random.randint(6, 17)
    op = random.choice(["+", "-"])
    if op == "+" :
        correct = a + b
        task = f"{a} \\+ {b}"
    else:
        if b > a:
            a, b = b, a
        correct = a - b
        task = f"{a} \\- {b}"
    captcha_sessions[message.from_user.id] = {"step": "math", "correct": correct}
    text = (
        "🤖 *Проверка*\n\n"
        f"Считай без калькулятора: *{task}*\n"
        "Нажми правильный ответ\\."
    )
    await message.answer(text, reply_markup=math_captcha_keyboard(correct))


async def send_fruit_captcha(event_message: types.Message, user_id: int):
    emoji_char, name = random.choice(CAPTCHA_FRUITS)
    captcha_sessions[user_id] = {"step": "fruit", "correct": emoji_char}
    text = (
        "🤖 *Ещё один шаг*\n\n"
        f"Найди на кнопках *{escape_md(name)}*\\.\n"
        "Эмодзи в тексте специально нет — так меньше ботов проходит\\."
    )
    kb = fruit_captcha_keyboard(emoji_char)
    try:
        await event_message.edit_text(text, reply_markup=kb)
    except Exception:
        await event_message.answer(text, reply_markup=kb)


async def send_sponsor_gate(message: types.Message, missing: list[dict]):
    builder = InlineKeyboardBuilder()
    for sp in REQUIRED_SPONSORS:
        mark = "✅ " if sp not in missing else ""
        builder.button(text=f"{mark}{sp['title']}", url=sp["link"])
    builder.button(text="Проверить подписку", callback_data="check_sponsors")
    builder.adjust(1)
    text = (
        "📢 *Сначала подписка на спонсоров*\n\n"
        "Без этого бот закрыт — задания, клик и вывод не работают\\.\n"
        "Подпишись на оба канала и жми проверку\\."
    )
    await message.answer(text, reply_markup=builder.as_markup())


async def user_can_use_bot(user_id: int, event) -> bool:
    if await is_user_banned(user_id):
        text = "🚫 Ты заблокирован в боте\\.\nЕсли это ошибка — напиши админу\\."
        if isinstance(event, types.CallbackQuery):
            await event.answer("Ты заблокирован", show_alert=True)
        else:
            await event.answer(text)
        return False

    if await needs_captcha(user_id):
        msg = event.message if isinstance(event, types.CallbackQuery) else event
        if isinstance(event, types.CallbackQuery):
            await event.answer("Сначала проверка", show_alert=True)
        await send_captcha(msg)
        return False

    if not await is_admin(user_id):
        missing = await get_missing_sponsors(user_id)
        if missing:
            msg = event.message if isinstance(event, types.CallbackQuery) else event
            if isinstance(event, types.CallbackQuery):
                await event.answer("Нужна подписка на спонсоров", show_alert=True)
            await send_sponsor_gate(msg, missing)
            return False
    return True


@dp.callback_query(F.data.startswith("capm:"))
async def process_math_captcha(callback: types.CallbackQuery):
    session = captcha_sessions.get(callback.from_user.id)
    if not session or session.get("step") != "math":
        await callback.answer("Капча устарела, нажми /start", show_alert=True)
        return
    clicked = int(callback.data.split(":")[1])
    if clicked != session["correct"]:
        await callback.answer("Мимо. Новые цифры.", show_alert=True)
        a = random.randint(8, 24)
        b = random.randint(6, 17)
        correct = a + b
        captcha_sessions[callback.from_user.id] = {"step": "math", "correct": correct}
        text = (
            "🤖 *Проверка*\n\n"
            f"Считай: *{a} \\+ {b}*"
        )
        await callback.message.edit_text(text, reply_markup=math_captcha_keyboard(correct))
        return
    await callback.answer("Ок, дальше")
    await send_fruit_captcha(callback.message, callback.from_user.id)


@dp.callback_query(F.data.startswith("capf:"))
async def process_fruit_captcha(callback: types.CallbackQuery):
    session = captcha_sessions.get(callback.from_user.id)
    if not session or session.get("step") != "fruit":
        await callback.answer("Капча устарела, нажми /start", show_alert=True)
        return
    clicked = callback.data.split(":", 1)[1]
    if clicked != session["correct"]:
        await callback.answer("Не то. Смотри ещё раз.", show_alert=True)
        emoji_char, name = random.choice(CAPTCHA_FRUITS)
        captcha_sessions[callback.from_user.id] = {"step": "fruit", "correct": emoji_char}
        text = (
            "🤖 *Ещё один шаг*\n\n"
            f"Найди на кнопках *{escape_md(name)}*\\."
        )
        await callback.message.edit_text(text, reply_markup=fruit_captcha_keyboard(emoji_char))
        return
    captcha_sessions.pop(callback.from_user.id, None)
    await mark_captcha_passed(callback.from_user.id)
    await callback.answer("Прошёл")
    try:
        await callback.message.delete()
    except Exception:
        pass
    missing = [] if await is_admin(callback.from_user.id) else await get_missing_sponsors(callback.from_user.id)
    if missing:
        await send_sponsor_gate(callback.message, missing)
        return
    await show_start_screen(callback.message, callback.from_user)


@dp.callback_query(F.data == "check_sponsors")
async def check_sponsors_handler(callback: types.CallbackQuery):
    if await is_user_banned(callback.from_user.id):
        await callback.answer("Ты заблокирован", show_alert=True)
        return
    missing = await get_missing_sponsors(callback.from_user.id)
    if missing:
        names = ", ".join(sp["title"] for sp in missing)
        await callback.answer(f"Ещё нет подписки: {names}", show_alert=True)
        builder = InlineKeyboardBuilder()
        for sp in REQUIRED_SPONSORS:
            mark = "✅ " if sp not in missing else ""
            builder.button(text=f"{mark}{sp['title']}", url=sp["link"])
        builder.button(text="Проверить подписку", callback_data="check_sponsors")
        builder.adjust(1)
        try:
            await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
        except Exception:
            pass
        return
    await maybe_pay_referrer(callback.from_user.id)
    await callback.answer("Есть, можно работать")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await show_start_screen(callback.message, callback.from_user)


@dp.message(F.text.in_(MENU_BUTTONS))
async def guard_menu_buttons(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not await user_can_use_bot(user_id, message):
        return
    handlers = {
        BTN_EARN: earn_hub_cmd,
        BTN_TASKS: earn_cmd,
        BTN_WITHDRAW: withdraw_cmd,
        BTN_PROFILE: profile_cmd,
        BTN_ADMIN: admin_panel,
    }
    handler = handlers.get(message.text)
    if handler:
        await handler(message, state)


async def show_start_screen(message: types.Message, user: types.User):
    first_name_esc = escape_md(user.first_name)
    welcome_text = (
        f"Привет, *{first_name_esc}* 👋\n\n"
        "Фарми звёзды: задания, клик, друзья\\.\n"
        "Потом меняй на подарки Telegram\\."
    )
    welcome_photo = None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'welcome_photo'") as cursor:
            row = await cursor.fetchone()
            if row:
                welcome_photo = row[0]
    kb = await main_keyboard(user.id)
    if welcome_photo:
        try:
            await message.answer_photo(photo=welcome_photo, caption=welcome_text, reply_markup=kb)
        except Exception:
            await message.answer(welcome_text, reply_markup=kb)
    else:
        await message.answer(welcome_text, reply_markup=kb)


async def try_activate_check(message: types.Message, args: str) -> bool:
    if not args or not args.startswith("check_"):
        return False
    check_code = args.replace("check_", "")
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT creator_id, amount, is_used, chat_id, msg_id FROM checks WHERE code = ?",
            (check_code,),
        ) as cursor:
            check_data = await cursor.fetchone()
        if not check_data:
            await message.answer("❌ Чек не найден или уже недействителен\\.")
            return True
        creator_id, amount, is_used, c_chat_id, c_msg_id = check_data
        if user_id == creator_id:
            await message.answer("❌ Свой чек активировать нельзя\\.")
            return True
        if is_used != 0:
            await message.answer("❌ Этот чек уже забрали\\.")
            return True
        await db.execute("UPDATE checks SET is_used = 1, used_by = ? WHERE code = ?", (user_id, check_code))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await log_balance_change(db, user_id, amount, "check")
        await db.commit()
    amount_val = int(amount) if amount.is_integer() else amount
    amount_esc = escape_md(f"{amount_val}")
    await message.answer(f"🎉 Чек на `{amount_esc}` ⭐ активирован\\.")
    if c_chat_id and c_msg_id:
        builder = InlineKeyboardBuilder()
        builder.button(text="Уже забрали", callback_data="check_already_used")
        try:
            await bot.edit_message_reply_markup(chat_id=c_chat_id, message_id=c_msg_id, reply_markup=builder.as_markup())
        except Exception:
            pass
    return True


@dp.message(Command("start"))
async def start_cmd(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or ""
    args = command.args

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            is_new = await cursor.fetchone() is None
        referrer_id = None
        if is_new and args and args.isdigit() and int(args) != user_id:
            referrer_id = int(args)
        await db.execute(
            "INSERT INTO users (user_id, username, balance, is_passed_captcha, completed_tasks, used_promo, referrer_id, referral_paid) "
            "VALUES (?, ?, 0.0, 0, '', 0, ?, 0) ON CONFLICT(user_id) DO UPDATE SET username = ?",
            (user_id, username, referrer_id, username),
        )
        await db.commit()

    if await is_user_banned(user_id):
        await message.answer("🚫 Ты заблокирован в боте\\.")
        return

    if await needs_captcha(user_id):
        await send_captcha(message)
        return

    if not await is_admin(user_id):
        missing = await get_missing_sponsors(user_id)
        if missing:
            await send_sponsor_gate(message, missing)
            return

    await try_activate_check(message, args or "")
    await show_start_screen(message, message.from_user)


@dp.callback_query(F.data == "check_already_used")
async def check_already_used_handler(callback: types.CallbackQuery):
    await callback.answer("Этот чек уже забрали", show_alert=True)


async def earn_hub_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    text = (
        "⭐ *Заработать звёзды*\n\n"
        "Клик — быстрый фарм с откатом\\.\n"
        "Бонус — раз в сутки\\.\n"
        "Друзья — основная касса, если кидать ссылку\\."
    )
    await message.answer(text, reply_markup=earn_keyboard())


@dp.callback_query(F.data == "earn_click")
async def earn_click_cb(callback: types.CallbackQuery, state: FSMContext):
    if not await user_can_use_bot(callback.from_user.id, callback):
        return
    await click_cmd(callback.message, state, from_user_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "earn_daily")
async def earn_daily_cb(callback: types.CallbackQuery, state: FSMContext):
    if not await user_can_use_bot(callback.from_user.id, callback):
        return
    await daily_bonus_cmd(callback.message, state, from_user_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "earn_friends")
async def earn_friends_cb(callback: types.CallbackQuery, state: FSMContext):
    if not await user_can_use_bot(callback.from_user.id, callback):
        return
    await friends_cmd(callback.message, state, from_user_id=callback.from_user.id)
    await callback.answer()


async def daily_bonus_cmd(message: types.Message, state: FSMContext, from_user_id: int | None = None):
    await state.clear()
    user_id = from_user_id or message.from_user.id
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_daily FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            last_daily_str = row[0] if row else None
        if last_daily_str:
            last_daily = datetime.fromisoformat(last_daily_str)
            if now < last_daily + timedelta(hours=24):
                remaining = (last_daily + timedelta(hours=24)) - now
                hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                await message.answer(
                    f"⏳ Бонус уже забирал\\.\nСледующий через *{escape_md(str(hours))} ч\\.* *{escape_md(str(minutes))} мин\\.*"
                )
                return
        bonus_amount = random_daily_bonus()
        await db.execute(
            "UPDATE users SET balance = balance + ?, last_daily = ? WHERE user_id = ?",
            (bonus_amount, now.isoformat(), user_id),
        )
        await log_balance_change(db, user_id, bonus_amount, "daily")
        await db.commit()
    bonus_esc = escape_md(f"{bonus_amount:.1f}")
    await message.answer(f"🎁 Забрал ежедневку: \\+{bonus_esc} ⭐")


async def click_cmd(message: types.Message, state: FSMContext, from_user_id: int | None = None):
    await state.clear()
    user_id = from_user_id or message.from_user.id
    if not await is_click_enabled():
        await message.answer("🖱 Клик сейчас выключен\\.")
        return
    now = datetime.now()
    cooldown_min = await get_click_cooldown_min()
    reward = await get_click_reward()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_click FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            last_click_str = row[0] if row else None
        if last_click_str:
            last_click = datetime.fromisoformat(last_click_str)
            if now < last_click + timedelta(minutes=cooldown_min):
                remaining = (last_click + timedelta(minutes=cooldown_min)) - now
                minutes, seconds = divmod(int(remaining.total_seconds()), 60)
                await message.answer(
                    "🖱 Рано ещё\\.\n"
                    + quote_block(f"Подожди {minutes} мин\\. {seconds} сек\\.")
                )
                return
        await db.execute(
            "UPDATE users SET balance = balance + ?, last_click = ? WHERE user_id = ?",
            (reward, now.isoformat(), user_id),
        )
        await log_balance_change(db, user_id, reward, "click")
        await db.commit()
    reward_esc = escape_md(f"{reward:.2f}")
    cd_esc = escape_md(str(cooldown_min))
    await message.answer(
        "🖱 Засчитано\\.\n"
        + quote_block(f"\\+{reward_esc} ⭐\nСледующий клик через {cd_esc} мин\\.")
    )


async def friends_cmd(message: types.Message, state: FSMContext, from_user_id: int | None = None):
    await state.clear()
    user_id = from_user_id or message.from_user.id
    bot_info = await bot.get_me()
    ref_reward = await get_ref_reward()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)) as cursor:
            invited_count = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id = ? AND referral_paid = 1", (user_id,)
        ) as cursor:
            paid_count = (await cursor.fetchone())[0]
    reward_esc = escape_md(f"{ref_reward}")
    invited_esc = escape_md(str(invited_count))
    paid_esc = escape_md(str(paid_count))
    ref_link_esc = escape_md(ref_link)
    text = (
        f"За друга: *\\+{reward_esc}* 💫\n\n"
        f"Ссылка:\n`{ref_link_esc}`\n\n"
        "Награда падает не сразу\\. Друг должен:\n"
        "1\\) подписаться на спонсоров\n"
        "2\\) закрыть хотя бы одно задание\n\n"
        f"Зашло по ссылке: *{invited_esc}*\n"
        f"Уже принесли награду: *{paid_esc}*"
    )
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Отправить ссылку",
        switch_inline_query=f"\n🚀 Забирай звёзды Telegram со мной: {ref_link}",
    )
    await message.answer(text, reply_markup=builder.as_markup())


async def check_unsubscriptions(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT completed_tasks FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            completed = row[0].split(",") if row and row[0] else []
        if not completed:
            return
        async with db.execute("SELECT id, channel_id, title FROM channels") as cursor:
            all_channels = {str(ch[0]): (ch[1], ch[2]) for ch in await cursor.fetchall()}
        unsubbed_tasks = []
        for ch_db_id in list(completed):
            if ch_db_id in all_channels:
                ch_id, _title = all_channels[ch_db_id]
                try:
                    member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
                    if member.status not in ["member", "administrator", "creator"]:
                        unsubbed_tasks.append(ch_db_id)
                except Exception:
                    pass
        if unsubbed_tasks:
            for ch_db_id in unsubbed_tasks:
                completed.remove(ch_db_id)
            penalty_total = len(unsubbed_tasks) * UNSUB_PENALTY
            await db.execute(
                "UPDATE users SET balance = balance - ?, completed_tasks = ? WHERE user_id = ?",
                (penalty_total, ",".join(completed), user_id),
            )
            await db.commit()
            try:
                await bot.send_message(
                    user_id,
                    f"⚠️ Отписка от каналов \\({escape_md(str(len(unsubbed_tasks)))}\\)\\.\n"
                    f"Штраф: *\\-`{escape_md(f'{penalty_total:.2f}')}` ⭐*",
                )
            except Exception:
                pass


async def send_next_task(event, user_id: int):
    await check_unsubscriptions(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT completed_tasks FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            completed = row[0].split(",") if row and row[0] else []
        async with db.execute("SELECT id, channel_id, title, link FROM channels") as cursor:
            channels = await cursor.fetchall()

    target_channel = None
    for ch in channels:
        ch_db_id, ch_id, title, link = ch
        if str(ch_db_id) in completed:
            continue
        if not await is_admin(user_id):
            try:
                member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
                if member.status in ["member", "administrator", "creator"]:
                    completed.append(str(ch_db_id))
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE users SET completed_tasks = ? WHERE user_id = ?",
                            (",".join(completed), user_id),
                        )
                        await db.commit()
                    await maybe_pay_referrer(user_id)
                    continue
            except Exception:
                pass
        target_channel = ch
        break

    if not target_channel:
        text = "😔 Заданий сейчас нет, загляни позже\\."
        if isinstance(event, types.CallbackQuery):
            try:
                await event.message.delete()
            except Exception:
                pass
            await event.message.answer(text)
        else:
            await event.answer(text)
        return

    ch_db_id, ch_id, title, link = target_channel
    builder = InlineKeyboardBuilder()
    builder.button(text="Перейти", url=link)
    builder.button(text="Проверить", callback_data=f"check_sub:{ch_db_id}:{ch_id}")
    builder.button(text="Пропустить", callback_data=f"skip_task:{ch_db_id}")
    builder.adjust(2, 1)
    title_esc = escape_md(title)
    reward_esc = escape_md(str(REWARD_PER_SUB))
    task_text = (
        "💡 *Задание*\n\n"
        f"Подпишись на *{title_esc}* и жми «Проверить»\\.\n\n"
        f"Награда: *\\+{reward_esc}* 💫"
    )
    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.delete()
        except Exception:
            pass
        await event.message.answer(task_text, reply_markup=builder.as_markup())
    else:
        await event.answer(task_text, reply_markup=builder.as_markup())


async def earn_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await send_next_task(message, message.from_user.id)


@dp.callback_query(F.data.startswith("check_sub:"))
async def check_sub_handler(callback: types.CallbackQuery):
    if not await user_can_use_bot(callback.from_user.id, callback):
        return
    _, ch_db_id, ch_id = callback.data.split(":")
    user_id = callback.from_user.id
    try:
        member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT completed_tasks, COALESCE(balance, 0.0) FROM users WHERE user_id = ?",
                    (user_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                    completed = row[0].split(",") if row and row[0] else []
                    balance = float(row[1]) if row else 0.0
                if str(ch_db_id) in completed:
                    await callback.answer("Это задание уже закрыто", show_alert=True)
                    return
                completed.append(str(ch_db_id))
                new_balance = balance + REWARD_PER_SUB
                await db.execute(
                    "UPDATE users SET balance = ?, completed_tasks = ? WHERE user_id = ?",
                    (new_balance, ",".join(completed), user_id),
                )
                await log_balance_change(db, user_id, REWARD_PER_SUB, "task")
                await db.commit()
            await maybe_pay_referrer(user_id)
            await callback.answer("Подписка ок, звёзды начислены", show_alert=True)
            await send_next_task(callback, user_id)
        else:
            await callback.answer("Ещё не подписался", show_alert=True)
    except Exception:
        await callback.answer("Не смог проверить. Бот должен быть админом канала.", show_alert=True)


@dp.callback_query(F.data.startswith("skip_task:"))
async def skip_task_handler(callback: types.CallbackQuery):
    if not await user_can_use_bot(callback.from_user.id, callback):
        return
    await callback.answer("Ок, следующее")
    await send_next_task(callback, callback.from_user.id)


async def build_profile_text(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (user_id,)) as cursor:
            balance = float((await cursor.fetchone())[0])
        async with db.execute(
            "SELECT SUM(amount) FROM withdraws WHERE user_id = ? AND status = 'pending'", (user_id,)
        ) as cursor:
            row_pending = await cursor.fetchone()
            pending = float(row_pending[0]) if row_pending and row_pending[0] is not None else 0.0
        async with db.execute(
            "SELECT SUM(amount) FROM withdraws WHERE user_id = ? AND status = 'completed'", (user_id,)
        ) as cursor:
            row_completed = await cursor.fetchone()
            completed = float(row_completed[0]) if row_completed and row_completed[0] is not None else 0.0
    return (
        "👤 *Профиль*\n\n"
        f"Баланс: `{escape_md(f'{balance:.2f}')}` ⭐\n"
        f"Ждёт вывода: `{escape_md(f'{pending:.2f}')}` ⭐\n"
        f"Уже вывел: `{escape_md(f'{completed:.2f}')}` ⭐"
    )


async def profile_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(await build_profile_text(message.from_user.id), reply_markup=await profile_keyboard())


@dp.callback_query(F.data == "profile_back")
async def profile_back_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = await build_profile_text(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=await profile_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=await profile_keyboard())
    await callback.answer()


async def build_top_text(period: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        if period == "all":
            async with db.execute(
                "SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT ?",
                (TOP_USERS_LIMIT,),
            ) as cursor:
                top_rows = await cursor.fetchall()
        else:
            since = datetime.now() - (timedelta(days=1) if period == "day" else timedelta(days=7))
            async with db.execute(
                """
                SELECT bl.user_id, u.username, SUM(bl.amount) as total
                FROM balance_log bl
                LEFT JOIN users u ON bl.user_id = u.user_id
                WHERE bl.created_at >= ?
                GROUP BY bl.user_id
                ORDER BY total DESC
                LIMIT ?
                """,
                (since.isoformat(), TOP_USERS_LIMIT),
            ) as cursor:
                top_rows = await cursor.fetchall()
    period_labels = {"day": "за день", "week": "за неделю", "all": "за всё время"}
    header = f"🏆 *Топ {period_labels[period]}*\n\n"
    if not top_rows:
        return header + "Пока тихо\\."
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (u_id, u_name, val) in enumerate(top_rows, start=1):
        display_name = escape_md(u_name) if u_name else f"ID{u_id}"
        prefix = medals[idx - 1] if idx <= 3 else f"{idx}\\."
        lines.append(f"{prefix} {display_name} — {escape_md(f'{val:.1f}')} ⭐")
    return header + quote_block("\n".join(lines))


@dp.callback_query(F.data.startswith("show_top:"))
async def show_top_handler(callback: types.CallbackQuery):
    if not await is_top_enabled():
        await callback.answer("Топ сейчас выключен", show_alert=True)
        return
    period = callback.data.split(":")[1]
    text = await build_top_text(period)
    try:
        await callback.message.edit_text(text, reply_markup=top_period_keyboard(period))
    except Exception:
        await callback.message.answer(text, reply_markup=top_period_keyboard(period))
    await callback.answer()


@dp.callback_query(F.data == "activate_promo")
async def promo_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("🎟 Кинь промокод:")
    await state.set_state(UserStates.waiting_for_promo)
    await callback.answer()


@dp.message(UserStates.waiting_for_promo)
async def promo_process(message: types.Message, state: FSMContext):
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    code = (message.text or "").strip().upper()
    if not code:
        await message.answer("❌ Напиши промокод\\.")
        return
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT amount, max_activations, used_activations FROM promocodes WHERE code = ?", (code,)
        ) as cursor:
            promo_row = await cursor.fetchone()
        if not promo_row:
            await message.answer("❌ Такого промокода нет\\.")
            await state.clear()
            return
        amount, max_activations, used_activations = promo_row
        amount = float(amount)
        async with db.execute(
            "SELECT 1 FROM promo_activations WHERE promo_code = ? AND user_id = ?", (code, user_id)
        ) as cursor:
            already_used = await cursor.fetchone() is not None
        if already_used:
            await message.answer("❌ Этот промокод ты уже вводил\\.")
            await state.clear()
            return
        if max_activations is not None and used_activations >= max_activations:
            await message.answer("❌ Лимит активаций кончился\\.")
            await state.clear()
            return
        await db.execute("INSERT INTO users (user_id, balance) VALUES (?, 0.0) ON CONFLICT(user_id) DO NOTHING", (user_id,))
        await db.commit()
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.execute("INSERT INTO promo_activations (promo_code, user_id) VALUES (?, ?)", (code, user_id))
        await db.execute("UPDATE promocodes SET used_activations = used_activations + 1 WHERE code = ?", (code,))
        await log_balance_change(db, user_id, amount, "promo")
        await db.commit()
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            new_balance = (await cursor.fetchone())[0]
    amount_val = int(amount) if amount.is_integer() else amount
    await message.answer(
        f"🎉 Промокод засчитан, \\+{escape_md(str(amount_val))} ⭐\nБаланс: `{escape_md(f'{new_balance:.2f}')}` ⭐"
    )
    await state.clear()


async def withdraw_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (message.from_user.id,)
        ) as cursor:
            balance = float((await cursor.fetchone())[0])
    text = (
        "💸 *Вывод*\n\n"
        f"Баланс: `{escape_md(f'{balance:.2f}')}` ⭐\n\n"
        "Выбери подарок:"
    )
    await message.answer(text, reply_markup=withdraw_keyboard())


@dp.callback_query(F.data.startswith("wdg:"))
async def process_withdraw(callback: types.CallbackQuery, state: FSMContext):
    if not await user_can_use_bot(callback.from_user.id, callback):
        return
    idx = int(callback.data.split(":")[1])
    if idx < 0 or idx >= len(GIFTS):
        await callback.answer("Подарок не найден", show_alert=True)
        return
    emoji, amount = GIFTS[idx]
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (user_id,)) as cursor:
            balance = float((await cursor.fetchone())[0])
    if balance < amount:
        await callback.answer(f"Не хватает. Нужно {amount} ⭐", show_alert=True)
        return
    await state.update_data(pending_amount=amount, pending_emoji=emoji)
    text = (
        "✍️ *Добавить надпись на подарок?*\n\n"
        + quote_block(f"Это плюс {escape_md(str(GIFT_TEXT_PRICE))} ⭐ к сумме заявки\\.")
    )
    await callback.message.edit_text(text, reply_markup=gift_text_ask_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "gift_ask:yes")
async def gift_ask_yes_handler(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "✍️ Напиши текст для подарка\\.\n\n"
        f"Максимум {GIFT_TEXT_MAX_LEN} символов\\."
    )
    await callback.message.edit_text(text, reply_markup=gift_text_cancel_keyboard())
    await state.set_state(UserStates.waiting_for_gift_text)
    await callback.answer()


@dp.callback_query(F.data == "gift_ask_back")
async def gift_ask_back_handler(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(None)
    await state.set_data(data)
    text = (
        "✍️ *Добавить надпись на подарок?*\n\n"
        + quote_block(f"Это плюс {escape_md(str(GIFT_TEXT_PRICE))} ⭐ к сумме заявки\\.")
    )
    await callback.message.edit_text(text, reply_markup=gift_text_ask_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "gift_ask:no")
async def gift_ask_no_handler(callback: types.CallbackQuery, state: FSMContext):
    await finalize_withdraw(callback, state, None)


@dp.message(UserStates.waiting_for_gift_text)
async def gift_text_input_handler(message: types.Message, state: FSMContext):
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    gift_text = (message.text or "").strip()
    if not gift_text:
        await message.answer("❌ Пусто не пойдёт, напиши ещё раз:")
        return
    if len(gift_text) > GIFT_TEXT_MAX_LEN:
        gift_text = gift_text[:GIFT_TEXT_MAX_LEN]
    await finalize_withdraw(message, state, gift_text)


def bot_link_md() -> str:
    clean = BOT_USERNAME.replace("@", "")
    return f"[{escape_md(BOT_USERNAME)}]({escape_md('https://t.me/' + clean)})"


async def finalize_withdraw(event, state: FSMContext, gift_text: str | None):
    data = await state.get_data()
    amount = data.get("pending_amount")
    gift_emoji = data.get("pending_emoji") or "⭐"
    user_id = event.from_user.id
    if amount is None:
        if isinstance(event, types.CallbackQuery):
            await event.answer("Сессия слетела, собери заявку заново.", show_alert=True)
        else:
            await event.answer("❌ Сессия слетела, собери заявку заново.")
        await state.clear()
        return
    total_cost = amount + (GIFT_TEXT_PRICE if gift_text else 0)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (user_id,)) as cursor:
            balance = float((await cursor.fetchone())[0])
        if balance < total_cost:
            msg_text = f"❌ Не хватает с учётом надписи. Нужно: {total_cost} ⭐"
            if isinstance(event, types.CallbackQuery):
                await event.answer(msg_text, show_alert=True)
            else:
                await event.answer(msg_text)
            await state.clear()
            return
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total_cost, user_id))
        cursor = await db.execute(
            "INSERT INTO withdraws (user_id, amount, gift_text, gift_emoji) VALUES (?, ?, ?, ?)",
            (user_id, amount, gift_text, gift_emoji),
        )
        withdraw_id = cursor.lastrowid
        await db.commit()
    await state.clear()

    gift_text_line = f"\n✍️ Надпись: _{escape_md(gift_text)}_" if gift_text else ""
    result_text = (
        f"🎉 Заявка на `{int(amount)}` {gift_emoji} ушла админу\\.{gift_text_line}\n"
        "Жди подтверждения\\."
    )
    if isinstance(event, types.CallbackQuery):
        await event.answer("Заявка создана", show_alert=True)
        try:
            await event.message.edit_text(result_text)
        except Exception:
            await event.message.answer(result_text)
    else:
        await event.answer(result_text)

    username = event.from_user.username
    user_mention = f"@{username}" if username else f"ID: {user_id}"
    user_mention_esc = escape_md(user_mention)
    amount_esc = escape_md(str(int(amount)))

    request_log = (
        "🧾 *Новая заявка\\!*\n\n"
        f"👤 {user_mention_esc}\n"
        f"⏳ {amount_esc} {gift_emoji} ожидает подтверждения\n\n"
        f"{bot_link_md()}"
    )
    await send_log(request_log)

    admin_msg = (
        "💸 *Новая заявка на вывод*\n\n"
        f"🆔 Заявка: \\#{withdraw_id}\n"
        f"👤 {user_mention_esc}\n"
        f"💰 {amount_esc} {gift_emoji}"
        f"{gift_text_line}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выдано", callback_data=f"withdraw_approve:{withdraw_id}")
    builder.button(text="❌ Отклонить", callback_data=f"withdraw_reject:{withdraw_id}")
    builder.adjust(2)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cursor:
            admins = await cursor.fetchall()
    for adm in admins:
        try:
            await bot.send_message(adm[0], admin_msg, reply_markup=builder.as_markup())
        except Exception as e:
            logging.error(f"Ошибка отправки админу ({adm[0]}): {e}")


@dp.callback_query(F.data.startswith("withdraw_approve:"))
async def withdraw_approve_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    w_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT w.user_id, w.amount, w.status, u.username, w.gift_text, COALESCE(w.gift_emoji, '⭐') "
            "FROM withdraws w LEFT JOIN users u ON w.user_id = u.user_id WHERE w.id = ?",
            (w_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        u_id, amt, status, u_name, gift_text, gift_emoji = row
        if status != "pending":
            await callback.answer("Уже обработана", show_alert=True)
            return
        await db.execute("UPDATE withdraws SET status = 'completed' WHERE id = ?", (w_id,))
        await db.commit()
    try:
        await bot.send_message(
            u_id,
            f"✅ Заявка на {int(amt)} {gift_emoji} одобрена. Подарок отправлен.",
            parse_mode=None,
        )
    except Exception:
        pass
    user_mention = f"@{u_name}" if u_name else f"ID: {u_id}"
    user_mention_esc = escape_md(user_mention)
    amt_esc = escape_md(str(int(amt)))
    payout_log = (
        "✅ *Новая выплата\\!*\n\n"
        f"👤 {user_mention_esc}\n"
        f"✅ {amt_esc} {gift_emoji} успешно выведено\n\n"
        f"{bot_link_md()}"
    )
    await send_log(payout_log)
    gift_status_line = f"\n✍️ Надпись: _{escape_md(gift_text)}_" if gift_text else ""
    status_admin_msg = (
        "💸 *Заявка на вывод*\n\n"
        f"🆔 \\#{w_id}\n"
        f"👤 {user_mention_esc}\n"
        f"💰 *{amt_esc}* {gift_emoji}"
        f"{gift_status_line}\n\n"
        "✅ *Выдано*"
    )
    await callback.message.edit_text(status_admin_msg)
    await callback.answer("Ок, выдано", show_alert=True)


@dp.callback_query(F.data.startswith("withdraw_reject:"))
async def withdraw_reject_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    w_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT w.user_id, w.amount, w.status, u.username, w.gift_text, COALESCE(w.gift_emoji, '⭐') "
            "FROM withdraws w LEFT JOIN users u ON w.user_id = u.user_id WHERE w.id = ?",
            (w_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        u_id, amt, status, u_name, gift_text, gift_emoji = row
        if status != "pending":
            await callback.answer("Уже обработана", show_alert=True)
            return
        gift_refund = GIFT_TEXT_PRICE if gift_text else 0
        total_refund = amt + gift_refund
        await db.execute("UPDATE withdraws SET status = 'rejected' WHERE id = ?", (w_id,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_refund, u_id))
        await db.commit()
    try:
        await bot.send_message(
            u_id,
            f"❌ Ваша заявка на {int(amt)} ⭐ была отклонена. Звёзды возвращены на баланс.",
            parse_mode=None,
        )
    except Exception:
        pass
    user_mention = f"@{u_name}" if u_name else f"ID: {u_id}"
    user_mention_esc = escape_md(user_mention)
    amt_esc = escape_md(str(int(amt)))
    gift_status_line = f"\n✍️ Надпись: _{escape_md(gift_text)}_" if gift_text else ""
    status_admin_msg = (
        "💸 *Заявка на вывод*\n\n"
        f"🆔 \\#{w_id}\n"
        f"👤 {user_mention_esc}\n"
        f"💰 *{amt_esc}* {gift_emoji}"
        f"{gift_status_line}\n\n"
        "❌ *Отклонено*"
    )
    await callback.message.edit_text(status_admin_msg)
    await callback.answer("Отклонил, звёзды вернул", show_alert=True)


@dp.callback_query(F.data == "back_to_main")
async def back_to_main_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await show_start_screen(callback.message, callback.from_user)
    await callback.answer()


async def admin_panel(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    text, kb = await render_admin_home(message)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "admin_home")
async def admin_home_handler(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await state.clear()
    text, kb = await render_admin_home(callback)
    await edit_or_send(callback, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "admin_people")
async def admin_people_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "🔎 *Люди*\n\nБаланс, поиск, баны, админы — тут\\.", admin_people_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_content")
async def admin_content_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "📢 *Контент*\n\nКаналы, промо, чеки, баннер и рассылка\\.", admin_content_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_settings")
async def admin_settings_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "⚙️ *Настройки*\n\nРефка, клик, топы\\.", admin_settings_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_toggle_top")
async def admin_toggle_top_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    current = await is_top_enabled()
    await set_setting("top_enabled", "0" if current else "1")
    await callback.answer("Топы выключены" if current else "Топы включены", show_alert=True)


@dp.callback_query(F.data == "admin_stats")
async def admin_stats_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-1 day')") as c:
            new_users_24h = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-7 day')") as c:
            new_users_7d = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1") as c:
            banned_count = (await c.fetchone())[0]
        async with db.execute("SELECT SUM(balance) FROM users") as c:
            total_balance = (await c.fetchone())[0] or 0.0
        async with db.execute("SELECT COUNT(*) FROM withdraws WHERE status = 'pending'") as c:
            pending_withdraws = (await c.fetchone())[0]
        async with db.execute("SELECT SUM(amount) FROM withdraws WHERE status = 'completed'") as c:
            total_paid_out = (await c.fetchone())[0] or 0.0
        async with db.execute("SELECT COUNT(*) FROM channels") as c:
            total_channels = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM promocodes") as c:
            total_promos = (await c.fetchone())[0]
    stats_text = (
        "📊 *Сводка*\n\n"
        f"Всего людей: {total_users}\n"
        f"За сутки: {new_users_24h}\n"
        f"За неделю: {new_users_7d}\n"
        f"В бане: {banned_count}\n\n"
        f"На балансах: {total_balance:.2f} ⭐\n"
        f"Выплачено: {total_paid_out:.2f} ⭐\n"
        f"Заявок ждут: {pending_withdraws}\n\n"
        f"Каналов: {total_channels}\n"
        f"Промокодов: {total_promos}"
    )
    await edit_or_send(callback, escape_md_preserve_formatting(stats_text), back_admin_kb())
    await callback.answer()


@dp.callback_query(F.data == "admin_change_balance")
async def admin_change_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(
        callback,
        "💰 *Баланс*\n\nКинь ID или @username\\. Человек должен хотя бы раз жать /start\\.",
        back_admin_kb(),
    )
    await state.set_state(AdminStates.waiting_for_balance_user)
    await callback.answer()


@dp.message(AdminStates.waiting_for_balance_user)
async def admin_change_balance_user(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    text = (message.text or "").strip().lstrip("@")
    if not text:
        await message.answer("❌ Ещё раз ID или username:")
        return
    target_id = None
    username = None
    balance = 0.0
    async with aiosqlite.connect(DB_PATH) as db:
        if text.isdigit():
            async with db.execute("SELECT user_id, username, balance FROM users WHERE user_id = ?", (int(text),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id, username, balance = row
        else:
            async with db.execute(
                "SELECT user_id, username, balance FROM users WHERE LOWER(username) = LOWER(?)", (text,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id, username, balance = row
    if not target_id:
        await message.answer("❌ Нет такого в базе\\. Попробуй ещё раз:")
        return
    user_info = f"@{escape_md(username)}" if username else f"ID: `{target_id}`"
    msg_text = (
        "💰 *Баланс*\n\n"
        f"{user_info}\n"
        f"Сейчас: `{escape_md(f'{balance:.2f}')}` ⭐"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Плюс", callback_data=f"bal_act:add:{target_id}")
    builder.button(text="➖ Минус", callback_data=f"bal_act:sub:{target_id}")
    builder.button(text="✏️ Поставить", callback_data=f"bal_act:set:{target_id}")
    builder.button(text="‹ Назад в админку", callback_data="admin_home")
    builder.adjust(3, 1)
    await message.answer(msg_text, reply_markup=builder.as_markup())
    await state.clear()


@dp.callback_query(F.data.startswith("bal_act:"))
async def admin_balance_action(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    _, action, target_id = callback.data.split(":")
    await state.update_data(target_user_id=int(target_id), balance_action=action)
    prompts = {
        "add": "➕ Сколько накинуть?",
        "sub": "➖ Сколько снять?",
        "set": "✏️ Какой баланс поставить?",
    }
    await callback.message.answer(prompts[action])
    await state.set_state(AdminStates.waiting_for_balance_value)
    await callback.answer()


@dp.message(AdminStates.waiting_for_balance_value)
async def admin_change_balance_value_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        val = float(message.text.replace(",", "."))
        if val < 0 and (await state.get_data()).get("balance_action") != "set":
            val = abs(val)
    except ValueError:
        await message.answer("❌ Это не число.")
        return
    data = await state.get_data()
    target_id = data.get("target_user_id")
    action = data.get("balance_action")
    async with aiosqlite.connect(DB_PATH) as db:
        if action == "add":
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (val, target_id))
            await log_balance_change(db, target_id, val, "admin")
        elif action == "sub":
            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (val, target_id))
        elif action == "set":
            await db.execute("UPDATE users SET balance = ? WHERE user_id = ?", (val, target_id))
        await db.commit()
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (target_id,)) as c:
            new_bal = (await c.fetchone())[0]
    await message.answer(
        f"✅ Готово\\.\nID `{target_id}`\nБаланс: `{escape_md(f'{new_bal:.2f}')}` ⭐",
        reply_markup=back_admin_kb(),
    )
    await state.clear()


@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "🔎 Кинь ID или @username:", back_admin_kb())
    await state.set_state(AdminStates.waiting_for_find_user)
    await callback.answer()


@dp.message(AdminStates.waiting_for_find_user)
async def admin_find_user_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    text = (message.text or "").strip().lstrip("@")
    if not text:
        await message.answer("❌ Ещё раз ID или username:")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        if text.isdigit():
            query = "SELECT user_id, username, balance, is_banned, created_at, referrer_id FROM users WHERE user_id = ?"
            param = int(text)
        else:
            query = "SELECT user_id, username, balance, is_banned, created_at, referrer_id FROM users WHERE LOWER(username) = LOWER(?)"
            param = text
        async with db.execute(query, (param,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            await message.answer("❌ Нет в базе\\. Ещё раз:")
            return
        u_id, u_name, balance, banned, created_at, ref_id = row
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (u_id,)) as c:
            invited = (await c.fetchone())[0]
        async with db.execute("SELECT SUM(amount) FROM withdraws WHERE user_id = ? AND status='completed'", (u_id,)) as c:
            paid = (await c.fetchone())[0] or 0.0
        async with db.execute("SELECT SUM(amount) FROM withdraws WHERE user_id = ? AND status='pending'", (u_id,)) as c:
            pending = (await c.fetchone())[0] or 0.0
    is_adm = await is_admin(u_id)
    user_info = f"@{escape_md(u_name)}" if u_name else "без username"
    status_line = "бан" if banned else ("админ" if is_adm else "живой")
    card_text = (
        "🔎 *Карточка*\n\n"
        f"{user_info}\n"
        f"ID: {u_id}\n"
        f"Статус: {status_line}\n"
        f"Баланс: {balance:.2f} ⭐\n"
        f"Выведено: {paid:.2f} ⭐\n"
        f"В ожидании: {pending:.2f} ⭐\n"
        f"Рефов: {invited}\n"
        f"Регистрация: {created_at}"
    )
    await message.answer(escape_md_preserve_formatting(card_text), reply_markup=back_admin_kb())
    await state.clear()


@dp.callback_query(F.data.startswith("admin_pending_w_page:"))
async def admin_pending_withdraws_page(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    page = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT w.id, w.user_id, w.amount, u.username, w.gift_text, COALESCE(w.gift_emoji, '⭐') "
            "FROM withdraws w LEFT JOIN users u ON w.user_id = u.user_id "
            "WHERE w.status = 'pending' ORDER BY w.id ASC"
        ) as cursor:
            withdraws = await cursor.fetchall()
    if not withdraws:
        await edit_or_send(callback, "🎉 Очередь выводов пустая\\.", back_admin_kb())
        await callback.answer()
        return
    total_items = len(withdraws)
    total_pages = (total_items + WITHDRAWS_PER_PAGE - 1) // WITHDRAWS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start_idx = page * WITHDRAWS_PER_PAGE
    page_items = withdraws[start_idx:start_idx + WITHDRAWS_PER_PAGE]
    text_lines = [
        "⏳ *Заявки на вывод*",
        f"стр\\. *{page + 1}/{total_pages}* · всего *{total_items}*\n",
    ]
    for w_id, u_id, amt, u_name, gtext, gemoji in page_items:
        u_info = f"@{escape_md(u_name)}" if u_name else f"`{u_id}`"
        gift_line = f"\n✍️ _{escape_md(gtext)}_" if gtext else ""
        text_lines.append(f"\\#{w_id} · {u_info} · *{escape_md(str(int(amt)))}* {gemoji}{gift_line}")
    text = "\n".join(text_lines)
    builder = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"admin_pending_w_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton(text="➡️", callback_data=f"admin_pending_w_page:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.button(text="‹ Назад в админку", callback_data="admin_home")
    builder.adjust(2, 1)
    await edit_or_send(callback, text, builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "📣 Пришли текст или фото — разлетится всем\\.", back_admin_kb())
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()


@dp.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()
    await message.answer(f"🚀 Пошло\\.\nПолучателей: *{len(users)}*")
    success = 0
    failed = 0
    for u in users:
        u_id = u[0]
        while True:
            try:
                if message.photo:
                    await bot.send_photo(chat_id=u_id, photo=message.photo[-1].file_id, caption=escape_md(message.caption or ""))
                else:
                    await bot.send_message(chat_id=u_id, text=escape_md(message.text))
                success += 1
                await asyncio.sleep(0.05)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except (TelegramForbiddenError, Exception):
                failed += 1
                break
    await message.answer(
        f"✅ Рассылка села\\.\n🟢 {success}\n🔴 {failed}",
        reply_markup=back_admin_kb(),
    )
    await state.clear()


@dp.callback_query(F.data == "admin_set_ref_reward")
async def admin_set_ref_reward_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    cur_rew = await get_ref_reward()
    text = (
        "👥 *Рефка*\n\n"
        f"Сейчас: *{escape_md(str(cur_rew))}* ⭐ за друга \\(после подписки и 1 задания\\)\\.\n\n"
        "Напиши новое число:"
    )
    await edit_or_send(callback, text, back_admin_kb())
    await state.set_state(AdminStates.waiting_for_ref_reward)
    await callback.answer()


@dp.message(AdminStates.waiting_for_ref_reward)
async def admin_set_ref_reward_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        val = float(message.text.replace(",", "."))
        if val < 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Нужно число.")
        return
    await set_setting("ref_reward", str(val))
    await message.answer(f"✅ Теперь за друга `{escape_md(str(val))}` ⭐", reply_markup=back_admin_kb())
    await state.clear()


@dp.callback_query(F.data == "admin_click_menu")
async def admin_click_menu(callback: types.CallbackQuery, state: FSMContext | None = None):
    if not await is_admin(callback.from_user.id):
        return
    reward = await get_click_reward()
    cooldown = await get_click_cooldown_min()
    enabled = await is_click_enabled()
    status = "вкл" if enabled else "выкл"
    text = (
        "🖱 *Клик*\n\n"
        f"Награда: *{escape_md(f'{reward:.2f}')}* ⭐\n"
        f"Кулдаун: *{escape_md(str(cooldown))}* мин\n"
        f"Кнопка: *{escape_md(status)}*"
    )
    await edit_or_send(callback, text, click_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "click_toggle")
async def click_toggle_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    current = await is_click_enabled()
    await set_setting("click_enabled", "0" if current else "1")
    await admin_click_menu(callback)


@dp.callback_query(F.data == "click_set_reward")
async def click_set_reward_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await callback.message.answer("💰 Новая награда за клик \\(можно дробь, типа 0\\.15\\):")
    await state.set_state(AdminStates.waiting_for_click_reward)
    await callback.answer()


@dp.message(AdminStates.waiting_for_click_reward)
async def click_set_reward_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        val = float(message.text.replace(",", "."))
        if val <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Нужно положительное число.")
        return
    await set_setting("click_reward", str(val))
    await state.clear()
    await message.answer(f"✅ Клик теперь `{escape_md(str(val))}` ⭐")


@dp.callback_query(F.data == "click_set_cooldown")
async def click_set_cooldown_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await callback.message.answer("⏱ Кулдаун в минутах:")
    await state.set_state(AdminStates.waiting_for_click_cooldown)
    await callback.answer()


@dp.message(AdminStates.waiting_for_click_cooldown)
async def click_set_cooldown_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        val = int(message.text.strip())
        if val <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Целое число минут.")
        return
    await set_setting("click_cooldown_min", str(val))
    await state.clear()
    await message.answer(f"✅ Кулдаун `{val}` мин\\.")


@dp.callback_query(F.data == "admin_ban_user")
async def admin_ban_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "🚫 Кого баним? ID или @username:", back_admin_kb())
    await state.set_state(AdminStates.waiting_for_ban_user)
    await callback.answer()


@dp.message(AdminStates.waiting_for_ban_user)
async def admin_ban_user_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    text = (message.text or "").strip().lstrip("@")
    target_id = None
    async with aiosqlite.connect(DB_PATH) as db:
        if text.isdigit():
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (int(text),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id = row[0]
        else:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (text,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id = row[0]
        if not target_id:
            await message.answer("❌ Нет такого\\. Ещё раз:")
            return
        if target_id == PRIMARY_ADMIN_ID:
            await message.answer("❌ Главного не баним\\.")
            await state.clear()
            return
        await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,))
        await db.commit()
    await message.answer(f"✅ `{target_id}` в бане\\.", reply_markup=back_admin_kb())
    await state.clear()


@dp.callback_query(F.data == "admin_unban_user")
async def admin_unban_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "✅ Кого достаём из бана?", back_admin_kb())
    await state.set_state(AdminStates.waiting_for_unban_user)
    await callback.answer()


@dp.message(AdminStates.waiting_for_unban_user)
async def admin_unban_user_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    text = (message.text or "").strip().lstrip("@")
    target_id = None
    async with aiosqlite.connect(DB_PATH) as db:
        if text.isdigit():
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (int(text),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id = row[0]
        else:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (text,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id = row[0]
        if not target_id:
            await message.answer("❌ Нет такого\\. Ещё раз:")
            return
        await db.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,))
        await db.commit()
    await message.answer(f"✅ `{target_id}` разбанен\\.", reply_markup=back_admin_kb())
    await state.clear()


@dp.callback_query(F.data == "admin_add_admin")
async def admin_add_admin_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "👤 Кому выдать админку? @username или ID:", back_admin_kb())
    await state.set_state(AdminStates.waiting_for_new_admin)
    await callback.answer()


@dp.message(AdminStates.waiting_for_new_admin)
async def admin_add_admin_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    text = message.text.strip().replace("@", "")
    target_id = None
    if text.isdigit():
        target_id = int(text)
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (text,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id = row[0]
    if not target_id:
        await message.answer("❌ Человек должен хотя бы раз написать боту.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (target_id,))
        await db.commit()
    await message.answer(f"✅ `{target_id}` теперь админ\\.", reply_markup=back_admin_kb())
    await state.clear()


@dp.callback_query(F.data == "admin_list_admins")
async def admin_list_admins(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT a.user_id, u.username FROM admins a LEFT JOIN users u ON a.user_id = u.user_id"
        ) as cursor:
            admins = await cursor.fetchall()
    if not admins:
        await edit_or_send(callback, "Список админов пуст\\.", back_admin_kb())
        await callback.answer()
        return
    text = "👥 *Админы*\n\n"
    builder = InlineKeyboardBuilder()
    for adm_id, username in admins:
        u_str = f"@{escape_md(username)}" if username else "без ника"
        if adm_id == PRIMARY_ADMIN_ID:
            text += f"👑 {u_str} \\(`{adm_id}`\\) — главный\n"
        else:
            text += f"• {u_str} \\(`{adm_id}`\\)\n"
            builder.button(text=f"Снять {adm_id}", callback_data=f"remove_admin:{adm_id}")
    builder.button(text="‹ Назад в админку", callback_data="admin_home")
    builder.adjust(1)
    await edit_or_send(callback, text, builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("remove_admin:"))
async def remove_admin_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    target_id = int(callback.data.split(":")[1])
    if target_id == PRIMARY_ADMIN_ID:
        await callback.answer("Главного не снимаем", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (target_id,))
        await db.commit()
    await callback.answer("Снял")
    await admin_list_admins(callback)


@dp.callback_query(F.data == "admin_top_users")
async def admin_top_users(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT 10") as cursor:
            top_users = await cursor.fetchall()
    if not top_users:
        await edit_or_send(callback, "Пока никого\\.", back_admin_kb())
        await callback.answer()
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (u_id, u_name, bal) in enumerate(top_users, start=1):
        username_str = f"@{escape_md(u_name)}" if u_name else "без ника"
        prefix = medals[idx - 1] if idx <= 3 else f"{idx}\\."
        lines.append(f"{prefix} {username_str} `{u_id}` — *{escape_md(f'{bal:.2f}')}* ⭐")
    text = "🏆 *Топ по балансу*\n\n" + "\n".join(lines)
    await edit_or_send(callback, text, back_admin_kb())
    await callback.answer()


@dp.callback_query(F.data == "admin_promo_menu")
async def admin_promo_menu(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await state.clear()
    await edit_or_send(callback, "🎟 *Промокоды*", promo_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "promo_create_start")
async def promo_create_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await callback.message.answer("🎟 Название промокода, без пробелов:")
    await state.set_state(AdminStates.waiting_for_promo_code)
    await callback.answer()


@dp.message(AdminStates.waiting_for_promo_code)
async def promo_create_code_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    code = message.text.strip().upper()
    if not code or " " in code:
        await message.answer("❌ Без пробелов. Ещё раз:")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM promocodes WHERE code = ?", (code,)) as cursor:
            exists = await cursor.fetchone() is not None
    if exists:
        await message.answer("❌ Такой уже есть. Другое имя:")
        return
    await state.update_data(new_promo_code=code)
    await message.answer("💰 Сколько звёзд за активацию?")
    await state.set_state(AdminStates.waiting_for_promo_amount)


@dp.message(AdminStates.waiting_for_promo_amount)
async def promo_create_amount_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Нужно число.")
        return
    await state.update_data(new_promo_amount=amount)
    await message.answer("🔢 Сколько активаций? `0` — без лимита\\.")
    await state.set_state(AdminStates.waiting_for_promo_limit)


@dp.message(AdminStates.waiting_for_promo_limit)
async def promo_create_limit_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        limit = int(message.text.strip())
        if limit < 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Целое число, 0 или больше.")
        return
    data = await state.get_data()
    code = data.get("new_promo_code")
    amount = data.get("new_promo_amount")
    max_activations = None if limit == 0 else limit
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO promocodes (code, amount, max_activations) VALUES (?, ?, ?)",
            (code, amount, max_activations),
        )
        await db.commit()
    limit_text = "без лимита" if max_activations is None else str(max_activations)
    await message.answer(
        "✅ Промокод живой\\.\n"
        f"`{escape_md(code)}` · *{escape_md(str(amount))}* ⭐ · {escape_md(limit_text)}",
        reply_markup=back_admin_kb([("🎟 К промо", "admin_promo_menu")]),
    )
    await state.clear()


@dp.callback_query(F.data == "promo_list")
async def promo_list_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT code, amount, max_activations, used_activations FROM promocodes ORDER BY created_at DESC"
        ) as cursor:
            promos = await cursor.fetchall()
    builder = InlineKeyboardBuilder()
    if not promos:
        builder.button(text="‹ Назад", callback_data="admin_promo_menu")
        await edit_or_send(callback, "📋 Промокодов нет\\.", builder.as_markup())
        await callback.answer()
        return
    text_lines = ["🎟 *Промокоды*\n"]
    for code, amount, max_act, used_act in promos:
        limit_text = "∞" if max_act is None else f"{used_act}/{max_act}"
        text_lines.append(f"`{escape_md(code)}` — *{escape_md(str(amount))}* ⭐ \\({escape_md(limit_text)}\\)")
        builder.button(text=f"Удалить {code}", callback_data=f"promo_delete:{code}")
    builder.button(text="‹ Назад", callback_data="admin_promo_menu")
    builder.adjust(1)
    await edit_or_send(callback, "\n".join(text_lines), builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("promo_delete:"))
async def promo_delete_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    code = callback.data.split(":", 1)[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM promocodes WHERE code = ?", (code,))
        await db.execute("DELETE FROM promo_activations WHERE promo_code = ?", (code,))
        await db.commit()
    await callback.answer(f"{code} удалён")
    await promo_list_handler(callback)


@dp.callback_query(F.data == "admin_create_check")
async def admin_create_check_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "🧸 Сколько звёзд в чеке?", back_admin_kb())
    await state.set_state(AdminStates.waiting_for_check_amount)
    await callback.answer()


@dp.message(AdminStates.waiting_for_check_amount)
async def admin_create_check_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Нужно число.")
        return
    check_code = secrets.token_hex(4)
    creator_id = message.from_user.id
    welcome_photo = None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'welcome_photo'") as cursor:
            row = await cursor.fetchone()
            if row:
                welcome_photo = row[0]
    bot_info = await bot.get_me()
    check_link = f"https://t.me/{bot_info.username}?start=check_{check_code}"
    amount_val = int(amount) if amount.is_integer() else amount
    caption_text = f"🧸 Чек на `{escape_md(str(amount_val))}` ⭐"
    builder = InlineKeyboardBuilder()
    builder.button(text="Забрать", url=check_link)
    sent_msg = None
    if welcome_photo:
        try:
            sent_msg = await message.answer_photo(photo=welcome_photo, caption=caption_text, reply_markup=builder.as_markup())
        except Exception:
            sent_msg = await message.answer(caption_text, reply_markup=builder.as_markup())
    else:
        sent_msg = await message.answer(caption_text, reply_markup=builder.as_markup())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO checks (code, creator_id, amount, chat_id, msg_id) VALUES (?, ?, ?, ?, ?)",
            (check_code, creator_id, amount, sent_msg.chat.id if sent_msg else None, sent_msg.message_id if sent_msg else None),
        )
        await db.commit()
    await state.clear()


@dp.callback_query(F.data == "admin_set_photo")
async def admin_set_photo_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await edit_or_send(callback, "📸 Кинь картинку для баннера и чеков\\.", back_admin_kb())
    await state.set_state(AdminStates.waiting_for_welcome_photo)
    await callback.answer()


@dp.message(AdminStates.waiting_for_welcome_photo, F.photo)
async def admin_set_photo_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    photo_id = message.photo[-1].file_id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('welcome_photo', ?)", (photo_id,))
        await db.commit()
    await message.answer("✅ Баннер обновил\\.", reply_markup=back_admin_kb())
    await state.clear()


@dp.callback_query(F.data == "admin_add_channel")
async def add_channel_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    text = (
        "➕ *Канал*\n\n"
        "Юзернейм вроде `@mychannel` или ID `-100…`\\.\n"
        "Бот уже должен быть админом там\\."
    )
    await edit_or_send(callback, text, back_admin_kb())
    await state.set_state(AdminStates.waiting_for_channel)
    await callback.answer()


@dp.message(AdminStates.waiting_for_channel)
async def add_channel_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    input_text = message.text.strip()
    if input_text.startswith("-100") or input_text.lstrip("-").isdigit():
        chat_identifier = int(input_text)
    else:
        chat_identifier = input_text if input_text.startswith("@") else f"@{input_text}"
    try:
        chat = await bot.get_chat(chat_identifier)
        ch_id = str(chat.id)
        title = chat.title
        if chat.username:
            link = f"https://t.me/{chat.username}"
        else:
            link = f"https://t.me/c/{str(chat.id).replace('-100', '')}/1"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO channels (channel_id, title, link) VALUES (?, ?, ?)", (ch_id, title, link))
            await db.commit()
        await message.answer(
            f"✅ Добавил *{escape_md(title)}*\n{escape_md(link)}",
            reply_markup=back_admin_kb([("📋 К каналам", "admin_list_channels")]),
        )
        await state.clear()
    except Exception as e:
        await message.answer(
            f"❌ Не вышло\\.\n`{escape_md(str(e))}`\nПроверь, что бот админ и юзернейм верный\\."
        )


@dp.callback_query(F.data == "admin_list_channels")
async def list_channels(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, title FROM channels") as cursor:
            channels = await cursor.fetchall()
    builder = InlineKeyboardBuilder()
    if not channels:
        builder.button(text="‹ Назад в админку", callback_data="admin_home")
        await edit_or_send(callback, "📋 Каналов нет\\.", builder.as_markup())
        await callback.answer()
        return
    text = f"📋 *Каналы* · {len(channels)}\n\n"
    for ch in channels:
        text += f"• \\#{ch[0]} — *{escape_md(ch[1])}*\n"
        builder.button(text=f"Удалить #{ch[0]}", callback_data=f"del_ch:{ch[0]}")
    builder.button(text="‹ Назад в админку", callback_data="admin_home")
    builder.adjust(2)
    await edit_or_send(callback, text, builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("del_ch:"))
async def delete_channel(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    ch_db_id = callback.data.split(":")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channels WHERE id = ?", (ch_db_id,))
        await db.commit()
    await callback.answer("Удалил")
    await list_channels(callback)


async def send_farm_ping():
    bot_info = await bot.get_me()
    start_url = f"https://t.me/{bot_info.username}?start=farm"
    text = (
        "🎉 *Приглашай друзей — зарабатывай звёзды\\!*\n\n"
        "Каждый приглашённый друг — это звёзды на твой баланс\\.\n"
        "Отправь свою ссылку в чаты и получай награду за каждого\\."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Начать фармить", url=start_url)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE COALESCE(is_banned, 0) = 0") as cursor:
            users = await cursor.fetchall()
    for (u_id,) in users:
        try:
            await bot.send_message(u_id, text, reply_markup=builder.as_markup())
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(u_id, text, reply_markup=builder.as_markup())
            except Exception:
                pass
        except Exception:
            pass


async def daily_farm_loop():
    while True:
        try:
            last = await get_setting("last_farm_ping", "")
            now = datetime.now()
            if not last:
                await set_setting("last_farm_ping", now.isoformat())
            else:
                try:
                    should = now - datetime.fromisoformat(last) >= timedelta(hours=24)
                except ValueError:
                    should = True
                if should:
                    logging.info("Ежедневный пинг: старт рассылки")
                    await send_farm_ping()
                    await set_setting("last_farm_ping", now.isoformat())
        except Exception as e:
            logging.error(f"Ошибка ежедневного пинга: {e}")
        await asyncio.sleep(30 * 60)


async def main():
    logging.info(f"База: {DB_PATH}")
    await init_db()
    asyncio.create_task(backup_db_loop())
    asyncio.create_task(daily_farm_loop())
    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logging.error(f"Polling упал, рестарт через 5 сек: {e}")
            await asyncio.sleep(5)
            continue
        else:
            break


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
