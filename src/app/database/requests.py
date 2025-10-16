from sqlalchemy import select, update

import src.app.database.models as db
from src.app.database.models import User


async def check_user(tg_id):
    async with db.async_sqlite_session() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id))

        if user:
            return True
        else:
            return False


async def get_all_users():
    async with db.async_sqlite_session() as session:
        result = await session.scalars(select(User.tg_id))
        users = result.all()  # получаем список всех пользователей
        return users


async def register_user(tg_id, name, phone):
    async with db.async_sqlite_session() as session:
        session.add(User(tg_id=tg_id, name=name, phone_number=phone))
        await session.commit()


async def update_user(tg_id, notion_token, page_link):
    async with db.async_sqlite_session() as session:
        await session.execute(
            update(User).where(User.tg_id == tg_id).values(notion_token=notion_token, page_link=page_link))
        await session.commit()


async def get_data(tg_id):
    async with db.async_sqlite_session() as session:
        result = await session.execute(
            select(User.notion_token, User.page_link).where(User.tg_id == tg_id)
        )
        row = result.first()
        if row:
            return row
        else:
            return False


async def add_data(tg_id, token, link):
    async with db.async_sqlite_session() as session:
        await session.execute(update(User).where(User.tg_id == tg_id).values(notion_token=token, page_link=link))
        await session.commit()
