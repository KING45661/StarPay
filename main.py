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

# ==================== ВОССТАНОВЛЕНИЕ ИЗ БЭКАПА ====================
def restore_from_backup():
    """Восстанавливает БД из последнего бэкапа если основной файл потерян или пуст"""
    db_path = Path("/app/data/bot_database.db")
    backup_dir = Path("/app/backups")
    
    # Если БД существует и имеет размер > 0, всё ОК
    if db_path.exists() and db_path.stat().st_size > 0:
        logging.info("✅ БД найдена и содержит данные")
        return
    
    # БД потеряна или пуста — ищем последний бэкап
    if backup_dir.exists():
        backups = sorted(backup_dir.glob("bot_database_*.db"), reverse=True)
        if backups:
            latest_backup = backups[0]
            logging.info(f"🔄 БД потеряна! Восстанавливаю из бэкапа: {latest_backup.name}")
            shutil.copy(latest_backup, db_path)
            logging.info(f"✅ БД восстановлена из бэкапа: {db_path}")
            return
    
    logging.warning("⚠️ Бэкапов не найдено, создаётся новая БД")
    
# ==================== НАСТРОЙКИ ====================
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
# Читает путь из переменных Railway. Если её нет (локально) — использует bot_database.db
DB_PATH = os.getenv("DB_PATH", "/app/data/bot_database.db")
WITHDRAWS_PER_PAGE = 10
TOP_USERS_LIMIT = 10
DAILY_BONUS_MIN = 0.1
DAILY_BONUS_MAX = 1.0
DAILY_BONUS_STEP = 0.1

GIFT_TEXT_PRICE = 0.5
GIFT_TEXT_MAX_LEN = 100

CAPTCHA_FRUITS = [
    ("🥭", "манго"),
    ("🍎", "яблоко"),
    ("🍋", "лимон"),
    ("🍑", "персик"),
    ("🍇", "виноград"),
    ("🍓", "клубнику")
]

MENU_BUTTONS = ["💎 Задания", "🎁 Вывести Звёзды", "📅 Ежедневный бонус", "👤 Профиль", "👥 Друзья", "👑 Админ-панель"]

def escape_md(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'([_ *\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

def random_daily_bonus() -> float:
    steps = round((DAILY_BONUS_MAX - DAILY_BONUS_MIN) / DAILY_BONUS_STEP)
    chosen_step = random.randint(0, steps)
    return round(DAILY_BONUS_MIN + chosen_step * DAILY_BONUS_STEP, 1)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2)
)
dp = Dispatcher(storage=MemoryStorage())

# ==================== ХЕЛПЕР ЛОГИРОВАНИЯ ====================
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
    """Записывает положительное начисление в лог для расчёта топов по периодам.
    source: 'task' | 'referral' | 'daily' | 'promo' | 'check' | 'admin'
    Отрицательные изменения (штрафы, выводы) сюда не пишутся — топы считают заработок, не текущий баланс."""
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

# ==================== БЭКАП БАЗЫ ====================
async def backup_db_loop():
    backup_dir = Path("backups")
    backup_dir.mkdir(exist_ok=True)
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

# ==================== БАЗА ДАННЫХ ====================
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        for col_def in [
            "referrer_id INTEGER DEFAULT NULL",
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "last_daily TIMESTAMP DEFAULT NULL"
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

        # Промокоды: множественные, с лимитом активаций и суммой
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY,
                amount REAL,
                max_activations INTEGER,
                used_activations INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Кто уже активировал какой промокод — заменяет старое поле used_promo (было только для одного "NEW")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_activations (
                promo_code TEXT,
                user_id INTEGER,
                activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (promo_code, user_id)
            )
        """)

        # Лог начислений для расчёта топов "за день/неделю/всё время"
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

async def get_ref_reward() -> float:
    return float(await get_setting("ref_reward", "5.0"))

# ==================== FSM ====================
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

class UserStates(StatesGroup):
    waiting_for_promo = State()
    waiting_for_gift_text = State()

# ==================== КЛАВИАТУРЫ ====================
async def main_keyboard(user_id: int):
    builder = ReplyKeyboardBuilder()
    builder.button(text="💎 Задания")
    builder.button(text="👥 Друзья")
    builder.button(text="🎁 Вывести Звёзды")
    builder.button(text="📅 Ежедневный бонус")
    builder.button(text="👤 Профиль")
    if await is_admin(user_id):
        builder.button(text="👑 Админ-панель")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup(resize_keyboard=True)

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="🏆 Топ пользователей", callback_data="admin_top_users")
    builder.button(text="💰 Баланс пользователя", callback_data="admin_change_balance")
    builder.button(text="⏳ Заявки на вывод", callback_data="admin_pending_w_page:0")
    builder.button(text="🎟 Промокоды", callback_data="admin_promo_menu")
    builder.button(text="🎟 Создать чек", callback_data="admin_create_check")
    builder.button(text="➕ Добавить канал", callback_data="admin_add_channel")
    builder.button(text="📋 Список каналов", callback_data="admin_list_channels")
    builder.button(text="📢 Рассылка", callback_data="admin_broadcast")
    builder.button(text="🖼 Изменить баннер", callback_data="admin_set_photo")
    builder.button(text="⚙️ Настройка рефки", callback_data="admin_set_ref_reward")
    builder.button(text="🏆 Топы: вкл/выкл", callback_data="admin_toggle_top")
    builder.button(text="➕ Добавить админа", callback_data="admin_add_admin")
    builder.button(text="👥 Список админов", callback_data="admin_list_admins")
    builder.adjust(2, 2, 2, 2, 2, 2, 2)
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

# ==================== КАПЧА ====================
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_passed_captcha = 1 WHERE user_id = ?", (callback.from_user.id,))
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

# ==================== СТАРТ ====================
async def show_start_screen(message: types.Message, user: types.User):
    first_name_esc = escape_md(user.first_name)
    welcome_text = (
        f"Привет, *{first_name_esc}* 👏\n\n"
        "_Приглашай друзей и зарабатывай звёзды\._\n"
        "_Копи и выводи подарками Telegram\._"
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
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            is_new = await cursor.fetchone() is None

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
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (ref_reward, referrer_id))
            await log_balance_change(db, referrer_id, ref_reward, "referral")
            try:
                ref_reward_esc = escape_md(f"{ref_reward}")
                await bot.send_message(
                    referrer_id,
                    f"🎉 *По вашей ссылке зарегистрировался новый друг\! Вам начислено \+`{ref_reward_esc}` 💫*"
                )
            except Exception: pass

        await db.commit()

        async with db.execute("SELECT is_passed_captcha FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            is_passed = row[0] if row else 0

    if not is_passed:
        await send_captcha(message)
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

# ==================== ЕЖЕДНЕВНЫЙ БОНУС ====================
@dp.message(F.text == "📅 Ежедневный бонус")
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

# ==================== ДРУЗЬЯ ====================
@dp.message(F.text == "👥 Друзья")
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
        f"🎉 *Приглашай по этой ссылке своих друзей, отправляй её во все чаты и зарабатывай звёзды\!*\n\n"
        f"Приглашено тобой: *{invited_esc}* 👤"
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="💞 Отправить Ссылку Друзьям",
        switch_inline_query=f"\n🚀 Зарабатывай бесплатные звёзды Telegram со мной! Ссылка: {ref_link}"
    )

    await message.answer(text, reply_markup=builder.as_markup())

# ==================== ЗАДАНИЯ ====================
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

@dp.message(F.text == "💎 Задания")
async def earn_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await send_next_task(message, message.from_user.id)

@dp.callback_query(F.data.startswith("check_sub:"))
async def check_sub_handler(callback: types.CallbackQuery):
    _, ch_db_id, ch_id = callback.data.split(":")
    user_id = callback.from_user.id

    try:
        member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT completed_tasks, COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    completed = row[0].split(",") if row and row[0] else []
                    balance = float(row[1]) if row else 0.0

                if str(ch_db_id) in completed:
                    await callback.answer("❌ Вы уже получили награду за это задание!", show_alert=True)
                    return

                completed.append(str(ch_db_id))
                new_completed_str = ",".join(completed)
                new_balance = balance + REWARD_PER_SUB

                await db.execute(
                    "UPDATE users SET balance = ?, completed_tasks = ? WHERE user_id = ?",
                    (new_balance, new_completed_str, user_id)
                )
                await log_balance_change(db, user_id, REWARD_PER_SUB, "task")
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

# ==================== ПРОФИЛЬ ====================
@dp.message(F.text == "👤 Профиль")
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
        "*Баланс*\n"
        f"└ Обычный: `{bal_str}` ⭐\n\n"
        "*Выводы*\n"
        f"├ Ожидают: `{pend_str}` ⭐\n"
        f"└ Выведено: `{comp_str}` ⭐"
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
        "*Баланс*\n"
        f"└ Обычный: `{bal_str}` ⭐\n\n"
        "*Выводы*\n"
        f"├ Ожидают: `{pend_str}` ⭐\n"
        f"└ Выведено: `{comp_str}` ⭐"
    )
    try:
        await callback.message.edit_text(profile_text, reply_markup=await profile_keyboard())
    except Exception:
        await callback.message.answer(profile_text, reply_markup=await profile_keyboard())
    await callback.answer()

# ==================== ТОП ПОЛЬЗОВАТЕЛЕЙ (профиль) ====================
async def build_top_text(period: str) -> str:
    """period: 'day' | 'week' | 'all'"""
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
    text = header
    for idx, (u_id, u_name, val) in enumerate(top_rows, start=1):
        display_name = escape_md(u_name) if u_name else f"ID{u_id}"
        val_esc = escape_md(f"{val:.1f}")
        prefix = medals[idx - 1] if idx <= 3 else f"{idx}\."
        text += f"{prefix} {display_name} — {val_esc} ⭐\n"

    return text

@dp.callback_query(F.data.startswith("show_top:"))
async def show_top_handler(callback: types.CallbackQuery):
    if not await is_top_enabled():
        await callback.answer("❌ Топ пользователей сейчас отключён администратором.", show_alert=True)
        return

    parts = callback.data.split(":")
    period = parts[1]

    text = await build_top_text(period)
    try:
        await callback.message.edit_text(text, reply_markup=top_period_keyboard(period))
    except Exception:
        await callback.message.answer(text, reply_markup=top_period_keyboard(period))
    await callback.answer()

# ==================== ПРОМОКОДЫ (пользователь) ====================
@dp.callback_query(F.data == "activate_promo")
async def promo_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("🎟 *Введите промокод:*")
    await state.set_state(UserStates.waiting_for_promo)
    await callback.answer()

@dp.message(UserStates.waiting_for_promo)
async def promo_process(message: types.Message, state: FSMContext):
    if message.text in MENU_BUTTONS:
        await state.clear()
        if message.text == "👑 Админ-панель" and await is_admin(message.from_user.id):
            await admin_panel(message, state)
        elif message.text == "🎁 Вывести Звёзды":
            await withdraw_cmd(message, state)
        elif message.text == "👤 Профиль":
            await profile_cmd(message, state)
        elif message.text == "👥 Друзья":
            await friends_cmd(message, state)
        elif message.text == "💎 Задания":
            await earn_cmd(message, state)
        elif message.text == "📅 Ежедневный бонус":
            await daily_bonus_cmd(message, state)
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

        # Гарантируем, что пользователь есть в таблице users, и коммитим это ДО обновления баланса
        await db.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, 0.0) ON CONFLICT(user_id) DO NOTHING", (user_id,)
        )
        await db.commit()

        # Начисляем баланс и фиксируем активацию промокода
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.execute(
            "INSERT INTO promo_activations (promo_code, user_id) VALUES (?, ?)", (code, user_id)
        )
        await db.execute(
            "UPDATE promocodes SET used_activations = used_activations + 1 WHERE code = ?", (code,)
        )
        await log_balance_change(db, user_id, amount, "promo")
        # ВАЖНО: коммит сразу после UPDATE баланса, до чтения нового значения
        await db.commit()

        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            new_balance = row[0] if row else 0.0

    amount_val = int(amount) if amount.is_integer() else amount
    amount_esc = escape_md(f"{amount_val}")
    bal_esc = escape_md(f"{new_balance:.2f}")
    await message.answer(f"🎉 *Промокод успешно активирован\! Начислено \+{amount_esc} ⭐*\nНовый баланс: `{bal_esc}` ⭐")

    await state.clear()

# ==================== ВЫВОД ====================
@dp.message(F.text == "🎁 Вывести Звёзды")
async def withdraw_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(balance, 0.0) FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            balance = float(row[0]) if row else 0.0

    bal_esc = escape_md(f"{balance:.2f}")
    min_esc = escape_md(str(int(MIN_WITHDRAW)))
    text = (
        "💸 *Вывод звёзд*\n\n"
        f"├ Баланс: `{bal_esc}` ⭐\n\n"
        "Выберите подарок для вывода:\n\n"
        "*Условия вывода:*\n"
        f"> • Минимальная сумма: *{min_esc}* 💫"
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
        f"Стоимость: *\+{price_esc}* ⭐\n"
        "Напишите текст, который будет написан в профиле подарка 💫"
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
    # Возврат к вопросу "добавить надпись?", не сбрасывая pending_amount
    data = await state.get_data()
    await state.set_state(None)
    await state.set_data(data)

    price_esc = escape_md(str(GIFT_TEXT_PRICE))
    text = (
        "✍️ *Добавить надпись на подарок?*\n\n"
        f"Стоимость: *\+{price_esc}* ⭐\n"
        "Напишите текст, который будет написан в профиле подарка 💫"
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
    """event может быть types.CallbackQuery (пропуск надписи) или types.Message (ввод текста надписи)."""
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

    request_log = (
        "🧾 *Новая заявка\\!*\n\n"
        f"👤 {user_mention_esc}\n"
        f"⏳ {amount_esc} ⭐ ожидает подтверждения"
        f"{gift_text_line}\n\n"
        f"[{escape_md(BOT_USERNAME)}]({bot_link_esc})"
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
    gift_log_line = f"\n✍️ Надпись: _{escape_md(gift_text)}_" if gift_text else ""

    payout_log = (
        "🧾 *Новая выплата\\!*\n\n"
        f"👤 {user_mention_esc}\n"
        f"✅ {amt_esc} ⭐ успешно выведено"
        f"{gift_log_line}\n\n"
        f"[{escape_md(BOT_USERNAME)}]({bot_link_esc})"
    )
    await send_log(payout_log)

    gift_status_line = f"\n✍️ Надпись: _{escape_md(gift_text)}_" if gift_text else ""
    status_admin_msg = (
        "💸 *Новая заявка на вывод\\!*\n\n"
        f"🆔 Заявка: \\#{w_id}\n"
        f"👤 Пользователь: {user_mention_esc}\n"
        f"💰 Сумма: *{amt_esc} ⭐*"
        f"{gift_status_line}\n\n"
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
        await bot.send_message(u_id, f"❌ Ваша заявка #{w_id} на {int(amt)} ⭐ была отклонена. Средства возвращены на баланс.", parse_mode=None)
    except Exception: pass

    user_mention = f"@{u_name}" if u_name else f"ID: {u_id}"
    user_mention_esc = escape_md(user_mention)
    amt_esc = escape_md(str(int(amt)))
    gift_status_line = f"\n✍️ Надпись: _{escape_md(gift_text)}_" if gift_text else ""

    status_admin_msg = (
        "💸 *Новая заявка на вывод\\!*\n\n"
        f"🆔 Заявка: \\#{w_id}\n"
        f"👤 Пользователь: {user_mention_esc}\n"
        f"💰 Сумма: *{amt_esc} ⭐*"
        f"{gift_status_line}\n\n"
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

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.message(F.text == "👑 Админ-панель")
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
        f"👥 Пользователей: *{total_users}*\n"
        f"⏳ Заявок в ожидании: *{pending_withdraws}*\n\n"
        "Выберите раздел:"
    )
    await message.answer(text, reply_markup=admin_keyboard())

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
        f"👥 Пользователей: *{total_users}*\n"
        f"⏳ Заявок в ожидании: *{pending_withdraws}*\n\n"
        "Выберите раздел:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=admin_keyboard())
    await callback.answer()

# --- Топ: вкл/выкл ---
@dp.callback_query(F.data == "admin_toggle_top")
async def admin_toggle_top_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    current = await is_top_enabled()
    new_value = "0" if current else "1"
    await set_setting("top_enabled", new_value)

    status_text = "включена ✅" if new_value == "1" else "отключена ❌"
    await callback.answer(f"Кнопка топов теперь: {status_text}", show_alert=True)

# --- Статистика ---
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
        f"├ Всего: *{total_users}*\n"
        f"├ За 24ч: *{new_users_24h}*\n"
        f"└ За 7 дней: *{new_users_7d}*\n\n"
        "*💰 Финансы*\n"
        f"├ Суммарный баланс: *{escape_md(f'{total_balance:.2f}')}* ⭐\n"
        f"├ Всего выплачено: *{escape_md(f'{total_paid_out:.2f}')}* ⭐\n"
        f"└ Заявок ожидают: *{pending_withdraws}*\n\n"
        "*📢 Каналы и промокоды*\n"
        f"├ Каналов подключено: *{total_channels}*\n"
        f"└ Промокодов создано: *{total_promos}*"
    )
    await callback.message.answer(stats_text)
    await callback.answer()

# --- Баланс пользователя ---
@dp.callback_query(F.data == "admin_change_balance")
async def admin_change_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await callback.message.answer(
        "👤 *Введите ID или @username пользователя для изменения баланса:*\n\n"
        "⚠️ Пользователь должен хотя бы раз запустить бота \(/start\), иначе он не будет найден в базе\."
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
        # состояние НЕ сбрасываем, чтобы админ мог сразу ввести данные заново
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
    builder.adjust(3)

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

    await callback.message.answer(prompts[action])
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
    await message.answer(f"✅ *Баланс обновлён\!*\n\n👤 Пользователь: `{target_id}`\n💰 Новый баланс: `{bal_esc}` ⭐")
    await state.clear()

# --- Заявки на вывод ---
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
        if callback.message.text:
            await callback.message.edit_text(msg_text)
        else:
            await callback.message.answer(msg_text)
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

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup() if nav_buttons else None)
    except Exception:
        await callback.message.answer(text, reply_markup=builder.as_markup() if nav_buttons else None)

    await callback.answer()

# --- Рассылка ---
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return

    text = "📢 *Отправьте сообщение \(текст или фото\) для рассылки всем пользователям:*"
    await callback.message.answer(text)
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

    await message.answer(f"🚀 *Рассылка запущена\.\.\.*\n👥 Получателей: *{len(users)}*")
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
                logging.warning(f"Flood control, ждём {e.retry_after} сек")
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramForbiddenError:
                failed += 1
                break
            except Exception:
                failed += 1
                break

    await message.answer(
        "✅ *Рассылка завершена\!*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🟢 Успешно: *{success}*\n"
        f"🔴 Ошибок: *{failed}*"
    )
    await state.clear()

# --- Настройка рефки ---
@dp.callback_query(F.data == "admin_set_ref_reward")
async def admin_set_ref_reward_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    cur_rew = await get_ref_reward()
    cur_esc = escape_md(str(cur_rew))
    text = (
        "⚙️ *Настройка реферальной награды*\n\n"
        f"Текущая награда за друга: `{cur_esc}` ⭐\n\n"
        "Введите новое количество звёзд за приглашенного друга:"
    )
    await callback.message.answer(text)
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
    await message.answer(f"✅ *Награда за приглашенного друга изменена на `{val_esc}` ⭐\!*")
    await state.clear()

# --- Добавление админа ---
@dp.callback_query(F.data == "admin_add_admin")
async def admin_add_admin_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    msg = escape_md("👤 Пришлите @username или ID пользователя, которому хотите выдать админ-права:")
    await callback.message.answer(f"*{msg}*")
    await state.set_state(AdminStates.waiting_for_new_admin)
    await callback.answer()

@dp.message(AdminStates.waiting_for_new_admin)
async def admin_add_admin_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
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
                if row: target_id = row[0]

    if not target_id:
        await message.answer("❌ Пользователь не найден в базе данных бота. Он должен хотя бы раз написать боту.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (target_id,))
        await db.commit()

    await message.answer(f"✅ *Пользователю `{target_id}` успешно выданы права администратора\!*")
    await state.clear()

# --- Список админов ---
@dp.callback_query(F.data == "admin_list_admins")
async def admin_list_admins(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT a.user_id, u.username FROM admins a LEFT JOIN users u ON a.user_id = u.user_id") as cursor:
            admins = await cursor.fetchall()

    if not admins:
        await callback.message.answer("Список админов пуст.")
        return

    text = "👥 *Список администраторов*\n━━━━━━━━━━━━━━━━━━\n\n"
    builder = InlineKeyboardBuilder()

    for adm_id, username in admins:
        u_str = f"@{escape_md(username)}" if username else "Без юзернейма"
        adm_id_esc = escape_md(str(adm_id))

        if adm_id == PRIMARY_ADMIN_ID:
            text += f"👑 {u_str} \(ID: `{adm_id_esc}`\) — *Главный админ*\n"
        else:
            text += f"👤 {u_str} \(ID: `{adm_id_esc}`\)\n"
            builder.button(text=f"❌ Снять ID {adm_id}", callback_data=f"remove_admin:{adm_id}")

    builder.adjust(1)
    await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("remove_admin:"))
async def remove_admin_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    target_id = int(callback.data.split(":")[1])

    if target_id == PRIMARY_ADMIN_ID:
        await callback.answer("❌ Нельзя снять главного администратора!", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (target_id,))
        await db.commit()

    await callback.answer("Администратор удалён!", show_alert=True)
    await callback.message.edit_text(f"✅ *Пользователь `{target_id}` был лишён админ\-прав\.*")

# --- Топ пользователей (админ) ---
@dp.callback_query(F.data == "admin_top_users")
async def admin_top_users(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT 10") as cursor:
            top_users = await cursor.fetchall()

    if not top_users:
        await callback.message.answer("Список пользователей пуст.")
        return

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 *ТОП\-10 Пользователей по звёздам*\n━━━━━━━━━━━━━━━━━━\n\n"
    for idx, (u_id, u_name, bal) in enumerate(top_users, start=1):
        username_str = f"@{escape_md(u_name)}" if u_name else "Без юзернейма"
        id_str = escape_md(str(u_id))
        bal_esc = escape_md(f"{bal:.2f}")
        prefix = medals[idx - 1] if idx <= 3 else f"*{idx}\.*"
        text += f"{prefix} {username_str} \(ID: `{id_str}`\) — *{bal_esc}* ⭐\n"

    await callback.message.answer(text)
    await callback.answer()

# --- Промокоды: меню ---
@dp.callback_query(F.data == "admin_promo_menu")
async def admin_promo_menu(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await state.clear()
    text = "🎟 *Управление промокодами*\n\nВыберите действие:"
    try:
        await callback.message.edit_text(text, reply_markup=promo_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=promo_menu_keyboard())
    await callback.answer()

# --- Промокоды: создание ---
@dp.callback_query(F.data == "promo_create_start")
async def promo_create_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await callback.message.answer("🎟 *Введите название промокода* \(например: `NEWYEAR2026`\):")
    await state.set_state(AdminStates.waiting_for_promo_code)
    await callback.answer()

@dp.message(AdminStates.waiting_for_promo_code)
async def promo_create_code_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    code = message.text.strip().upper()
    if not code or " " in code:
        await message.answer("❌ Промокод не должен содержать пробелов. Введите ещё раз:")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM promocodes WHERE code = ?", (code,)) as cursor:
            exists = await cursor.fetchone() is not None

    if exists:
        await message.answer("❌ Такой промокод уже существует\\! Введите другое название:")
        return

    await state.update_data(new_promo_code=code)
    await message.answer("💰 *Сколько звёзд будет начисляться за активацию?*")
    await state.set_state(AdminStates.waiting_for_promo_amount)

@dp.message(AdminStates.waiting_for_promo_amount)
async def promo_create_amount_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Введите корректное положительное число.")
        return

    await state.update_data(new_promo_amount=amount)
    await message.answer(
        "🔢 *Сколько раз можно активировать этот промокод?*\n\n"
        "Введите число, либо `0` для безлимитных активаций\."
    )
    await state.set_state(AdminStates.waiting_for_promo_limit)

@dp.message(AdminStates.waiting_for_promo_limit)
async def promo_create_limit_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    try:
        limit = int(message.text.strip())
        if limit < 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Введите целое число (0 или больше).")
        return

    data = await state.get_data()
    code = data.get("new_promo_code")
    amount = data.get("new_promo_amount")
    max_activations = None if limit == 0 else limit

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO promocodes (code, amount, max_activations) VALUES (?, ?, ?)",
            (code, amount, max_activations)
        )
        await db.commit()

    amount_esc = escape_md(str(amount))
    limit_text = "Безлимит ♾️" if max_activations is None else str(max_activations)
    limit_esc = escape_md(limit_text)
    code_esc = escape_md(code)

    await message.answer(
        "✅ *Промокод создан\!*\n\n"
        f"🎟 Код: `{code_esc}`\n"
        f"💰 Начисление: *{amount_esc}* ⭐\n"
        f"🔢 Лимит активаций: *{limit_esc}*"
    )
    await state.clear()

# --- Промокоды: список ---
@dp.callback_query(F.data == "promo_list")
async def promo_list_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT code, amount, max_activations, used_activations FROM promocodes ORDER BY created_at DESC"
        ) as cursor:
            promos = await cursor.fetchall()

    if not promos:
        text = "📋 *Промокодов пока нет\.*"
        builder = InlineKeyboardBuilder()
        builder.button(text="← Назад", callback_data="admin_promo_menu")
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=builder.as_markup())
        await callback.answer()
        return

    text_lines = ["🎟 *Список промокодов*", "━━━━━━━━━━━━━━━━━━\n"]
    builder = InlineKeyboardBuilder()

    for code, amount, max_act, used_act in promos:
        code_esc = escape_md(code)
        amount_esc = escape_md(str(amount))
        limit_text = "♾️" if max_act is None else f"{used_act}/{max_act}"
        limit_esc = escape_md(limit_text)
        text_lines.append(f"🎟 `{code_esc}` — *{amount_esc}* ⭐ \(исп\.: {limit_esc}\)")
        builder.button(text=f"❌ Удалить {code}", callback_data=f"promo_delete:{code}")

    builder.button(text="← Назад", callback_data="admin_promo_menu")
    builder.adjust(1)

    text = "\n".join(text_lines)
    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("promo_delete:"))
async def promo_delete_handler(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    code = callback.data.split(":", 1)[1]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM promocodes WHERE code = ?", (code,))
        await db.execute("DELETE FROM promo_activations WHERE promo_code = ?", (code,))
        await db.commit()

    await callback.answer(f"Промокод {code} удалён!", show_alert=True)
    await promo_list_handler(callback)

# --- Создание чека ---
@dp.callback_query(F.data == "admin_create_check")
async def admin_create_check_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await callback.message.answer("🎟 *Введите количество звёзд для чека:*")
    await state.set_state(AdminStates.waiting_for_check_amount)
    await callback.answer()

@dp.message(AdminStates.waiting_for_check_amount)
async def admin_create_check_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Введите корректное число звезд.")
        return

    check_code = secrets.token_hex(4)
    creator_id = message.from_user.id
    welcome_photo = None

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'welcome_photo'") as cursor:
            row = await cursor.fetchone()
            if row: welcome_photo = row[0]

    bot_info = await bot.get_me()
    check_link = f"https://t.me/{bot_info.username}?start=check_{check_code}"

    amount_val = int(amount) if amount.is_integer() else amount
    amount_esc = escape_md(f"{amount_val}")
    caption_text = f"🧸 *Чек на `{amount_esc}` Telegram Stars*"

    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Получить", url=check_link)

    sent_msg = None
    if welcome_photo:
        try:
            sent_msg = await message.answer_photo(
                photo=welcome_photo,
                caption=caption_text,
                reply_markup=builder.as_markup()
            )
        except Exception:
            sent_msg = await message.answer(caption_text, reply_markup=builder.as_markup())
    else:
        sent_msg = await message.answer(caption_text, reply_markup=builder.as_markup())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO checks (code, creator_id, amount, chat_id, msg_id) VALUES (?, ?, ?, ?, ?)",
            (check_code, creator_id, amount, sent_msg.chat.id if sent_msg else None, sent_msg.message_id if sent_msg else None)
        )
        await db.commit()

    await state.clear()

# --- Фоновое фото ---
@dp.callback_query(F.data == "admin_set_photo")
async def admin_set_photo_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await callback.message.answer("📸 *Отправьте изображение*, которое будет отображаться на баннере бота и на чеках\.")
    await state.set_state(AdminStates.waiting_for_welcome_photo)

@dp.message(AdminStates.waiting_for_welcome_photo, F.photo)
async def admin_set_photo_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    photo_id = message.photo[-1].file_id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('welcome_photo', ?)", (photo_id,))
        await db.commit()
    await message.answer("✅ *Фото баннера и чеков успешно обновлено\!*")
    await state.clear()

# --- Канал ---
@dp.callback_query(F.data == "admin_add_channel")
async def add_channel_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    text = (
        "➕ *Добавление канала*\n\n"
        "Пришлите юзернейм канала \(например: `@mychannel`\) или его ID \(например: `-100123456789`\):\n\n"
        "⚠️ *Важно:* Бот должен быть предварительно добавлен в этот канал в качестве *Администратора*\!"
    )
    await callback.message.answer(text)
    await state.set_state(AdminStates.waiting_for_channel)

@dp.message(AdminStates.waiting_for_channel)
async def add_channel_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    if message.text in MENU_BUTTONS:
        await state.clear()
        return

    input_text = message.text.strip()

    if input_text.startswith("-100") or input_text.lstrip('-').isdigit():
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

        title_esc = escape_md(title)
        link_esc = escape_md(link)
        await message.answer(f"✅ *Канал успешно добавлен\!*\n📌 Название: {title_esc}\n🔗 Ссылка: {link_esc}")
        await state.clear()
    except Exception as e:
        err_esc = escape_md(str(e))
        await message.answer(f"❌ *Ошибка добавления\!*\n\nПричина: `{err_esc}`\n\nУбедитесь, что:\n1\. Бот добавлен в этот канал *Администратором*\.\n2\. Вы указали правильный `@username` или `ID` канала\.")

@dp.callback_query(F.data == "admin_list_channels")
async def list_channels(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, title FROM channels") as cursor:
            channels = await cursor.fetchall()

    if not channels:
        msg_text = "📋 *Список каналов пуст\.*"
        if callback.message.text:
            await callback.message.edit_text(msg_text)
        else:
            await callback.message.answer(msg_text)
        await callback.answer()
        return

    text = f"📋 *Активные каналы* \(всего: {len(channels)}\)\n━━━━━━━━━━━━━━━━━━\n\n"
    builder = InlineKeyboardBuilder()
    for ch in channels:
        ch_title_esc = escape_md(ch[1])
        text += f"• \\#{ch[0]} — *{ch_title_esc}*\n"
        builder.button(text=f"❌ Удалить #{ch[0]}", callback_data=f"del_ch:{ch[0]}")

    builder.adjust(2)
    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("del_ch:"))
async def delete_channel(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id): return
    ch_db_id = callback.data.split(":")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channels WHERE id = ?", (ch_db_id,))
        await db.commit()

    await callback.answer("Канал удалён!")
    await callback.message.edit_text("✅ *Канал успешно удалён из системы\.*")

# ==================== ВОССТАНОВЛЕНИЕ ИЗ БЭКАПА ====================
def restore_from_backup():
    """Восстанавливает БД из последнего бэкапа если основной файл потерян или пуст"""
    db_path = Path("/app/data/bot_database.db")
    backup_dir = Path("/app/backups")
    
    # Если БД существует и имеет размер > 0, всё ОК
    if db_path.exists() and db_path.stat().st_size > 0:
        logging.info("✅ БД найдена и содержит данные")
        return
    
    # БД потеряна или пуста — ищем последний бэкап
    if backup_dir.exists():
        backups = sorted(backup_dir.glob("bot_database_*.db"), reverse=True)
        if backups:
            latest_backup = backups[0]
            logging.info(f"🔄 БД потеряна! Восстанавливаю из бэкапа: {latest_backup.name}")
            shutil.copy(latest_backup, db_path)
            logging.info(f"✅ БД восстановлена из бэкапа: {db_path}")
            return
    
    logging.warning("⚠️ Бэкапов не найдено, создаётся новая БД")

# ==================== ЗАПУСК ====================
async def main():
    restore_from_backup()
    await init_db()
    asyncio.create_task(backup_db_loop())
    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logging.error(f"Критическая ошибка polling, перезапуск через 5 сек: {e}")
            await asyncio.sleep(5)
            continue
        else:
            break

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    asyncio.run(main())
