import asyncio
import logging
import tempfile
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from bot import texts
from bot.audio import to_mp3_chunks
from bot.config import settings
from bot.db import REQ_EMPTY, REQ_OK, REQ_TOO_BIG, REQ_TOO_LONG, Database, Request
from bot.fmt import fmt_duration
from bot.openrouter import OpenRouter

log = logging.getLogger(__name__)

router = Router(name="user")

TG_TEXT_LIMIT = 4000
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024


class FullTextCb(CallbackData, prefix="full"):
    transcript_id: int


class LanguageCb(CallbackData, prefix="lang"):
    code: str  # a key of texts.LANGUAGES


AUTO = "auto"


async def user_language(db: Database, user_id: int) -> str:
    """The user's STT language choice, falling back to STT_LANGUAGE from .env; "auto" = auto-detect."""
    return await db.get_stt_language(user_id) or settings.stt_language or AUTO


def language_name(code: str) -> str:
    return texts.LANGUAGES.get(code, code)


def settings_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=("✅ " if code == current else "") + name,
            callback_data=LanguageCb(code=code).pack(),
        )
        for code, name in texts.LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)])


async def set_status(status: Message, text: str, **kwargs) -> None:
    try:
        await status.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def record_request(db: Database, req: Request) -> None:
    try:
        await db.add_request(req)
    except Exception:
        log.exception("Failed to record usage for %s", req.user_id)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(texts.WELCOME)


@router.message(Command("settings"))
async def cmd_settings(message: Message, db: Database) -> None:
    current = await user_language(db, message.from_user.id)
    await message.answer(
        texts.SETTINGS.format(language=language_name(current)),
        reply_markup=settings_keyboard(current),
    )


@router.callback_query(LanguageCb.filter())
async def on_language(query: CallbackQuery, callback_data: LanguageCb, db: Database) -> None:
    code = callback_data.code
    if code not in texts.LANGUAGES:
        await query.answer()
        return
    await db.set_stt_language(query.from_user.id, code)
    await set_status(
        query.message,
        texts.SETTINGS.format(language=language_name(code)),
        reply_markup=settings_keyboard(code),
    )
    await query.answer(texts.LANGUAGE_SET.format(language=language_name(code)))
    log.info("User %s set STT language: %s", query.from_user.id, code)


@router.message(F.voice | F.video_note)
async def on_voice(message: Message, db: Database, api: OpenRouter, jobs: asyncio.Semaphore) -> None:
    media = message.voice or message.video_note
    duration = media.duration or 0
    req = Request(
        user_id=message.from_user.id,
        kind="voice" if message.voice else "video_note",
        duration_sec=duration,
        file_size=media.file_size,
        stt_model=api.stt_model,
        llm_model=api.summary_model,
    )

    if media.file_size and media.file_size > TG_DOWNLOAD_LIMIT:
        req.status = REQ_TOO_BIG
        await record_request(db, req)
        await message.reply(texts.ERROR_TOO_BIG)
        return
    if duration > settings.max_duration_sec:
        req.status = REQ_TOO_LONG
        await record_request(db, req)
        await message.reply(texts.ERROR_TOO_LONG.format(max_min=settings.max_duration_sec // 60))
        return

    status = await message.reply(texts.STATUS_DOWNLOADING)
    started = time.monotonic()
    # recorded in `finally`, so money spent before a failure is still counted
    try:
        async with jobs, ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                src = tmp_dir / ("voice.ogg" if message.voice else "note.mp4")
                await message.bot.download(media.file_id, destination=src)

                await set_status(status, texts.STATUS_TRANSCRIBING)
                chunks = await to_mp3_chunks(src, tmp_dir, settings.chunk_sec)
                language = await user_language(db, message.from_user.id)
                parts = []
                for chunk in chunks:
                    part = await api.transcribe(chunk, None if language == AUTO else language)
                    req.stt_seconds += part.seconds
                    req.stt_cost += part.cost
                    parts.append(part.text)
            transcript = " ".join(p for p in parts if p).strip()
            req.transcript_chars = len(transcript)

            if not transcript:
                req.status = REQ_EMPTY
                await set_status(status, texts.ERROR_EMPTY)
                return

            await set_status(status, texts.STATUS_SUMMARIZING)
            result = await api.summarize(transcript)
            summary = result.text
            req.summary_chars = len(summary)
            req.llm_prompt_tokens = result.prompt_tokens
            req.llm_completion_tokens = result.completion_tokens
            req.llm_reasoning_tokens = result.reasoning_tokens
            req.llm_cost = result.cost
            transcript_id = await db.save_transcript(message.from_user.id, transcript)
            req.transcript_id = transcript_id
            req.status = REQ_OK
    except Exception:
        log.exception("Failed to process message %s from %s", message.message_id, message.from_user.id)
        await set_status(status, texts.ERROR_GENERIC)
        return
    finally:
        req.processing_ms = int((time.monotonic() - started) * 1000)
        await record_request(db, req)

    header = f"🎙 {fmt_duration(duration)} · 📝 Кратко\n\n"
    body = summary or transcript
    if len(header) + len(body) > TG_TEXT_LIMIT:
        body = body[: TG_TEXT_LIMIT - len(header) - 1] + "…"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=texts.FULL_TEXT_BUTTON,
                callback_data=FullTextCb(transcript_id=transcript_id).pack(),
            )
        ]]
    )
    await set_status(status, header + body, reply_markup=keyboard)
    log.info("Processed %ss voice for %s (%d chars)", duration, message.from_user.id, len(transcript))


@router.callback_query(FullTextCb.filter())
async def on_full_text(query: CallbackQuery, callback_data: FullTextCb, db: Database) -> None:
    transcript = await db.get_transcript(callback_data.transcript_id, query.from_user.id)
    if transcript is None:
        await query.answer(texts.TRANSCRIPT_NOT_FOUND, show_alert=True)
        return
    await query.answer()

    if len(transcript) <= TG_TEXT_LIMIT:
        await query.message.reply(transcript)
    else:
        await query.message.reply_document(
            BufferedInputFile(transcript.encode(), filename=f"transcript_{callback_data.transcript_id}.txt"),
            caption="📄 Полная расшифровка",
        )
    # one-shot button: drop it once the full text is out
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(texts.NOT_VOICE)
