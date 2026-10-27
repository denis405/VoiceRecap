import csv
import html
import io
import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import texts
from bot.config import settings
from bot.db import APPROVED, PENDING, REJECTED, Database, Usage, User
from bot.fmt import fmt_duration, fmt_int, fmt_money, fmt_time

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.from_user.id == settings.admin_id)
router.callback_query.filter(F.from_user.id == settings.admin_id)

STATUS_ICONS = {APPROVED: "✅", PENDING: "⏳", REJECTED: "🚫"}
DAY = 86400
TOP_USERS = 15


class AccessCb(CallbackData, prefix="acc"):
    action: str  # "approve" | "reject"
    user_id: int


def describe(user: User) -> str:
    name = html.escape(user.full_name or "—")
    username = f" @{html.escape(user.username)}" if user.username else ""
    return f'<a href="tg://user?id={user.id}">{name}</a>{username} (<code>{user.id}</code>)'


def describe_id(user_id: int, users: dict[int, User]) -> str:
    """Like describe(), but also works for ids missing from the users table (the admin)."""
    if user_id in users:
        return describe(users[user_id])
    name = "Администратор" if user_id == settings.admin_id else "—"
    return f'<a href="tg://user?id={user_id}">{name}</a> (<code>{user_id}</code>)'


def usage_line(u: Usage) -> str:
    return f"{u.ok} сообщ. · 🎙 {fmt_duration(u.audio_sec)} · {fmt_money(u.cost)}"


def decision_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=AccessCb(action="approve", user_id=user_id).pack()),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=AccessCb(action="reject", user_id=user_id).pack()),
            ]
        ]
    )


async def notify_admin_new_user(bot: Bot, admin_id: int, user: User) -> None:
    try:
        await bot.send_message(
            admin_id,
            f"🆕 Запрос на доступ\n{describe(user)}",
            parse_mode=ParseMode.HTML,
            reply_markup=decision_keyboard(user.id),
        )
    except TelegramAPIError:
        log.exception("Failed to notify admin about user %s", user.id)


async def notify_user(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        log.warning("Failed to notify user %s", user_id)


async def apply_decision(bot: Bot, db: Database, user_id: int, status: str) -> User | None:
    if not await db.set_status(user_id, status):
        return None
    await notify_user(bot, user_id, texts.ACCESS_GRANTED if status == APPROVED else texts.ACCESS_REVOKED)
    log.info("User %s -> %s", user_id, status)
    return await db.get_user(user_id)


@router.callback_query(AccessCb.filter())
async def on_decision(query: CallbackQuery, callback_data: AccessCb, db: Database) -> None:
    status = APPROVED if callback_data.action == "approve" else REJECTED
    current = await db.get_user(callback_data.user_id)
    if current is None:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    user = current if current.status == status else await apply_decision(query.bot, db, current.id, status)
    verdict = "✅ Одобрен" if status == APPROVED else "❌ Отклонён"
    await query.message.edit_text(f"{verdict}\n{describe(user)}", parse_mode=ParseMode.HTML)
    await query.answer()


@router.message(Command("users"))
async def cmd_users(message: Message, db: Database) -> None:
    users = await db.list_users()
    if not users:
        await message.answer("Пока никто не запрашивал доступ.")
        return
    usage = dict(await db.usage_by_user())
    lines = []
    for u in users:
        line = f"{STATUS_ICONS.get(u.status, '?')} {describe(u)}"
        if u.id in usage:
            line += f" · {usage[u.id].ok} сообщ. · {fmt_money(usage[u.id].cost)}"
        lines.append(line)
    lines.append("\n/approve &lt;id&gt; — одобрить, /revoke &lt;id&gt; — отключить, /stats &lt;id&gt; — статистика")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


async def _change_by_command(message: Message, command: CommandObject, db: Database, status: str) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(f"Использование: /{command.command} <id>")
        return
    user = await apply_decision(message.bot, db, int(arg), status)
    if user is None:
        await message.answer("Пользователь не найден. Он должен сначала написать боту.")
        return
    await message.answer(f"{STATUS_ICONS[status]} {describe(user)}", parse_mode=ParseMode.HTML)


@router.message(Command("approve"))
async def cmd_approve(message: Message, command: CommandObject, db: Database) -> None:
    await _change_by_command(message, command, db, APPROVED)


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, db: Database) -> None:
    await _change_by_command(message, command, db, REJECTED)


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, db: Database) -> None:
    arg = (command.args or "").strip()
    if arg:
        if not arg.isdigit():
            await message.answer("Использование: /stats или /stats <id>")
            return
        await _user_stats(message, db, int(arg))
        return

    now = int(time.time())
    periods = [
        ("Сегодня", now - now % DAY),
        ("7 дней", now - 7 * DAY),
        ("30 дней", now - 30 * DAY),
        ("Всё время", None),
    ]
    lines = ["📊 <b>Статистика</b> (время UTC)", ""]
    for title, since in periods:
        u = await db.usage(since)
        lines.append(f"<b>{title}:</b> {usage_line(u)} · 👥 {u.users}")

    top = await db.usage_by_user(now - 30 * DAY)
    if top:
        users = {u.id: u for u in await db.list_users()}
        lines += ["", "<b>Топ за 30 дней:</b>"]
        for i, (user_id, u) in enumerate(top[:TOP_USERS], 1):
            lines.append(f"{i}. {describe_id(user_id, users)} — {usage_line(u)}")

    lines.append("\n/stats &lt;id&gt; — по пользователю, /stats_csv — выгрузка всех запросов")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


async def _user_stats(message: Message, db: Database, user_id: int) -> None:
    user = await db.get_user(user_id)
    total = await db.usage(user_id=user_id)
    if user is None and not total.requests:
        await message.answer("Пользователь не найден.")
        return
    month = await db.usage(int(time.time()) - 30 * DAY, user_id)
    avg = total.audio_sec // total.ok if total.ok else 0

    lines = [
        f"📊 {describe_id(user_id, {user.id: user} if user else {})}",
        "",
        f"<b>Всего:</b> {usage_line(total)}",
        f"<b>30 дней:</b> {usage_line(month)}",
        "",
        f"Длина: в среднем {fmt_duration(avg)}, максимум {fmt_duration(total.max_duration_sec)}",
        f"Расшифровано: {fmt_int(total.transcript_chars)} символов",
        f"Токены LLM: {fmt_int(total.prompt_tokens)} вход / {fmt_int(total.completion_tokens)} выход"
        f" (reasoning {fmt_int(total.reasoning_tokens)})",
        f"Расходы: STT {fmt_money(total.stt_cost)} + LLM {fmt_money(total.llm_cost)}"
        f" = <b>{fmt_money(total.cost)}</b>",
        f"Не обработано: {total.requests - total.ok} из {total.requests}",
        f"Последний раз: {fmt_time(total.last_at)}",
    ]
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("stats_csv"))
async def cmd_stats_csv(message: Message, db: Database) -> None:
    headers, rows = await db.export_requests()
    if not rows:
        await message.answer("Пока нет данных.")
        return
    buf = io.StringIO()
    # ";" and decimal commas: what Excel with a Russian locale expects
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([f"{v:.6f}".replace(".", ",") if isinstance(v, float) else v for v in row])
    filename = f"voicerecap_stats_{time.strftime('%Y-%m-%d', time.gmtime())}.csv"
    await message.answer_document(
        BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=filename),
        caption=f"📊 {len(rows)} запросов",
    )
