from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder

reg = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='⌨️ Зарегистрироваться', request_contact=True)]],
                          resize_keyboard=True)

menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text='📝 Начать импорт'), KeyboardButton(text='📋 Задать данные Notion')]],
    resize_keyboard=True)

cancel = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text='🚫 Отмена')]],
    resize_keyboard=True, one_time_keyboard=True
)

choice = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text='✅ Да'), KeyboardButton(text='❌ Нет')]],
    resize_keyboard=True, one_time_keyboard=True
)
