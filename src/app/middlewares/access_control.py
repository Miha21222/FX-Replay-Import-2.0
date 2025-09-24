from aiogram import BaseMiddleware
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest
from typing import Callable, Dict, Any, Awaitable

class GroupAccessMiddleware(BaseMiddleware):
    def __init__(self, bot, group_id: int):
        self.bot = bot
        self.group_id = group_id

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        user_id = event.from_user.id
        try:
            member = await self.bot.get_chat_member(chat_id=self.group_id, user_id=user_id)
            if member.status not in ("member", "administrator", "creator"):
                await event.answer("❌ Доступ запрещён")
                return
        except TelegramBadRequest:
            await event.answer("⚠️ Не удалось проверить доступ")
            return

        return await handler(event, data)
