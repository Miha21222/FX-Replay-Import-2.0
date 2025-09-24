from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import src.app.handlers.keyboards as kb
from src.app.database.requests import check_user, register_user
from src.app.handlers.utils.states import Reg, Menu

register_rt = Router()


@register_rt.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = await check_user(message.from_user.id)
    user_name = message.from_user.first_name
    if not user:
        await state.set_state(Reg.data)
        await message.answer("👋 Приветствую! Зарегистрируйтесь, нажав на кнопку ниже", reply_markup=kb.reg)
    else:
        await state.set_state(Menu.menu)
        await message.answer(f'👋 Приветствую, {user_name}! Выберите действие по кнопкам ниже', reply_markup=kb.menu)


@register_rt.message(Reg.data)
async def cmd_data(message: Message, state: FSMContext):
    if not message.contact:
        await state.set_state(Reg.data)
        await message.answer('⚠️ Пожалуйста, для регистрации нажмите кнопку ниже!', reply_markup=kb.reg)
    else:
        await state.update_data(phone=message.contact.phone_number)
        await state.update_data(name=message.contact.first_name)
        data = await state.get_data()
        await register_user(message.from_user.id, data['name'], data['phone'])
        await state.set_state(Menu.menu)
        await message.answer(f'✅ Пользователь {data['name']} успешно зарегистрирован!', reply_markup=kb.menu)
