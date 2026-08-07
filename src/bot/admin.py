"""Админские команды: выдача приглашений и ручной допуск.

Отвечаем только тем, кто в ADMIN_IDS. Допущенный, но не админ, получит на эти
команды молчание, а не «недостаточно прав»: existence админки незачем светить.

Роутер подключать ДО основного — там есть catch-all обработчики, которые иначе
перехватят команду.
"""

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import access
from .models import AllowedUser, Invite, now_ts

log = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)

_ALLOW_USAGE = (
    "Использование: <code>/allow &lt;telegram_id&gt;</code>\n\n"
    "Узнать id можно у @userinfobot. Для одноразовой ссылки — /invite."
)


def _is_admin(message: Message, admin_ids: frozenset[int]) -> bool:
    return message.from_user is not None and message.from_user.id in admin_ids


@router.message(Command("invite"))
async def cmd_invite(message: Message, session: AsyncSession, admin_ids: frozenset[int]) -> None:
    if not _is_admin(message, admin_ids):
        return
    assert message.from_user is not None and message.bot is not None

    invite = await access.create_invite(session, message.from_user.id)
    await session.commit()

    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start={invite.code}"
    hours = access.INVITE_TTL_SECONDS // 3600
    await message.answer(
        f"Одноразовая ссылка (действует {hours} ч):\n\n"
        f"<code>{link}</code>\n\n"
        "Перешедший по ней получит доступ к боту со своими, отдельными данными.",
        parse_mode="HTML",
    )
    log.info("Инвайт выпущен: admin=%s code=%s", message.from_user.id, invite.code)


@router.message(Command("allow"))
async def cmd_allow(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    admin_ids: frozenset[int],
) -> None:
    if not _is_admin(message, admin_ids):
        return
    assert message.from_user is not None

    args = (command.args or "").strip()
    # isdecimal, а не isdigit: последний истинен для символов вроде "³",
    # на которых int() потом падает.
    if not args.isdecimal():
        await message.answer(_ALLOW_USAGE, parse_mode="HTML")
        return

    user_id = int(args)
    await access.allow_user(session, user_id, invited_by=message.from_user.id)
    await session.commit()
    await message.answer(f"Пользователь <code>{user_id}</code> допущен.", parse_mode="HTML")
    log.info("Доступ выдан вручную: admin=%s user_id=%s", message.from_user.id, user_id)


@router.message(Command("access"))
async def cmd_access(message: Message, session: AsyncSession, admin_ids: frozenset[int]) -> None:
    """Показать, кто допущен и какие приглашения ещё не погашены."""
    if not _is_admin(message, admin_ids):
        return

    rows = list(await session.scalars(select(AllowedUser)))
    live = list(
        await session.scalars(
            select(Invite).where(Invite.used_by.is_(None), Invite.expires_at >= now_ts())
        )
    )

    lines = [f"<b>Админы</b> ({len(admin_ids)}): " + ", ".join(str(i) for i in sorted(admin_ids))]
    if rows:
        lines.append(f"\n<b>Допущены</b> ({len(rows)}):")
        lines += [
            f"  <code>{r.user_id}</code>" + (f" @{r.username}" if r.username else "") for r in rows
        ]
    else:
        lines.append("\n<b>Допущены</b>: никого (кроме админов)")
    lines.append(f"\n<b>Непогашенных приглашений</b>: {len(live)}")

    await message.answer("\n".join(lines), parse_mode="HTML")
