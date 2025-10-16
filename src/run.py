import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from dotenv import load_dotenv

import src.app.database.requests as rq
import src.app.handlers.keyboards as kb
from src.app.database.models import init_sqlite_models
from src.app.handlers.imp import imp_rt
from src.app.handlers.menu import menu_rt
from src.app.handlers.notion import ndata_rt
from src.app.handlers.register import register_rt
from src.app.handlers.utils.states import Menu
from src.app.middlewares.access_control import GroupAccessMiddleware

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
dp = Dispatcher()
GROUP_ID = -1002378955126


@dp.message(F.text == '🚫 Отмена')
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Menu.menu)
    await message.answer('⛔ Действие отменено!', reply_markup=kb.menu)


@dp.message(Command("clear"))
async def clear_state(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('✅ Текущее состояние очищено!')


async def main():
    load_dotenv()
    token = os.getenv('BOT_TOKEN')
    bot = Bot(token)
    dp.include_routers(register_rt, menu_rt, ndata_rt, imp_rt)
    dp.startup.register(startup)
    dp.shutdown.register(shutdown)
    dp.message.middleware(GroupAccessMiddleware(bot, GROUP_ID))
    await dp.start_polling(bot)


async def startup(dispatcher: Dispatcher):
    await init_sqlite_models()
    load_dotenv()
    token = os.getenv('BOT_TOKEN')
    bot = Bot(token)
    users = await rq.get_all_users()
    text = '✅ Бот запущен!'
    for user in users:
        await bot.send_message(chat_id=user, text=text)
    logging.info("Starting up...")


async def shutdown(dispatcher: Dispatcher):
    users = await rq.get_all_users()
    load_dotenv()
    token = os.getenv('BOT_TOKEN')
    bot = Bot(token)
    text = '🚫 Бот приостановлен!'
    for user in users:
        await bot.send_message(chat_id=user, text=text)
    logging.info("Shutting down...")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info('Shutdown')
