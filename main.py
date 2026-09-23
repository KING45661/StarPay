import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

# Замените на токен вашего бота от @BotFather
TOKEN = "8834278881:AAEyZ0y2QjTTcZeUfmGLvGhkDqrP2bAiYoo"

# Инициализация бота и диспетчера
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Хендлер на команду /start
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("Тест")

# Главная функция для запуска поллинга
async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
