from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot import texts
from bot.db import APPROVED, PENDING, Database
from bot.handlers.admin import notify_admin_new_user


class AccessMiddleware(BaseMiddleware):
    """Lets through only the admin and approved users; registers newcomers as pending."""

    def __init__(self, db: Database, admin_id: int):
        self.db = db
        self.admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.chat.type != ChatType.PRIVATE:
            return None

        tg_user = event.from_user
        if tg_user is None or tg_user.is_bot:
            return None
        if tg_user.id == self.admin_id:
            return await handler(event, data)

        user = await self.db.get_user(tg_user.id)
        if user is None:
            user = await self.db.add_pending(tg_user.id, tg_user.username, tg_user.full_name)
            await notify_admin_new_user(event.bot, self.admin_id, user)
            await self._reply(event, texts.ACCESS_REQUESTED)
            return None

        if (user.username, user.full_name) != (tg_user.username, tg_user.full_name):
            await self.db.update_profile(tg_user.id, tg_user.username, tg_user.full_name)

        if user.status == APPROVED:
            return await handler(event, data)
        await self._reply(event, texts.ACCESS_PENDING if user.status == PENDING else texts.ACCESS_DENIED)
        return None

    @staticmethod
    async def _reply(event: TelegramObject, text: str) -> None:
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
