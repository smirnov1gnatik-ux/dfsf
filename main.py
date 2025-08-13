import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

BOT_TOKEN = "7953049696:AAFFB8btvJXptxw4l_0RpkB1nR13FOxlZ5Y"  # вставьте сюда токен от BotFather

dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Я работаю 🤖")

async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
