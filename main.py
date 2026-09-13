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
MIN_WITHDRAW = 15.0

DB_PATH = os.getenv("DB_PATH", "/app/data/bot_database.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

WITHDRAWS_PER_PAGE = 10
TOP_USERS_LIMIT = 10
ADMIN_BUTTONS_PER_PAGE = 6

DAILY_BONUS_MIN = 0.1
DAILY_BONUS_MAX = 1.0
DAILY_BONUS_STEP = 0.1

GIFT_TEXT_PRICE = 0.5
GIFT_TEXT_MAX_LEN = 100

CLICK_REWARD_DEFAULT = 0.1
CLICK_COOLDOWN_MIN_DEFAULT = 10

CAPTCHA_FRUITS = [
    ("🥭", "манго"),
    ("🍎", "яблоко"),
    ("🍋", "лимон"),
    ("🍑", "персик"),
    ("🍇", "виноград"),
    ("🍓", "клубнику")
]

MENU_BUTTONS = [
    "💎 Задания", "🎁 Вывести Звёзды", "📅 Ежедневный бонус",
    "👤 Профиль", "👥 Друзья", "👑 Админ-панель", "🖱 Клик"
]

def escape_md(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'([_ *\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

def quote_block(text: str) -> str:
    lines = text.split("\n")
    return "\n".join(f">{line}" if line else ">" for line in lines)

def random_daily_bonus() -> float:
    steps = round((DAILY_BONUS_MAX - DAILY_BONUS_MIN) / DAILY_BONUS_STEP)
    chosen_step = random.randint(0, steps)
    return round(DAILY_BONUS_MIN + chosen_step * DAILY_BONUS_STEP, 1)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2)
)
dp = Dispatcher(storage=MemoryStorage())

async def send_log(text: str, disable_preview: bool = False):
    if LOG_CHANNEL_ID:
        try:
            await bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=disable_preview
            )
        except Exception as e:
            logging.error(f"Ошибка отправки лога в канал: {e}")

async def log_balance_change(db: aiosqlite.Connection, user_id: int, amount: float, source: str):
    if amount <= 0:
        return
    await db.execute(
        "INSERT INTO balance_log (user_id, amount, source, created_at) VALUES (?, ?, ?, ?)",
        (user_id, amount, source, datetime.now().isoformat())
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
    val = await get_setting("top_enabled", "1")
    return val == "1"

async def is_click_enabled() -> bool:
    val = await get_setting("click_enabled", "1")
    return val == "1"

async def get_click_reward() -> float:
    return float(await get_setting("click_reward", str(CLICK_REWARD_DEFAULT)))

async def get_click_cooldown_min() -> int:
    return int(float(await get_setting("click_cooldown_min", str(CLICK_COOLDOWN_MIN_DEFAULT))))

async def is_maintenance_mode() -> bool:
    val = await get_setting("maintenance_mode", "0")
    return val == "1"

async def backup_db_loop():
    backup_dir = Path(DB_PATH).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"bot_database_{timestamp}.db"
            shutil.copy(DB_PATH, backup_path)
            logging.info(f"Бэкап базы создан: {backup_path}")

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
                captcha_date TEXT DEFAULT NULL,
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
            "captcha_date TEXT DEFAULT NULL"
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (PRIMARY_ADMIN_ID,))

        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT,
                title TEXT,
                link TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ref_reward', '5.0')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('top_enabled', '1')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('click_enabled', '1')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('click_reward', ?)", (str(CLICK_REWARD_DEFAULT),))
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('click_cooldown_min', ?)", (str(CLICK_COOLDOWN_MIN_DEFAULT),))
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_mode', '0')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sponsor_1', '@StarPays_Reviews')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sponsor_2', '@StarPay_s')")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdraws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            await db.execute("ALTER TABLE withdraws ADD COLUMN gift_text TEXT DEFAULT NULL")
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

        await db.commit()

async def is_admin(user_id: int) -> bool:
    if user_id == PRIMARY_ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def is_user_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row and row[0])

async def get_ref_reward() -> float:
    return float(await get_setting("ref_reward", "5.0"))

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
    waiting_for_min_withdraw = State()
    waiting_for_find_user = State()
    waiting_for_admin_remove = State()
    waiting_for_sponsor_1 = State()
    waiting_for_sponsor_2 = State()

class UserStates(StatesGroup):
    waiting_for_promo = State()
    waiting_for_gift_text = State()

async def main_keyboard(user_id: int):
    builder = ReplyKeyboardBuilder()
    builder.button(text="💎 Задания")
    builder.button(text="👥 Друзья")
    builder.button(text="🖱 Клик")
    builder.button(text="🎁 Вывести Звёзды")
    builder.button(text="📅 Ежедневный бонус")
    builder.button(text="👤 Профиль")
    if await is_admin(user_id):
        builder.button(text="👑 Админ-панель")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)

ADMIN_MENU_ITEMS = [
    ("📊 Статистика", "admin_stats"),
    ("🏆 Топ пользователей", "admin_top_users"),
    ("💰 Баланс пользователя", "admin_change_balance"),
    ("🔎 Найти пользователя", "admin_find_user"),
    ("⏳ Заявки на вывод", "admin_pending_w_page:0"),
    ("🎟 Промокоды", "admin_promo_menu"),
    ("🎟 Создать чек", "admin_create_check"),
    ("➕ Добавить канал", "admin_add_channel"),
    ("📋 Список каналов", "admin_list_channels"),
    ("📢 Обязательные спонсоры", "admin_sponsors_menu"),
    ("📢 Рассылка", "admin_broadcast"),
    ("🖼 Изменить баннер", "admin_set_photo"),
    ("⚙️ Настройка рефки", "admin_set_ref_reward"),
    ("🖱 Настройка Клика", "admin_click_menu"),
    ("🏆 Топы: вкл/выкл", "admin_toggle_top"),
    ("🚫 Забанить пользователя", "admin_ban_user"),
    ("✅ Разбанить пользователя", "admin_unban_user"),
    ("➕ Добавить админа", "admin_add_admin"),
    ("👥 Список админов", "admin_list_admins"),
]

def admin_keyboard(page: int = 0):
    total = len(ADMIN_MENU_ITEMS)
    total_pages = (total + ADMIN_BUTTONS_PER_PAGE - 1) // ADMIN_BUTTONS_PER_PAGE
    page = max(0, min(page, total_pages - 1))

    start_idx = page * ADMIN_BUTTONS_PER_PAGE
    end_idx = start_idx + ADMIN_BUTTONS_PER_PAGE
    page_items = ADMIN_MENU_ITEMS[start_idx:end_idx]

    builder = InlineKeyboardBuilder()
    for label, cb in page_items:
        builder.button(text=label, callback_data=cb)
    builder.adjust(2)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"admin_page:{page - 1}"))
    nav_buttons.append(types.InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin_page_noop"))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton(text="➡️", callback_data=f"admin_page:{page + 1}"))
    builder.row(*nav_buttons)

    return builder.as_markup()

def click_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Награда за клик", callback_data="click_set_reward")
    builder.button(text="⏱ Кулдаун (мин)", callback_data="click_set_cooldown")
    builder.button(text="🔘 Вкл/Выкл кнопку", callback_data="click_toggle")
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    builder.adjust(1)
    return builder.as_markup()

async def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎟 Активировать промокод", callback_data="activate_promo")
    if await is_top_enabled():
        builder.button(text="🏆 Топ пользователей", callback_data="show_top:all:0")
    builder.button(text="← Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()

def withdraw_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🧸 15⭐", callback_data="withdraw_gift:15")
    builder.button(text="💖 15⭐", callback_data="withdraw_gift:15")
    builder.button(text="🎁 25⭐", callback_data="withdraw_gift:25")
    builder.button(text="🌹 25⭐", callback_data="withdraw_gift:25")
    builder.button(text="🍾 50⭐", callback_data="withdraw_gift:50")
    builder.button(text="💐 50⭐", callback_data="withdraw_gift:50")
    builder.button(text="🚀 50⭐", callback_data="withdraw_gift:50")
    builder.button(text="🎂 50⭐", callback_data="withdraw_gift:50")
    builder.button(text="🏆 100⭐", callback_data="withdraw_gift:100")
    builder.button(text="💍 100⭐", callback_data="withdraw_gift:100")
    builder.button(text="💎 100⭐", callback_data="withdraw_gift:100")
    builder.button(text="← Назад", callback_data="back_to_main")
    builder.adjust(2, 2, 2, 2, 2, 1)
    return builder.as_markup()

def gift_text_ask_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✨ Да, добавить (+{GIFT_TEXT_PRICE}⭐)", callback_data="gift_ask:yes")
    builder.button(text="➡️ Нет, пропустить", callback_data="gift_ask:no")
    builder.adjust(1)
    return builder.as_markup()

def gift_text_cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="← Отмена", callback_data="gift_ask_back")
    builder.adjust(1)
    return builder.as_markup()

def top_period_keyboard(active_period: str):
    labels = {"day": "За день", "week": "За неделю", "all": "За всё время"}
    builder = InlineKeyboardBuilder()
    for period, label in labels.items():
        text = f"· {label} ·" if period == active_period else label
        builder.button(text=text, callback_data=f"show_top:{period}:0")
    builder.button(text="← Назад", callback_data="profile_back")
    builder.adjust(2, 1, 1)
    return builder.as_markup()

def promo_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать промокод", callback_data="promo_create_start")
    builder.button(text="📋 Список промокодов", callback_data="promo_list")
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    builder.adjust(1)
    return builder.as_markup()

def generate_captcha_keyboard(correct_fruit_emoji: str):
    builder = InlineKeyboardBuilder()
    shuffled = CAPTCHA_FRUITS.copy()
    random.shuffle(shuffled)
    for emoji_char, _ in shuffled:
        builder.button(text=emoji_char, callback_data=f"captcha:{emoji_char}:{correct_fruit_emoji}")
    builder.adjust(3)
    return builder.as_markup()

async def send_captcha(message: types.Message):
    target_emoji, _ = random.choice(CAPTCHA_FRUITS)
    text = (
        "🤖 *ПРОВЕРКА НА РОБОТА*\n\n"
        f"Нажми на кнопку, где изображено {target_emoji}"
    )
    await message.answer(text, reply_markup=generate_captcha_keyboard(target_emoji))

@dp.callback_query(F.data.startswith("captcha:"))
async def process_captcha(callback: types.CallbackQuery):
    _, clicked, correct = callback.data.split(":")
    if clicked == correct:
        now_str = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_passed_captcha = 1, captcha_date = ? WHERE user_id = ?", (now_str, callback.from_user.id,))
            await db.commit()
        await callback.answer("✅ Проверка успешно пройдена!", show_alert=True)
        await callback.message.delete()
        await show_start_screen(callback.message, callback.from_user)
    else:
        await callback.answer("❌ Неверно! Попробуйте снова.", show_alert=True)
        target_emoji, _ = random.choice(CAPTCHA_FRUITS)
        text = (
            "🤖 *ПРОВЕРКА НА РОБОТА*\n\n"
            f"Нажми на кнопку, где изображено {target_emoji}"
        )
        await callback.message.edit_text(text, reply_markup=generate_captcha_keyboard(target_emoji))

async def check_sponsors_subscription(user_id: int) -> bool:
    s1 = await get_setting("sponsor_1", "@StarPays_Reviews")
    s2 = await get_setting("sponsor_2", "@StarPay_s")
    sponsors = [s1, s2]
    for sp in sponsors:
        if not sp:
            continue
        try:
            member = await bot.get_chat_member(chat_id=sp, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception:
            return False
    return True

async def send_sponsors_requirement(message: types.Message):
    s1 = await get_setting("sponsor_1", "@StarPays_Reviews")
    s2 = await get_setting("sponsor_2", "@StarPay_s")
    builder = InlineKeyboardBuilder()
    if s1:
        builder.button(text="📢 Спонсор 1", url=f"https://t.me/{s1.replace('@', '')}")
    if s2:
        builder.button(text="📢 Спонсор 2", url=f"https://t.me/{s2.replace('@', '')}")
    builder.button(text="✅ Я подписался", callback_data="check_sponsors_sub")
    builder.adjust(1)

    text = (
        "⚠️ *Обязательная подписка*\n\n"
        + quote_block(f"Для использования бота необходимо подписаться на наши спонсорские каналы:\n• {s1}\n• {s2}\n\nПодпишитесь и нажмите кнопку проверки ниже\\!")
    )
    await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "check_sponsors_sub")
async def check_sponsors_sub_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if await check_sponsors_subscription(user_id):
        await callback.answer("✅ Подписка подтверждена!", show_alert=True)
        await callback.message.delete()
        await show_start_screen(callback.message, callback.from_user)
    else:
        await callback.answer("❌ Вы подписались не на все каналы!", show_alert=True)

@dp.message(F.text.in_(MENU_BUTTONS))
async def guard_menu_buttons(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if await is_user_banned(user_id):
        await message.answer("🚫 *Вы заблокированы в этом боте\.*\nОбратитесь к администрации, если считаете это ошибкой\.")
        return

    handlers = {
        "💎 Задания": earn_cmd,
        "👥 Друзья": friends_cmd,
        "🖱 Клик": click_cmd,
        "🎁 Вывести Звёзды": withdraw_cmd,
        "📅 Ежедневный бонус": daily_bonus_cmd,
        "👤 Профиль": profile_cmd,
        "👑 Админ-панель": admin_panel,
    }
    handler = handlers.get(message.text)
    if handler:
        await handler(message, state)

async def show_start_screen(message: types.Message, user: types.User):
    first_name_esc = escape_md(user.first_name)
    welcome_text = (
        f"Привет, *{first_name_esc}* 👏\n\n"
        + quote_block("Приглашай друзей и зарабатывай звёзды\.\nКопи и выводи подарками Telegram\.")
    )

    welcome_photo = None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'welcome_photo'") as cursor:
            row = await cursor.fetchone()
            if row: welcome_photo = row[0]

    kb = await main_keyboard(user.id)
    if welcome_photo:
        try: await message.answer_photo(photo=welcome_photo, caption=welcome_text, reply_markup=kb)
        except Exception: await message.answer(welcome_text, reply_markup=kb)
    else:
        await message.answer(welcome_text, reply_markup=kb)

@dp.message(Command("start"))
async def start_cmd(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or ""
    args = command.args

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, is_passed_captcha, captcha_date FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user_row = await cursor.fetchone()
        
        is_new = user_row is None
        is_passed = 0
        captcha_date_str = None
        if user_row:
            is_passed = user_row[1]
            captcha_date_str = user_row[2]

        referrer_id = None
        if is_new and args and args.isdigit() and int(args) != user_id:
            referrer_id = int(args)

        await db.execute(
            "INSERT INTO users (user_id, username, balance, is_passed_captcha, completed_tasks, used_promo, referrer_id) "
            "VALUES (?, ?, 0.0, 0, '', 0, ?) ON CONFLICT(user_id) DO UPDATE SET username = ?",
            (user_id, username, referrer_id, username)
        )

        if is_new and referrer_id:
            ref_reward = await get_ref_reward()
            try:
                async with db.execute("SELECT completed_tasks FROM users WHERE user_id = ?", (user_id,)) as c:
                    r_tasks = (await c.fetchone())[0]
                    completed_list = r_tasks.split(",") if r_tasks else []
                if len(completed_list) >= 1:
                    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (ref_reward, referrer_id))
                    await log_balance_change(db, referrer_id, ref_reward, "referral")
                    await bot.send_message(
                        referrer_id,
                        f"🎉 *По вашей ссылке зарегистрировался друг и выполнил задание\! Вам начислено \+`{escape_md(str(ref_reward))}` 💫*"
                    )
            except Exception:
                pass

        await db.commit()

    if is_passed and captcha_date_str:
        try:
            c_date = datetime.fromisoformat(captcha_date_str)
            if datetime.now() >= c_date + timedelta(days=7):
                is_passed = 0
        except Exception:
            is_passed = 0

    if not is_passed:
        await send_captcha(message)
        return

    if not await check_sponsors_subscription(user_id):
        await send_sponsors_requirement(message)
        return

    if args and args.startswith("check_"):
        check_code = args.replace("check_", "")
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT creator_id, amount, is_used, chat_id, msg_id FROM checks WHERE code = ?", (check_code,)) as cursor:
                check_data = await cursor.fetchone()

            if check_data:
                creator_id, amount, is_used, c_chat_id, c_msg_id = check_data
                if user_id == creator_id:
                    await message.answer("❌ *Вы не можете активировать собственный чек\!*")
                    return

                if is_used == 0:
                    await db.execute("UPDATE checks SET is_used = 1, used_by = ? WHERE code = ?", (user_id, check_code))
                    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
                    await log_balance_change(db, user_id, amount, "check")
                    await db.commit()

                    amount_val = int(amount) if amount.is_integer() else amount
                    amount_esc = escape_md(f"{amount_val}")
                    await message.answer(f"🎉 *Вы успешно активировали чек на `{amount_esc}` Telegram Stars\!*")

                    if c_chat_id and c_msg_id:
                        builder = InlineKeyboardBuilder()
                        builder.button(text="✅ Получено", callback_data="check_already_used")
                        try:
                            await bot.edit_message_reply_markup(chat_id=c_chat_id, message_id=c_msg_id, reply_markup=builder.as_markup())
                        except Exception: pass
                else:
                    await message.answer("❌ Этот чек уже кто-то активировал!")
            else:
                await message.answer("❌ Чек не найден или недействителен!")

    await show_start_screen(message, message.from_user)

@dp.callback_query(F.data == "check_already_used")
async def check_already_used_handler(callback: types.CallbackQuery):
    await callback.answer("Этот чек уже был активирован!", show_alert=True)

async def daily_bonus_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
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
                h_esc, m_esc = escape_md(str(hours)), escape_md(str(minutes))
                await message.answer(f"⏳ *Вы уже получали бонус\!*\nСледующий бонус будет доступен через: *{h_esc} ч\. {m_esc} мин\.*")
                return

        bonus_amount = random_daily_bonus()
        await db.execute("UPDATE users SET balance = balance + ?, last_daily = ? WHERE user_id = ?",
                         (bonus_amount, now.isoformat(), user_id))
        await log_balance_change(db, user_id, bonus_amount, "daily")
        await db.commit()

    bonus_esc = escape_md(f"{bonus_amount:.1f}")
    await message.answer(f"🎁 *Поздравляем\! Вы получили ежедневный бонус: \+{bonus_esc} ⭐*")

async def click_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    if not await is_click_enabled():
        await message.answer("🖱 *Кнопка «Клик» временно отключена администратором\.*")
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
                m_esc, s_esc = escape_md(str(minutes)), escape_md(str(seconds))
                await message.answer(
                    "🖱 *Клик уже был использован\!*\n\n"
                    + quote_block(f"Следующий клик будет доступен через: {minutes} мин\\. {seconds} сек\\.")
                )
                return

        await db.execute(
            "UPDATE users SET balance = balance + ?, last_click = ? WHERE user_id = ?",
            (reward, now.isoformat(), user_id)
        )
        await log_balance_change(db, user_id, reward, "click")
        await db.commit()

    reward_esc = escape_md(f"{reward:.2f}")
    cd_esc = escape_md(str(cooldown_min))
    await message.answer(
        "🖱 *Клик засчитан\!*\n\n"
        + quote_block(f"Начислено: \\+{reward_esc} ⭐\nСледующий клик будет доступен через {cd_esc} мин\\.")
    )

async def friends_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    bot_info = await bot.get_me()

    ref_reward = await get_ref_reward()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            invited_count = row[0] if row else 0

    reward_esc = escape_md(f"{ref_reward}")
    invited_esc = escape_md(str(invited_count))
    ref_link_esc = escape_md(ref_link)

    text = (
        f"Получай *\+{reward_esc}* 💫 за каждого приглашенного друга\!\n\n"
        f"🔗 *Твоя реферальная ссылка:*\n`{ref_link_esc}`\n\n"
        + quote_block(f"Приглашай по этой ссылке своих друзей, отправляй её во все чаты и зарабатывай звёзды\\!")
        + f"\n\nПриглашено тобой: *{invited_esc}* 👤"
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="💞 Отправить Ссылку Друзьям",
        switch_inline_query=f"\n🚀 Зарабатывай бесплатные звёзды Telegram со мной! Ссылка: {ref_link}"
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
                ch_id, title = all_channels[ch_db_id]
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
                (penalty_total, ",".join(completed), user_id)
            )
            await db.commit()

            pen_esc = escape_md(f"{penalty_total:.2f}")
            cnt_esc = escape_md(str(len(unsubbed_tasks)))
            try:
                await bot.send_message(
                    user_id,
                    f"⚠️ *Обнаружена отписка от каналов \({cnt_esc}\)\!*\n"
                    f"С вашего баланса списан штраф: *\-`{pen_esc}` ⭐*"
                )
            except Exception: pass

async def send_next_task(event, user_id: int):
    if not await check_sponsors_subscription(user_id):
        if isinstance(event, types.CallbackQuery):
            await event.message.answer("⚠️ Сначала подпишитесь на спонсоров!")
        else:
            await send_sponsors_requirement(event)
        return

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
                        await db.execute("UPDATE users SET completed_tasks = ? WHERE user_id = ?", (",".join(completed), user_id))
                        await db.commit()
                    continue
            except Exception:
                pass

        target_channel = ch
        break

    if not target_channel:
        text = "😔 На данный момент нет доступных заданий\. Попробуйте позже\!"
        if isinstance(event, types.CallbackQuery):
            try: await event.message.delete()
            except Exception: pass
            await event.message.answer(text)
        else:
            await event.answer(text)
        return

    ch_db_id, ch_id, title, link = target_channel

    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Перейти", url=link)
    builder.button(text="✅ Подтвердить", callback_data=f"check_sub:{ch_db_id}:{ch_id}")
    builder.button(text="⏩ Пропустить", callback_data=f"skip_task:{ch_db_id}")
    builder.adjust(2, 1)

    title_esc = escape_md(title)
    reward_esc = escape_md(str(REWARD_PER_SUB))
    task_text = (
        "💡 *Получай Звёзды за простые задания\!* 👆\n\n"
        f"🟢 Подпишись на *{title_esc}* и нажми «Подтвердить»\n\n"
        f"Вознаграждение: *\+{reward_esc}* 💫"
    )

    if isinstance(event, types.CallbackQuery):
        try: await event.message.delete()
        except Exception: pass
        await event.message.answer(task_text, reply_markup=builder.as_markup())
    else:
        await event.answer(task_text, reply_markup=builder.as_markup())

async def earn_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await send_next_task(message, message.from_user.id)

@dp.callback_query(F.data.startswith("check_sub:"))
async def check_sub_handler(callback: types.CallbackQuery):
    if not await check_sponsors_subscription(callback.from_user.id):
        await callback.answer("❌ Сначала подпишитесь на спонсоров!", show_alert=True)
        return

    _, ch_db_id, ch_id = callback.data.split(":")
    user_id = callback.from_user.id

    try:
        member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT completed_tasks, COALESCE(balance, 0.0), referrer_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    completed = row[0].split(",") if row and row[0] else []
                    balance = float(row[1]) if row else 0.0
                    referrer_id = row[2] if row else None

                if str(ch_db_id) in completed:
                    await callback.answer("❌ Вы уже получили награду за это задание!", show_alert=True)
                    return

                is_first_task = len(completed) == 0

                completed.append(str(ch_db_id))
                new_completed_str = ",".join(completed)
                new_balance = balance + REWARD_PER_SUB

                await db.execute(
                    "UPDATE users SET balance = ?, completed_tasks = ? WHERE user_id = ?",
                    (new_balance, new_completed_str, user_id)
                )
                await log_balance_change(db, user_id, REWARD_PER_SUB, "task")

                if is_first_task and referrer_id:
                    ref_reward = await get_ref_reward()
                    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (ref_reward, referrer_id))
                    await log_balance_change(db, referrer_id, ref_reward, "referral")
                    try:
                        await bot.service_message_or_send(
                            referrer_id,
                            f"🎉 *Приглашенный вами пользователь выполнил задание\! Начислено \+`{escape_md(str(ref_reward))}` 💫*"
                        )
                    except Exception:
                        try:
                            await bot.send_message(
                                referrer_id,
                                f"🎉 *Приглашенный вами пользователь выполнил задание\! Начислено \+`{escape_md(str(ref_reward))}` 💫*"
                            )
                        except Exception: pass

                await db.commit()

            await callback.answer("✅ Успешно! Подписка подтверждена, звёзды начислены.", show_alert=True)
            await send_next_task(callback, user_id)
        else:
            await callback.answer("❌ Вы ещё не подписались на канал!", show_alert=True)
    except Exception:
        await callback.answer("⚠️ Ошибка проверки! Убедитесь, что бот добавлен в администраторы канала.", show_alert=True)

@dp.callback_query(F.data.startswith("skip_task:"))
async def skip_task_handler(callback: types.CallbackQuery):
    await callback.answer("Задание пропущено", show_alert=False)
    await send_next_task(callback, callback.from_user.id)

async def profile_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = float(row[0]) if row else 0.0

        async with db.execute("SELECT SUM(amount) FROM withdraws WHERE user_id = ? AND status = 'pending'", (user_id,)) as cursor:
            row_pending = await cursor.fetchone()
            pending = float(row_pending[0]) if row_pending and row_pending[0] is not None else 0.0

        async with db.execute("SELECT SUM(amount) FROM withdraws WHERE user_id = ? AND status = 'completed'", (user_id,)) as cursor:
            row_completed = await cursor.fetchone()
            completed = float(row_completed[0]) if row_completed and row_completed[0] is not None else 0.0

    bal_str = escape_md(f"{balance:.2f}")
    pend_str = escape_md(f"{pending:.2f}")
    comp_str = escape_md(f"{completed:.2f}")

    profile_text = (
        "👤 *Ваш Профиль*\n\n"
        + quote_block(
            f"*Баланс*\n"
            f"└ Обычный: `{bal_str}` ⭐\n\n"
            f"*Выводы*\n"
            f"├ Ожидают: `{pend_str}` ⭐\n"
            f"└ Выведено: `{comp_str}` ⭐"
        )
    )
    await message.answer(profile_text, reply_markup=await profile_keyboard())

@dp.callback_query(F.data == "profile_back")
async def profile_back_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = float(row[0]) if row else 0.0

        async with db.execute("SELECT SUM(amount) FROM withdraws WHERE user_id = ? AND status = 'pending'", (user_id,)) as cursor:
            row_pending = await cursor.fetchone()
            pending = float(row_pending[0]) if row_pending and row_pending[0] is not None else 0.0

        async with db.execute("SELECT SUM(amount) FROM withdraws WHERE user_id = ? AND status = 'completed'", (user_id,)) as cursor:
            row_completed = await cursor.fetchone()
            completed = float(row_completed[0]) if row_completed and row_completed[0] is not None else 0.0

    bal_str = escape_md(f"{balance:.2f}")
    pend_str = escape_md(f"{pending:.2f}")
    comp_str = escape_md(f"{completed:.2f}")

    profile_text = (
        "👤 *Ваш Профиль*\n\n"
        + quote_block(
            f"*Баланс*\n"
            f"└ Обычный: `{bal_str}` ⭐\n\n"
            f"*Выводы*\n"
            f"├ Ожидают: `{pend_str}` ⭐\n"
            f"└ Выведено: `{comp_str}` ⭐"
        )
    )
    try:
        await callback.message.edit_text(profile_text, reply_markup=await profile_keyboard())
    except Exception:
        await callback.message.answer(profile_text, reply_markup=await profile_keyboard())
    await callback.answer()

async def build_top_text(period: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        if period == "all":
            query = "SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT ?"
            params = (TOP_USERS_LIMIT,)
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
            top_rows = [(u_id, u_name, bal) for u_id, u_name, bal in rows]
        else:
            since = datetime.now() - (timedelta(days=1) if period == "day" else timedelta(days=7))
            query = """
                SELECT bl.user_id, u.username, SUM(bl.amount) as total
                FROM balance_log bl
                LEFT JOIN users u ON bl.user_id = u.user_id
                WHERE bl.created_at >= ?
                GROUP BY bl.user_id
                ORDER BY total DESC
                LIMIT ?
            """
            async with db.execute(query, (since.isoformat(), TOP_USERS_LIMIT)) as cursor:
                rows = await cursor.fetchall()
            top_rows = [(u_id, u_name, total) for u_id, u_name, total in rows]

    period_labels = {"day": "за день", "week": "за неделю", "all": "за всё время"}
    header = f"🏆 *Топ пользователей {period_labels[period]}*\n\n"

    if not top_rows:
        return header + "Пока пусто\."

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (u_id, u_name, val) in enumerate(top_rows, start=1):
        display_name = escape_md(u_name) if u_name else f"ID{u_id}"
        val_esc = escape_md(f"{val:.1f}")
        prefix = medals[idx - 1] if idx <= 3 else f"{idx}\."
        lines.append(f"{prefix} {display_name} — {val_esc} ⭐")

    return header + quote_block("\n".join(lines))

@dp.callback_query(F.data.startswith("show_top:"))
async def show_top_handler(callback: types.CallbackQuery):
    if not await is_top_enabled():
        await callback.answer("❌ Топ пользователей сейчас отключён.", show_alert=True)
        return

    parts = callback.data.split(":")
    period = parts[1]

    text = await build_top_text(period)
    try:
        await callback.message.edit_text(text, reply_markup=top_period_keyboard(period))
    except Exception:
        await callback.message.answer(text, reply_markup=top_period_keyboard(period))
    await callback.answer()

@dp.callback_query(F.data == "activate_promo")
async def promo_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("🎟 *Введите промокод:*")
    await state.set_state(UserStates.waiting_for_promo)
    await callback.answer()

@dp.message(UserStates.waiting_for_promo)
async def promo_process(message: types.Message, state: FSMContext):
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    code = (message.text or "").strip().upper()
    if not code:
        await message.answer("❌ Введите текст промокода\\!")
        return

    user_id = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT amount, max_activations, used_activations FROM promocodes WHERE code = ?", (code,)
        ) as cursor:
            promo_row = await cursor.fetchone()

        if not promo_row:
            await message.answer("❌ Неверный промокод\\!")
            await state.clear()
            return

        amount, max_activations, used_activations = promo_row
        amount = float(amount)

        async with db.execute(
            "SELECT 1 FROM promo_activations WHERE promo_code = ? AND user_id = ?", (code, user_id)
        ) as cursor:
            already_used = await cursor.fetchone() is not None

        if already_used:
            await message.answer("❌ Вы уже активировали этот промокод\\!")
            await state.clear()
            return

        if max_activations is not None and used_activations >= max_activations:
            await message.answer("❌ Лимит активаций этого промокода исчерпан\\!")
            await state.clear()
            return

        await db.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, 0.0) ON CONFLICT(user_id) DO NOTHING", (user_id,)
        )
        await db.commit()

        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.execute(
            "INSERT INTO promo_activations (promo_code, user_id) VALUES (?, ?)", (code, user_id)
        )
        await db.execute(
            "UPDATE promocodes SET used_activations = used_activations + 1 WHERE code = ?", (code,)
        )
        await log_balance_change(db, user_id, amount, "promo")
        await db.commit()

        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            new_balance = row[0] if row else 0.0

    amount_val = int(amount) if amount.is_integer() else amount
    amount_esc = escape_md(f"{amount_val}")
    bal_esc = escape_md(f"{new_balance:.2f}")
    await message.answer(f"🎉 *Промокод успешно активирован\! Начислено \+{amount_esc} ⭐*\nНовый баланс: `{bal_esc}` ⭐")

    await state.clear()

async def withdraw_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            balance = float(row[0]) if row else 0.0

    bal_esc = escape_md(f"{balance:.2f}")
    text = (
        "💸 *Вывод звёзд*\n\n"
        f"├ Баланс: `{bal_esc}` ⭐\n\n"
        "Выберите подарок для вывода:"
    )
    await message.answer(text, reply_markup=withdraw_keyboard())

@dp.callback_query(F.data.startswith("withdraw_gift:"))
async def process_withdraw(callback: types.CallbackQuery, state: FSMContext):
    amount = float(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = float(row[0]) if row else 0.0

        if balance < amount:
            await callback.answer(f"❌ Недостаточно средств! Нужно: {amount} ⭐", show_alert=True)
            return

    await state.update_data(pending_amount=amount)

    price_esc = escape_md(str(GIFT_TEXT_PRICE))
    text = (
        "✍️ *Добавить надпись на подарок?*\n\n"
        + quote_block(f"Стоимость: \\+{price_esc} ⭐\nНадпись увидит тот, кто получит подарок 💫")
    )
    await callback.message.edit_text(text, reply_markup=gift_text_ask_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "gift_ask:yes")
async def gift_ask_yes_handler(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "✍️ *Напишите текст, который будет написан в профиле подарка:*\n\n"
        f"Максимум {GIFT_TEXT_MAX_LEN} символов\."
    )
    await callback.message.edit_text(text, reply_markup=gift_text_cancel_keyboard())
    await state.set_state(UserStates.waiting_for_gift_text)
    await callback.answer()

@dp.callback_query(F.data == "gift_ask_back")
async def gift_ask_back_handler(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(None)
    await state.set_data(data)

    price_esc = escape_md(str(GIFT_TEXT_PRICE))
    text = (
        "✍️ *Добавить надпись на подарок?*\n\n"
        + quote_block(f"Стоимость: \\+{price_esc} ⭐\nНадпись увидит тот, кто получит подарок 💫")
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
        await message.answer("❌ Текст не может быть пустым\\! Введите текст ещё раз:")
        return

    if len(gift_text) > GIFT_TEXT_MAX_LEN:
        gift_text = gift_text[:GIFT_TEXT_MAX_LEN]

    await finalize_withdraw(message, state, gift_text)

async def finalize_withdraw(event, state: FSMContext, gift_text: str | None):
    data = await state.get_data()
    amount = data.get("pending_amount")
    user_id = event.from_user.id

    if amount is None:
        if isinstance(event, types.CallbackQuery):
            await event.answer("❌ Сессия устарела, попробуйте заново.", show_alert=True)
        else:
            await event.answer("❌ Сессия устарела, попробуйте заново.")
        await state.clear()
        return

    total_cost = amount + (GIFT_TEXT_PRICE if gift_text else 0)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = float(row[0]) if row else 0.0

        if balance < total_cost:
            msg_text = f"❌ Недостаточно средств с учётом надписи! Нужно: {total_cost} ⭐"
            if isinstance(event, types.CallbackQuery):
                await event.answer(msg_text, show_alert=True)
            else:
                await event.answer(msg_text)
            await state.clear()
            return

        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total_cost, user_id))
        cursor = await db.execute(
            "INSERT INTO withdraws (user_id, amount, gift_text) VALUES (?, ?, ?)",
            (user_id, amount, gift_text)
        )
        withdraw_id = cursor.lastrowid
        await db.commit()

    await state.clear()

    gift_text_line = f"\n✍️ Надпись: _{escape_md(gift_text)}_" if gift_text else ""
    result_text = (
        f"🎉 *Заявка на вывод `{int(amount)}` ⭐ успешно оформлена\!*{gift_text_line}\n"
        "Ожидайте подтверждения от администратора\."
    )

    if isinstance(event, types.CallbackQuery):
        await event.answer("✅ Заявка на вывод создана!", show_alert=True)
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
    clean_bot_username = BOT_USERNAME.replace("@", "")
    bot_link = f"https://t.me/{clean_bot_username}"
    bot_link_esc = escape_md(bot_link)

    gift_emoji = "🧸" if amount <= 15 else ("🎁" if amount <= 25 else ("🚀" if amount <= 50 else "🏆"))

    request_log = (
        "🧾 *Новая заявка\\!*\n\n"
        f"👤 {user_mention_esc}\n"
        f"⏳ {amount_esc} {gift_emoji} ожидает подтверждения"
        f"\n\n[{escape_md(BOT_USERNAME)}]({bot_link_esc})"
    )
    await send_log(request_log)

    gift_admin_line = f"\n✍️ Надпись: _{escape_md(gift_text)}_" if gift_text else ""
    admin_msg = (
        "💸 *Новая заявка на вывод\\!*\n\n"
        f"🆔 Заявка: \\#{withdraw_id}\n"
        f"👤 Пользователь: {user_mention_esc}\n"
        f"💰 Сумма: *{amount_esc} ⭐*"
        f"{gift_admin_line}"
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
    if not await is_admin(callback.from_user.id): return
    w_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT w.user_id, w.amount, w.status, u.username, w.gift_text "
            "FROM withdraws w LEFT JOIN users u ON w.user_id = u.user_id "
            "WHERE w.id = ?",
            (w_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await callback.answer("Заявка не найдена!", show_alert=True)
            return

        u_id, amt, status, u_name, gift_text = row
        if status != "pending":
            await callback.answer("Заявка уже обработана!", show_alert=True)
            return

        await db.execute("UPDATE withdraws SET status = 'completed' WHERE id = ?", (w_id,))
        await db.commit()

    try:
        await bot.send_message(
            u_id,
            f"✅ Ваша заявка #{w_id} на {int(amt)} ⭐️ была успешно обработана! Подарок отправлен.",
            parse_mode=None
        )
    except Exception: pass

    user_mention = f"@{u_name}" if u_name else f"ID: {u_id}"
    user_mention_esc = escape_md(user_mention)
    amt_esc = escape_md(str(int(amt)))
    clean_bot_username = BOT_USERNAME.replace("@", "")
    bot_link = f"https://t.me/{clean_bot_username}"
    bot_link_esc = escape_md(bot_link)
    
    gift_emoji = "🧸" if amt <= 15 else ("🎁" if amt <= 25 else ("🚀" if amt <= 50 else "🏆"))

    payout_log = (
        "🧾 *Новая выплата\\!*\n\n"
        f"👤 {user_mention_esc}\n"
        f"✅ {amt_esc} {gift_emoji} успешно выведено"
        f"\n\n[{escape_md(BOT_USERNAME)}]({bot_link_esc})"
    )
    await send_log(payout_log)

    status_admin_msg = (
        "💸 *Новая заявка на вывод\\!*\n\n"
        f"🆔 Заявка: \\#{w_id}\n"
        f"👤 Пользователь: {user_mention_esc}\n"
        f"💰 Сумма: *{amt_esc} ⭐*\n\n"
        "✅ *СТАТУС: ВЫДАНО*"
    )
    await callback.message.edit_text(status_admin_msg)
    await callback.answer("Заявка подтверждена!", show_alert=True)

@dp.callback_query(F.data.startswith("withdraw_reject:"))
async def withdraw_reject_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    w_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT w.user_id, w.amount, w.status, u.username, w.gift_text "
            "FROM withdraws w LEFT JOIN users u ON w.user_id = u.user_id "
            "WHERE w.id = ?",
            (w_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await callback.answer("Заявка не найдена!", show_alert=True)
            return

        u_id, amt, status, u_name, gift_text = row
        if status != "pending":
            await callback.answer("Заявка уже обработана!", show_alert=True)
            return

        gift_refund = GIFT_TEXT_PRICE if gift_text else 0
        total_refund = amt + gift_refund

        await db.execute("UPDATE withdraws SET status = 'rejected' WHERE id = ?", (w_id,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_refund, u_id))
        await db.commit()

    try:
        await bot.send_message(u_id, f"❌ Ваша заявка на {int(amt)} ⭐ была отклонена. Звёзды возвращены на баланс.", parse_mode=None)
    except Exception: pass

    user_mention = f"@{u_name}" if u_name else f"ID: {u_id}"
    user_mention_esc = escape_md(user_mention)
    amt_esc = escape_md(str(int(amt)))

    status_admin_msg = (
        "💸 *Новая заявка на вывод\\!*\n\n"
        f"🆔 Заявка: \\#{w_id}\n"
        f"👤 Пользователь: {user_mention_esc}\n"
        f"💰 Сумма: *{amt_esc} ⭐*\n\n"
        "❌ *СТАТУС: ОТКЛОНЕНО*"
    )
    await callback.message.edit_text(status_admin_msg)
    await callback.answer("Заявка отклонена, средства возвращены!", show_alert=True)

@dp.callback_query(F.data == "back_to_main")
async def back_to_main_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try: await callback.message.delete()
    except Exception: pass
    await show_start_screen(callback.message, callback.from_user)
    await callback.answer()

async def admin_panel(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM withdraws WHERE status = 'pending'") as c:
            pending_withdraws = (await c.fetchone())[0]

    text = (
        "👑 *Панель управления администратора*\n\n"
        + quote_block(f"👥 Пользователей: {total_users}\n⏳ Заявок в ожидании: {pending_withdraws}")
        + "\n\nВыберите раздел:"
    )
    builder = admin_keyboard(0)
    builder.inline_keyboard.append([types.InlineKeyboardButton(text="← Назад", callback_data="admin_back_to_main")])
    await message.answer(text, reply_markup=builder)

@dp.callback_query(F.data == "admin_back_to_main")
async def admin_back_to_main_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await show_start_screen(callback.message, callback.from_user)
    await callback.answer()

@dp.callback_query(F.data == "admin_back_to_panel")
async def admin_back_to_panel_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM withdraws WHERE status = 'pending'") as c:
            pending_withdraws = (await c.fetchone())[0]

    text = (
        "👑 *Панель управления администратора*\n\n"
        + quote_block(f"👥 Пользователей: {total_users}\n⏳ Заявок в ожидании: {pending_withdraws}")
        + "\n\nВыберите раздел:"
    )
    builder = admin_keyboard(0)
    builder.inline_keyboard.append([types.InlineKeyboardButton(text="← Назад", callback_data="admin_back_to_main")])
    try:
        await callback.message.edit_text(text, reply_markup=builder)
    except Exception:
        await callback.message.answer(text, reply_markup=builder)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_page:"))
async def admin_page_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    page = int(callback.data.split(":")[1])
    builder = admin_keyboard(page)
    builder.inline_keyboard.append([types.InlineKeyboardButton(text="← Назад", callback_data="admin_back_to_main")])
    try:
        await callback.message.edit_reply_markup(reply_markup=builder)
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data == "admin_page_noop")
async def admin_page_noop_handler(callback: types.CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data == "admin_toggle_top")
async def admin_toggle_top_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    current = await is_top_enabled()
    new_value = "0" if current else "1"
    await set_setting("top_enabled", new_value)

    status_text = "включена ✅" if new_value == "1" else "отключена ❌"
    await callback.answer(f"Кнопка топов теперь: {status_text}", show_alert=True)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return

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
        "📊 *Статистика бота*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "*👥 Пользователи*\n"
        + quote_block(
            f"Всего: {total_users}\n"
            f"За 24ч: {new_users_24h}\n"
            f"За 7 дней: {new_users_7d}\n"
            f"Забанено: {banned_count}"
        )
        + "\n\n*💰 Финансы*\n"
        + quote_block(
            f"Суммарный баланс: {total_balance:.2f} ⭐\n"
            f"Всего выплачено: {total_paid_out:.2f} ⭐\n"
            f"Заявок ожидают: {pending_withdraws}"
        )
        + "\n\n*📢 Каналы и промокоды*\n"
        + quote_block(f"Каналов подключено: {total_channels}\nПромокодов создано: {total_promos}")
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await callback.message.answer(escape_md_preserve_formatting(stats_text), reply_markup=builder.as_markup())
    await callback.answer()

def escape_md_preserve_formatting(text: str) -> str:
    def esc_line(line: str) -> str:
        result = re.sub(r'(?<!\\)([.\-!()])', r'\\\1', line)
        return result
    lines = text.split("\n")
    return "\n".join(esc_line(l) for l in lines)

@dp.callback_query(F.data == "admin_change_balance")
async def admin_change_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await callback.message.answer(
        "👤 *Введите ID или @username пользователя для изменения баланса:*\n\n"
        + quote_block("Пользователь должен хотя бы раз запустить бота \\(/start\\), иначе он не будет найден в базе\\."),
        reply_markup=builder.as_markup()
    )
    await state.set_state(AdminStates.waiting_for_balance_user)
    await callback.answer()

@dp.message(AdminStates.waiting_for_balance_user)
async def admin_change_balance_user(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    text = (message.text or "").strip().lstrip("@")
    if not text:
        await message.answer("❌ Введите ID или @username ещё раз:")
        return

    target_id = None
    username = None
    balance = 0.0

    async with aiosqlite.connect(DB_PATH) as db:
        if text.isdigit():
            async with db.execute("SELECT user_id, username, balance FROM users WHERE user_id = ?", (int(text),)) as cursor:
                row = await cursor.fetchone()
                if row: target_id, username, balance = row
        else:
            async with db.execute("SELECT user_id, username, balance FROM users WHERE LOWER(username) = LOWER(?)", (text,)) as cursor:
                row = await cursor.fetchone()
                if row: target_id, username, balance = row

    if not target_id:
        await message.answer(
            "❌ Пользователь не найден в базе\\!\n\n"
            "Убедитесь, что он хотя бы раз запускал бота \\(команда /start\\), и что username введён без опечаток\\.\n"
            "Попробуйте ввести ID или @username ещё раз:"
        )
        return

    user_info = f"@{escape_md(username)}" if username else f"ID: `{target_id}`"
    bal_esc = escape_md(f"{balance:.2f}")

    msg_text = (
        "💰 *Изменение баланса*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Пользователь: {user_info}\n"
        f"💰 Текущий баланс: `{bal_esc}` ⭐\n\n"
        "Выберите действие:"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Прибавить", callback_data=f"bal_act:add:{target_id}")
    builder.button(text="➖ Отобрать", callback_data=f"bal_act:sub:{target_id}")
    builder.button(text="✏️ Изменить", callback_data=f"bal_act:set:{target_id}")
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    builder.adjust(3, 1)

    await message.answer(msg_text, reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data.startswith("bal_act:"))
async def admin_balance_action(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    _, action, target_id = callback.data.split(":")

    await state.update_data(target_user_id=int(target_id), balance_action=action)

    prompts = {
        "add": "➕ *Введите сумму, которую нужно прибавить к балансу:*",
        "sub": "➖ *Введите сумму, которую нужно отнять из баланса:*",
        "set": "✏️ *Введите новое точное значение баланса:*"
    }

    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await callback.message.answer(prompts[action], reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_balance_value)
    await callback.answer()

@dp.message(AdminStates.waiting_for_balance_value)
async def admin_change_balance_value_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    try:
        val = float(message.text.replace(",", "."))
        if val < 0 and (await state.get_data()).get("balance_action") != "set":
            val = abs(val)
    except ValueError:
        await message.answer("❌ Введите корректное число!")
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

    bal_esc = escape_md(f"{new_bal:.2f}")
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await message.answer(f"✅ *Баланс обновлён\!*\n\n👤 Пользователь: `{target_id}`\n💰 Новый баланс: `{bal_esc}` ⭐", reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await callback.message.answer("🔎 *Введите ID или @username пользователя для просмотра карточки:*", reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_find_user)
    await callback.answer()

@dp.message(AdminStates.waiting_for_find_user)
async def admin_find_user_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    text = (message.text or "").strip().lstrip("@")
    if not text:
        await message.answer("❌ Введите ID или @username ещё раз:")
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
            await message.answer("❌ Пользователь не найден в базе\\! Попробуйте ещё раз:")
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
    status_line = "🚫 Забанен" if banned else ("👑 Администратор" if is_adm else "✅ Активен")

    card_text = (
        "🔎 *Карточка пользователя*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + quote_block(
            f"👤 {user_info}\n"
            f"🆔 ID: {u_id}\n"
            f"📌 Статус: {status_line}\n"
            f"💰 Баланс: {balance:.2f} ⭐\n"
            f"💸 Выведено: {paid:.2f} ⭐\n"
            f"⏳ В ожидании: {pending:.2f} ⭐\n"
            f"👥 Приглашено: {invited}\n"
            f"📅 Регистрация: {created_at}"
        )
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await message.answer(escape_md_preserve_formatting(card_text), reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data.startswith("admin_pending_w_page:"))
async def admin_pending_withdraws_page(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return

    page = int(callback.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT w.id, w.user_id, w.amount, u.username, w.gift_text "
            "FROM withdraws w "
            "LEFT JOIN users u ON w.user_id = u.user_id "
            "WHERE w.status = 'pending' "
            "ORDER BY w.id ASC"
        ) as cursor:
            withdraws = await cursor.fetchall()

    if not withdraws:
        msg_text = "🎉 *Нет ожидающих заявок на вывод\.*"
        builder = InlineKeyboardBuilder()
        builder.button(text="← Назад", callback_data="admin_back_to_panel")
        if callback.message.text:
            await callback.message.edit_text(msg_text, reply_markup=builder.as_markup())
        else:
            await callback.message.answer(msg_text, reply_markup=builder.as_markup())
        await callback.answer()
        return

    total_items = len(withdraws)
    total_pages = (total_items + WITHDRAWS_PER_PAGE - 1) // WITHDRAWS_PER_PAGE
    page = max(0, min(page, total_pages - 1))

    start_idx = page * WITHDRAWS_PER_PAGE
    end_idx = start_idx + WITHDRAWS_PER_PAGE
    page_items = withdraws[start_idx:end_idx]

    text_lines = [
        "⏳ *Ожидающие заявки на вывод*",
        f"Страница *{page + 1}/{total_pages}* · Всего: *{total_items}*\n",
        "━━━━━━━━━━━━━━━━━━"
    ]

    for w_id, u_id, amt, u_name, gtext in page_items:
        u_info = f"@{escape_md(u_name)}" if u_name else f"`{u_id}`"
        amt_esc = escape_md(f"{int(amt)}")
        gift_line = f"\n✍️ _{escape_md(gtext)}_" if gtext else ""
        text_lines.append(f"🆔 \\#{w_id} · 👤 {u_info} · 💰 *{amt_esc}* ⭐{gift_line}")
        text_lines.append("━━━━━━━━━━━━━━━━━━")

    text = "\n".join(text_lines)

    builder = InlineKeyboardBuilder()
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_pending_w_page:{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_pending_w_page:{page + 1}"))

    if nav_buttons:
        builder.row(*nav_buttons)
    builder.row(types.InlineKeyboardButton(text="← Назад в панель", callback_data="admin_back_to_panel"))

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=builder.as_markup())

    await callback.answer()

@dp.callback_query(F.data == "admin_sponsors_menu")
async def admin_sponsors_menu_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    s1 = await get_setting("sponsor_1", "@StarPays_Reviews")
    s2 = await get_setting("sponsor_2", "@StarPay_s")

    text = (
        "📢 *Управление обязательными спонсорами*\n\n"
        + quote_block(f"Текущие каналы:\n1. {s1}\n2. {s2}")
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить Спонсора 1", callback_data="admin_set_sponsor_1")
    builder.button(text="✏️ Изменить Спонсора 2", callback_data="admin_set_sponsor_2")
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    builder.adjust(1)
    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "admin_set_sponsor_1")
async def admin_set_sponsor_1_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await callback.message.answer("📢 *Введите новый username или ID для Спонсора 1 (например @channel):*", reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_sponsor_1)
    await callback.answer()

@dp.message(AdminStates.waiting_for_sponsor_1)
async def admin_set_sponsor_1_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    val = message.text.strip()
    await set_setting("sponsor_1", val)
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await message.answer(f"✅ *Спонсор 1 успешно изменен на `{escape_md(val)}`\!*", reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data == "admin_set_sponsor_2")
async def admin_set_sponsor_2_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await callback.message.answer("📢 *Введите новый username или ID для Спонсора 2 (например @channel):*", reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_sponsor_2)
    await callback.answer()

@dp.message(AdminStates.waiting_for_sponsor_2)
async def admin_set_sponsor_2_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    val = message.text.strip()
    await set_setting("sponsor_2", val)
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await message.answer(f"✅ *Спонсор 2 успешно изменен на `{escape_md(val)}`\!*", reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    text = "📢 *Отправьте сообщение \(текст или фото\) для рассылки всем пользователям:*"
    await callback.message.answer(text, reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

    await message.answer(f"🚀 *Рассылка запущена\.\.*\n👥 Получателей: *{len(users)}*")
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
            except TelegramForbiddenError:
                failed += 1
                break
            except Exception:
                failed += 1
                break

    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await message.answer(
        "✅ *Рассылка завершена\!*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🟢 Успешно: *{success}*\n"
        f"🔴 Ошибок: *{failed}*",
        reply_markup=builder.as_markup()
    )
    await state.clear()

@dp.callback_query(F.data == "admin_set_ref_reward")
async def admin_set_ref_reward_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    cur_rew = await get_ref_reward()
    cur_esc = escape_md(str(cur_rew))
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    text = (
        "⚙️ *Настройка реферальной награды*\n\n"
        + quote_block(f"Текущая награда за друга: {cur_esc} ⭐")
        + "\n\nВведите новое количество звёзд за приглашенного друга:"
    )
    await callback.message.answer(text, reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_ref_reward)
    await callback.answer()

@dp.message(AdminStates.waiting_for_ref_reward)
async def admin_set_ref_reward_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    try:
        val = float(message.text.replace(",", "."))
        if val < 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Введите корректное число.")
        return

    await set_setting("ref_reward", str(val))
    val_esc = escape_md(str(val))
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await message.answer(f"✅ *Награда за приглашенного друга изменена на `{val_esc}` ⭐\!*", reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data == "admin_click_menu")
async def admin_click_menu(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    reward = await get_click_reward()
    cooldown = await get_click_cooldown_min()
    enabled = await is_click_enabled()

    text = (
        "🖱 *Настройка кнопки «Клик»*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + quote_block(
            f"💰 Награда за клик: {reward:.2f} ⭐\n"
            f"⏱ Кулдаун: {cooldown} мин\.\n"
            f"🔘 Статус: {'включена ✅' if enabled else 'отключена ❌'}"
        )
    )
    try:
        await callback.message.edit_text(text, reply_markup=click_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=click_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "click_toggle")
async def click_toggle_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    current = await is_click_enabled()
    new_value = "0" if current else "1"
    await set_setting("click_enabled", new_value)
    status_text = "включена ✅" if new_value == "1" else "отключена ❌"
    await callback.answer(f"Кнопка «Клик» теперь: {status_text}", show_alert=True)
    await admin_click_menu(callback, None)

@dp.callback_query(F.data == "click_set_reward")
async def click_set_reward_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_click_menu")
    await callback.message.answer("💰 *Введите новую награду за один клик \(в звёздах\):*", reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_click_reward)
    await callback.answer()

@dp.message(AdminStates.waiting_for_click_reward)
async def click_set_reward_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        val = float(message.text.replace(",", "."))
        if val <= 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Введите корректное положительное число.")
        return

    await set_setting("click_reward", str(val))
    val_esc = escape_md(str(val))
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_click_menu")
    await message.answer(f"✅ *Награда за клик изменена на `{val_esc}` ⭐\!*", reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data == "click_set_cooldown")
async def click_set_cooldown_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_click_menu")
    await callback.message.answer("⏱ *Введите новый кулдаун между кликами \(в минутах\):*", reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_click_cooldown)
    await callback.answer()

@dp.message(AdminStates.waiting_for_click_cooldown)
async def click_set_cooldown_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return
    try:
        val = int(message.text.strip())
        if val <= 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Введите целое положительное число минут.")
        return

    await set_setting("click_cooldown_min", str(val))
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_click_menu")
    await message.answer(f"✅ *Кулдаун клика изменён на `{val}` мин\.\!*", reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data == "admin_ban_user")
async def admin_ban_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await callback.message.answer("🚫 *Введите ID или @username пользователя, которого нужно забанить:*", reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_ban_user)
    await callback.answer()

@dp.message(AdminStates.waiting_for_ban_user)
async def admin_ban_user_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    text = (message.text or "").strip().lstrip("@")
    target_id = None

    async with aiosqlite.connect(DB_PATH) as db:
        if text.isdigit():
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (int(text),)) as cursor:
                row = await cursor.fetchone()
                if row: target_id = row[0]
        else:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (text,)) as cursor:
                row = await cursor.fetchone()
                if row: target_id = row[0]

        if not target_id:
            await message.answer("❌ Пользователь не найден\\! Попробуйте ещё раз:")
            return

        if target_id == PRIMARY_ADMIN_ID:
            await message.answer("❌ Нельзя забанить главного администратора\\!")
            await state.clear()
            return

        await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,))
        await db.commit()

    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await message.answer(f"✅ *Пользователь `{target_id}` заблокирован в боте\\!*", reply_markup=builder.as_markup())
    await state.clear()

@dp.callback_query(F.data == "admin_unban_user")
async def admin_unban_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await callback.message.answer("✅ *Введите ID или @username пользователя, которого нужно разбанить:*", reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_unban_user)
    await callback.answer()

@dp.message(AdminStates.waiting_for_unban_user)
async def admin_unban_user_process(message: types.Message, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
    text = (message.text or "").strip().lstrip("@")
    target_id = None
    async with aiosqlite.connect(DB_PATH) as db:
        if text.isdigit():
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (int(text),)) as cursor:
                row = await cursor.fetchone()
                if row: target_id = row[0]
        else:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (text,)) as cursor:
                row = await cursor.fetchone()
                if row: target_id = row[0]
        if not target_id:
            await message.answer("❌ Пользователь не найден!")
            return
        await db.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,))
        await db.commit()
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="admin_back_to_panel")
    await message.answer(f"✅ *Пользователь `{target_id}` разблокирован\!*", reply_markup=builder.as_markup())
    await state.clear()

async def daily_mailing_scheduler():
    while True:
        now = datetime.now()
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT user_id FROM users WHERE is_banned = 0") as cursor:
                    users = await cursor.fetchall()
            for u in users:
                try:
                    builder = InlineKeyboardBuilder()
                    builder.button(text="✨ Начать фармить", url=f"https://t.me/{BOT_USERNAME.replace('@', '')}?start=farming")
                    builder.adjust(1)
                    await bot.send_message(
                        chat_id=u[0],
                        text="*Ежедневный бонус ждет вас\! Нажмите кнопку ниже, чтобы начать фармить звёзды\.*",
                        reply_markup=builder.as_markup(),
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Ошибка ежедневной рассылки: {e}")

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    asyncio.create_task(backup_db_loop())
    asyncio.create_task(daily_mailing_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
