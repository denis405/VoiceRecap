"""Bot profile: name, descriptions, command menu and avatar. Applied on startup, only what changed."""

import hashlib
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    FSInputFile,
    InputProfilePhotoStatic,
)

from bot.db import Database

log = logging.getLogger(__name__)

AVATAR = Path(__file__).resolve().parent.parent / "assets" / "avatar.jpg"

# language_code -> texts; None is the fallback for every language.
# Empty string removes a localized override, so everyone sees the Russian text.
NAMES = {
    None: "VoiceRecap — пересказ голосовых",
    "en": "",
}

SHORT_DESCRIPTIONS = {
    None: "Пришлите голосовое или кружочек — верну краткий пересказ и полную расшифровку.",
    "en": "",
}

DESCRIPTIONS = {
    None: (
        "🎙 Превращаю голосовые сообщения и кружочки в короткий текстовый пересказ.\n\n"
        "Что умею:\n"
        "• расшифровываю голосовые и кружочки — свои или пересланные из любого чата\n"
        "• делаю краткий пересказ: суть, ключевые мысли, задачи и сроки\n"
        "• предлагаю готовые варианты ответа — копируются одним тапом\n"
        "• по кнопке присылаю полную расшифровку\n\n"
        "🔒 Бот работает по приглашениям: нажмите «Старт», и администратор получит запрос на доступ."
    ),
    "en": "",
}

USER_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="settings", description="Язык расшифровки"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="users", description="Пользователи и их статусы"),
    BotCommand(command="approve", description="Одобрить: /approve <id>"),
    BotCommand(command="revoke", description="Отключить: /revoke <id>"),
    BotCommand(command="stats", description="Статистика: /stats или /stats <id>"),
    BotCommand(command="stats_csv", description="Выгрузить статистику в CSV"),
]


async def _step(name: str, coro) -> None:
    try:
        await coro
    except TelegramAPIError as e:
        log.warning("Profile setup step %r failed: %s", name, e)


async def _sync_texts(bot: Bot) -> None:
    for lang, name in NAMES.items():
        if (await bot.get_my_name(language_code=lang)).name != name:
            await bot.set_my_name(name=name, language_code=lang)
            log.info("Bot name set (%s)", lang or "default")
    for lang, text in SHORT_DESCRIPTIONS.items():
        if (await bot.get_my_short_description(language_code=lang)).short_description != text:
            await bot.set_my_short_description(short_description=text, language_code=lang)
            log.info("Short description set (%s)", lang or "default")
    for lang, text in DESCRIPTIONS.items():
        if (await bot.get_my_description(language_code=lang)).description != text:
            await bot.set_my_description(description=text, language_code=lang)
            log.info("Description set (%s)", lang or "default")


async def _sync_commands(bot: Bot, admin_id: int) -> None:
    for scope, commands in (
        (BotCommandScopeDefault(), USER_COMMANDS),
        (BotCommandScopeChat(chat_id=admin_id), ADMIN_COMMANDS),
    ):
        current = [(c.command, c.description) for c in await bot.get_my_commands(scope=scope)]
        if current != [(c.command, c.description) for c in commands]:
            await bot.set_my_commands(commands, scope=scope)
            log.info("Commands set for %s", scope.type)


async def _sync_avatar(bot: Bot, db: Database) -> None:
    if not AVATAR.exists():
        return
    digest = hashlib.sha256(AVATAR.read_bytes()).hexdigest()
    if await db.get_meta("avatar_sha256") == digest:
        return
    await bot.set_my_profile_photo(photo=InputProfilePhotoStatic(photo=FSInputFile(AVATAR)))
    await db.set_meta("avatar_sha256", digest)
    log.info("Profile photo uploaded")


async def setup_profile(bot: Bot, db: Database, admin_id: int) -> None:
    await _step("texts", _sync_texts(bot))
    await _step("commands", _sync_commands(bot, admin_id))
    await _step("avatar", _sync_avatar(bot, db))
