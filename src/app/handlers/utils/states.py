from aiogram.filters.state import State, StatesGroup


class Reg(StatesGroup):
    data = State()


class Menu(StatesGroup):
    menu = State()


class Imp(StatesGroup):
    file = State()


class NotionData(StatesGroup):
    token = State()
    link = State()
    choice = State()
