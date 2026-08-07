"""Кто допущен к боту и как выдаются приглашения.

Двухуровневый доступ:
  * админы — заданы в конфиге (ADMIN_IDS), строки в БД им не нужны;
  * остальные — попадают в таблицу allowed_users через /allow или через
    погашенный одноразовый инвайт.

Всем прочим бот не отвечает вовсе (см. AccessMiddleware): любая реакция, даже
«вам сюда нельзя», превращает бота в мишень для спама.
"""

import secrets

from sqlalchemy import CursorResult, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AllowedUser, Invite, now_ts

# Длина кода в байтах до base64url: 16 байт ≈ 22 символа — не перебирается и
# при этом ссылка остаётся читаемой.
INVITE_CODE_BYTES = 16
INVITE_TTL_SECONDS = 48 * 3600


async def is_allowed(session: AsyncSession, admin_ids: frozenset[int], user_id: int) -> bool:
    """Разрешено ли пользователю работать с ботом."""
    if user_id in admin_ids:
        return True
    return await session.get(AllowedUser, user_id) is not None


async def allow_user(
    session: AsyncSession,
    user_id: int,
    username: str | None = None,
    invited_by: int | None = None,
) -> AllowedUser:
    """Впустить пользователя. Идемпотентно: повторный вызов лишь освежает username.

    Вставка защищена вложенной транзакцией (SAVEPOINT): апдейты одного и того же
    пользователя сериализует Lock в DbSessionMiddleware, но два РАЗНЫХ апдейта
    (например, два перехода по разным инвайтам) могут дойти сюда одновременно.
    Проигравший гонку откатит только вставку, а не весь запрос, и перечитает
    строку победителя.
    """
    row = await session.get(AllowedUser, user_id)
    if row is None:
        try:
            async with session.begin_nested():
                row = AllowedUser(user_id=user_id, username=username, invited_by=invited_by)
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = await session.get(AllowedUser, user_id)
            assert row is not None
    elif username is not None and row.username != username:
        row.username = username
    return row


async def create_invite(session: AsyncSession, created_by: int) -> Invite:
    """Выпустить одноразовый код со сроком жизни INVITE_TTL_SECONDS."""
    invite = Invite(
        code=secrets.token_urlsafe(INVITE_CODE_BYTES),
        created_by=created_by,
        expires_at=now_ts() + INVITE_TTL_SECONDS,
    )
    session.add(invite)
    await session.flush()
    return invite


async def redeem_invite(
    session: AsyncSession, code: str, user_id: int, username: str | None
) -> bool:
    """Погасить код за этого пользователя. False — код неизвестен, просрочен
    или уже использован.

    Захват атомарный: проверка «свободен ли код» и его пометка — один UPDATE,
    условие перепроверяется движком в момент записи. Чтение с последующей
    записью позволило бы двум одновременным переходам увидеть used_by IS NULL
    и погасить один код дважды; здесь rowcount == 1 получит только первый.
    """
    now = now_ts()
    stmt = (
        update(Invite)
        .where(Invite.code == code, Invite.used_by.is_(None), Invite.expires_at >= now)
        .values(used_by=user_id, used_at=now)
    )
    result = await session.execute(stmt)
    assert isinstance(result, CursorResult)
    if result.rowcount != 1:
        return False

    invite = await session.get(Invite, code)
    await allow_user(session, user_id, username, invited_by=invite.created_by if invite else None)
    return True
