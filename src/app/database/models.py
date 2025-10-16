import datetime

from sqlalchemy import String, BigInteger, Date
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

psql_engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/db_name", echo=True)
# Для докера 🔽
# sqlite_engine = create_async_engine("sqlite+aiosqlite:////src/users.db", echo=True)
# Для локального запуска 🔽
sqlite_engine = create_async_engine("sqlite+aiosqlite:///src/users.db", echo=True)

async_sqlite_session = async_sessionmaker(sqlite_engine)
async_psql_session = async_sessionmaker(psql_engine)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(String)
    phone_number: Mapped[str] = mapped_column(String)
    notion_token: Mapped[str] = mapped_column(String, nullable=True)
    page_link: Mapped[str] = mapped_column(String, nullable=True)
    register_date = mapped_column(Date, default=datetime.date.today())


async def init_sqlite_models():
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def init_psql_models():
    async with psql_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
