"""Кто допущен к боту и как выдаются приглашения.

Двухуровневый доступ:
  * админы — заданы в конфиге (ADMIN_IDS), строки в БД им не нужны;
  * остальные — попадают в таблицу allowed_users через /allow или через
    погашенный одноразовый инвайт.

Всем прочим бот не отвечает вовсе (см. AccessMiddleware): любая реакция, даже
«вам сюда нельзя», превращает бота в мишень для спама.
"""

import secrets

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .db import READONLY
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


async def is_allowed_readonly(engine: AsyncEngine, admin_ids: frozenset[int], user_id: int) -> bool:
    """То же, но БЕЗ транзакции записи и вне области запроса.

    Для гейта, стоящего ДО общего замка (AccessGateMiddleware): бот находится в
    поиске Telegram по имени, поток чужих апдейтов штатен, и платить за каждый
    спам-месседж блокировкой записи — значит отдавать чужим ту пропускную
    способность, ради которой сеть выносили из транзакции. READONLY-соединение
    открывает DEFERRED и не трогает блокировку записи вовсе (WAL).
    """
    if user_id in admin_ids:
        return True
    async with engine.connect() as conn:
        ro = await conn.execution_options(**{READONLY: True})
        found = await ro.scalar(select(AllowedUser.user_id).where(AllowedUser.user_id == user_id))
    return found is not None


async def allow_user(
    session: AsyncSession,
    user_id: int,
    username: str | None = None,
    invited_by: int | None = None,
) -> AllowedUser:
    """Впустить пользователя. Идемпотентно: повторный вызов лишь освежает username.

    SAVEPOINT вокруг вставки — страховка от гонки с ВНЕШНИМ писателем (ручной
    скрипт над боевой базой, /allow с другого хоста при переезде). Внутри
    процесса гонки больше нет: замок записи один на приложение (L4), два
    апдейта не выполняются одновременно — прежний текст про «два разных
    апдейта одновременно» описывал пер-пользовательский замок, откаченный
    07.08. Проигравший гонку откатывает только вставку, не весь запрос, и
    перечитывает строку победителя.
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
