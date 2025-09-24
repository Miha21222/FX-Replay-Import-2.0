from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import src.app.database.requests as rq
import src.app.handlers.keyboards as kb
from src.app.handlers.utils.states import Menu, NotionData
from src.app.handlers.utils.validators import validate_notion_token, validate_notion_url

ndata_rt = Router()


@ndata_rt.message(NotionData.choice)
async def cmd_choice(message: Message, state: FSMContext):
    if message.text == '❌ Нет':
        await state.set_state(Menu.menu)
        await message.answer('👍 Понял, возвращаю в меню', reply_markup=kb.menu)
    elif message.text == '✅ Да':
        await state.set_state(NotionData.token)
        await message.answer('🔑 Введите API ключ Notion', reply_markup=kb.cancel)


@ndata_rt.message(NotionData.token)
async def cmd_token(message: Message, state: FSMContext):
    ok_token, msg_token = validate_notion_token(message.text)
    if not ok_token:
        await state.set_state(NotionData.token)
        await message.answer(msg_token)
    else:
        await state.update_data(token=message.text)
        await state.set_state(NotionData.link)
        await message.answer('🔗 Введите ссылку на страницу с журналом в Notion', reply_markup=kb.cancel)


@ndata_rt.message(NotionData.link)
async def cmd_link(message: Message, state: FSMContext):
    ok_url, msg_url = validate_notion_url(message.text)
    if not ok_url:
        await state.set_state(NotionData.link)
        await message.answer(msg_url)
    else:
        await state.update_data(link=message.text)
        data = await state.get_data()
        await rq.add_data(message.from_user.id, data['token'], data['link'])
        await state.set_state(Menu.menu)
        await message.answer('✅ Данные Notion успешно добавлены!', reply_markup=kb.menu)
