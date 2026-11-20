import asyncio
import html
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
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from aiogram.utils.chat_action import ChatActionSender

from bot import texts
from bot.audio import to_mp3_chunks
from bot.config import settings
from bot.db import REQ_CACHED, REQ_EMPTY, REQ_OK, REQ_TOO_BIG, REQ_TOO_LONG, Database, Request
from bot.fmt import fmt_duration
from bot.openrouter import OpenRouter, Recap

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


def voice_author(message: Message) -> str | None:
    """Name of whoever recorded the voice. The user's own voices are notes to self, so no name."""
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        return None if origin.sender_user.id == message.from_user.id else origin.sender_user.full_name
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name
    if isinstance(origin, MessageOriginChat):
        return origin.author_signature or origin.sender_chat.title
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.title
    return None


def recap_lines(recap: Recap) -> list[tuple[str, bool]]:
    """(text, bold) per line of the summary message."""
    # a lone gist is the whole recap (or a raw fallback text), not a lead to stand out
    lines = [(recap.gist, bool(recap.points or recap.tasks))] if recap.gist else []
    if recap.points:
        lines += [("", False)] + [(f"• {p}", False) for p in recap.points]
    if recap.tasks:
        lines += [("", False), (texts.TASKS_TITLE, True)] + [(f"• {t}", False) for t in recap.tasks]
    return lines


def result_message(duration: int, recap: Recap, transcript_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """HTML. Telegram's length limit counts the text without markup, so only plain lengths are summed."""
    header = f"🎙 {fmt_duration(duration)} · 📝 Кратко"
    out, size = [header, ""], len(header) + 2
    for text, bold in recap_lines(recap):
        room = TG_TEXT_LIMIT - size - 1
        cut = len(text) > room
        if cut:
            text = text[: max(room - 1, 0)] + "…"
        line = html.escape(text)
        out.append(f"<b>{line}</b>" if bold and line else line)
        size += len(text) + 1
        if cut:
            break
    button = InlineKeyboardButton(
        text=texts.FULL_TEXT_BUTTON,
        callback_data=FullTextCb(transcript_id=transcript_id).pack(),
    )
    return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=[[button]])


async def drop_button(query: CallbackQuery) -> None:
    """One-shot buttons: remove the pressed one, keep the rest."""
    markup = query.message.reply_markup
    rows = [
        [b for b in row if b.callback_data != query.data]
        for row in (markup.inline_keyboard if markup else [])
    ]
    rows = [row for row in rows if row]
    try:
        await query.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        )
    except TelegramBadRequest:
        pass


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


def cache_since() -> int:
    """Cache entries created before this are expired (the nightly job deletes them)."""
    return int(time.time()) - settings.recap_cache_ttl_days * 86400


async def cached_result(db: Database, file_unique_id: str, language: str, author: str, user_id: int):
    """(recap, transcript_id, transcript) for this user from the cache, or None. Never raises."""
    try:
        hit = await db.get_cached_recap(file_unique_id, language, author, cache_since())
        if hit is None:
            return None
        transcript_id = hit.transcript_id
        if hit.transcript_user_id != user_id:
            # "full text" only serves the user's own transcripts
            transcript_id = await db.save_transcript(user_id, hit.transcript)
        return hit.recap, transcript_id, hit.transcript
    except Exception:
        log.exception("Recap cache lookup failed for %s", file_unique_id)
        return None


async def store_cache(db: Database, file_unique_id: str, language: str, author: str, transcript_id: int, recap: Recap) -> None:
    try:
        await db.cache_recap(file_unique_id, language, author, transcript_id, recap)
    except Exception:
        log.exception("Failed to cache recap for %s", file_unique_id)


async def transcribe_media(
    message: Message, media, language: str, api: OpenRouter, req: Request, status: Message
) -> str:
    """Download, split with ffmpeg and transcribe; STT usage is added to req."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / ("voice.ogg" if message.voice else "note.mp4")
        await message.bot.download(media.file_id, destination=src)

        await set_status(status, texts.STATUS_TRANSCRIBING)
        chunks = await to_mp3_chunks(src, tmp_dir, settings.chunk_sec)
        parts = []
        for chunk in chunks:
            part = await api.transcribe(chunk, None if language == AUTO else language)
            req.stt_seconds += part.seconds
            req.stt_cost += part.cost
            parts.append(part.text)
    return " ".join(p for p in parts if p).strip()


@router.message(F.voice | F.video_note)
async def on_voice(message: Message, db: Database, api: OpenRouter, jobs: asyncio.Semaphore) -> None:
    media = message.voice or message.video_note
    duration = media.duration or 0
    user_id = message.from_user.id
    req = Request(
        user_id=user_id,
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

    file_unique_id = media.file_unique_id
    language = await user_language(db, user_id)
    author = voice_author(message)
    author_key = author or ""

    hit = await cached_result(db, file_unique_id, language, author_key, user_id)
    if hit:
        recap, transcript_id, transcript = hit
        req.status = REQ_CACHED
        req.stt_model = req.llm_model = None
        req.transcript_id = transcript_id
        req.transcript_chars = len(transcript)
        req.summary_chars = len(recap.text)
        await record_request(db, req)
        text, keyboard = result_message(duration, recap, transcript_id)
        await message.reply(text, reply_markup=keyboard, parse_mode="HTML")
        log.info("Served cached recap of %s to %s", file_unique_id, user_id)
        return

    status = await message.reply(texts.STATUS_DOWNLOADING)
    started = time.monotonic()
    # recorded in `finally`, so money spent before a failure is still counted
    try:
        async with jobs, ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            # same file under another author: only the summary has to be redone
            transcript = await db.get_cached_transcript(file_unique_id, language, cache_since())
            if transcript is None:
                transcript = await transcribe_media(message, media, language, api, req, status)
            else:
                req.stt_model = None
                log.info("Reusing cached transcript of %s for %s", file_unique_id, user_id)
            req.transcript_chars = len(transcript)

            if not transcript:
                req.status = REQ_EMPTY
                await set_status(status, texts.ERROR_EMPTY)
                return

            await set_status(status, texts.STATUS_SUMMARIZING)
            result = await api.summarize(transcript, author)
            recap = result.recap if result.recap.text.strip() else Recap(transcript, [], [])
            req.summary_chars = len(result.recap.text)
            req.llm_prompt_tokens = result.prompt_tokens
            req.llm_completion_tokens = result.completion_tokens
            req.llm_reasoning_tokens = result.reasoning_tokens
            req.llm_cost = result.cost
            transcript_id = await db.save_transcript(user_id, transcript)
            req.transcript_id = transcript_id
            req.status = REQ_OK
            if result.recap.text.strip():
                await store_cache(db, file_unique_id, language, author_key, transcript_id, recap)
    except Exception:
        log.exception("Failed to process message %s from %s", message.message_id, message.from_user.id)
        await set_status(status, texts.ERROR_GENERIC)
        return
    finally:
        req.processing_ms = int((time.monotonic() - started) * 1000)
        await record_request(db, req)

    text, keyboard = result_message(duration, recap, transcript_id)
    await set_status(status, text, reply_markup=keyboard, parse_mode="HTML")
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
    await drop_button(query)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(texts.NOT_VOICE)
