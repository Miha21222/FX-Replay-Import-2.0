from typing import Optional, Tuple

from aiogram import Router, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import src.app.database.requests as rq
import src.app.handlers.keyboards as kb
from aiogram.types.reply_keyboard_remove import ReplyKeyboardRemove
from src.app.handlers.utils.states import Menu, NotionData, Imp
from src.app.handlers.utils.validators import validate_notion_token, validate_notion_url

menu_rt = Router()


# ---------- helpers ----------
async def _get_notion_data(user_id: int) -> Tuple[Optional[str], Optional[str]]:
    data = await rq.get_data(user_id) or ()
    api_key = data[0] if len(data) > 0 else None
    page_link = data[1] if len(data) > 1 else None
    return api_key, page_link


def _validate(api_key: Optional[str], page_link: Optional[str]) -> Tuple[bool, str]:
    ok_token, msg_token = validate_notion_token(api_key or "")
    if not ok_token:
        return False, msg_token
    ok_url, msg_url = validate_notion_url(page_link or "")
    if not ok_url:
        return False, msg_url
    return True, ""


@menu_rt.message(Menu.menu)
async def cmd_menu(msg: Message, state: FSMContext, bot: Bot):
    text = msg.text
    api_key, page_link = await _get_notion_data(msg.from_user.id)
    ok, _ = _validate(api_key, page_link)
    if text == "📋 Задать данные Notion":
        if not api_key or not ok:
            await state.set_state(NotionData.token)
            await msg.answer("🔑 Введите API ключ Notion", reply_markup=kb.cancel)
            return
        await state.set_state(NotionData.choice)
        await msg.answer(
            "❗ Внимание! У вас уже есть добавленные данные Notion! Желаете их обновить?",
            reply_markup=kb.choice,
        )
        return
    elif text == '📝 Начать импорт':
        if not api_key or not ok:
            await msg.answer("⚠️ Не указаны данные Notion!")
            return
        await state.set_state(Imp.file)
        await msg.answer('📄 Пришлите файл со сделками', reply_markup=ReplyKeyboardRemove())
    else:
        await state.set_state(Menu.menu)
        await msg.answer('⚠️ Выберите действие ниже!', reply_markup=kb.menu)
