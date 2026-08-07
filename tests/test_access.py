"""Тесты контроля доступа: белый список, админы, одноразовые приглашения.
Фикстура session — в conftest.py (настоящий SQLite in-memory)."""

from datetime import datetime

from aiogram.types import Chat, Message
from aiogram.types import User as TgUser

from bot import access
from bot.middlewares import AccessMiddleware
from bot.models import AllowedUser, Invite, now_ts

ADMIN = frozenset({111})
STRANGER = 999
INVITEE = 555


# ---------- допуск ----------


async def test_stranger_is_not_allowed(session):
    assert await access.is_allowed(session, ADMIN, STRANGER) is False


async def test_admin_allowed_without_db_row(session):
    """Админа пускает конфиг — строка в allowed_users ему не нужна."""
    assert await access.is_allowed(session, ADMIN, 111) is True
    assert await session.get(AllowedUser, 111) is None


async def test_allow_user_grants_access(session):
    await access.allow_user(session, STRANGER, "vasya")
    assert await access.is_allowed(session, ADMIN, STRANGER) is True


async def test_allow_user_is_idempotent_and_refreshes_username(session):
    await access.allow_user(session, STRANGER, "old")
    row = await access.allow_user(session, STRANGER, "new")
    assert row.username == "new"

    from sqlalchemy import func, select

    count = await session.scalar(select(func.count()).select_from(AllowedUser))
    assert count == 1


async def test_empty_admin_ids_blocks_everyone(session):
    """Пустой ADMIN_IDS не должен случайно открывать доступ всем."""
    assert await access.is_allowed(session, frozenset(), 111) is False


# ---------- приглашения ----------


async def test_invite_grants_access(session):
    invite = await access.create_invite(session, created_by=111)
    assert await access.redeem_invite(session, invite.code, INVITEE, "petya") is True
    assert await access.is_allowed(session, ADMIN, INVITEE) is True


async def test_invite_records_who_invited(session):
    invite = await access.create_invite(session, created_by=111)
    await access.redeem_invite(session, invite.code, INVITEE, None)

    row = await session.get(AllowedUser, INVITEE)
    assert row is not None and row.invited_by == 111


async def test_invite_is_one_time(session):
    invite = await access.create_invite(session, created_by=111)
    assert await access.redeem_invite(session, invite.code, INVITEE, None) is True
    # Второй пользователь по тому же коду пройти не должен.
    assert await access.redeem_invite(session, invite.code, 777, None) is False
    assert await access.is_allowed(session, ADMIN, 777) is False


async def test_unknown_code_rejected(session):
    assert await access.redeem_invite(session, "no-such-code", INVITEE, None) is False
    assert await access.is_allowed(session, ADMIN, INVITEE) is False


async def test_expired_invite_rejected(session):
    invite = Invite(code="expired", created_by=111, expires_at=now_ts() - 1)
    session.add(invite)
    await session.flush()

    assert await access.redeem_invite(session, "expired", INVITEE, None) is False
    assert await access.is_allowed(session, ADMIN, INVITEE) is False


async def test_invite_codes_are_unique(session):
    codes = {(await access.create_invite(session, created_by=111)).code for _ in range(20)}
    assert len(codes) == 20


# ---------- middleware: собственно отсечение спама ----------


def _message(uid: int, text: str) -> Message:
    """Настоящий aiogram-Message: middleware проверяет тип через isinstance,
    поэтому SimpleNamespace тут не подошёл бы."""
    return Message(
        message_id=1,
        date=datetime(2026, 1, 1),
        chat=Chat(id=uid, type="private"),
        from_user=TgUser(id=uid, is_bot=False, first_name="T"),
        text=text,
    )


async def _pass_through(session, uid: int, text: str = "привет"):
    """Прогоняет апдейт через AccessMiddleware. True — обработчик был вызван."""
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    event = _message(uid, text)
    mw = AccessMiddleware(ADMIN)
    await mw(handler, event, {"event_from_user": event.from_user, "session": session})
    return called


async def test_middleware_drops_stranger(session):
    assert await _pass_through(session, STRANGER) is False


async def test_middleware_lets_admin_through(session):
    assert await _pass_through(session, 111) is True


async def test_middleware_lets_allowed_user_through(session):
    await access.allow_user(session, STRANGER, "vasya")
    assert await _pass_through(session, STRANGER) is True


async def test_middleware_redeems_valid_deeplink(session):
    invite = await access.create_invite(session, created_by=111)
    await session.commit()

    assert await _pass_through(session, INVITEE, f"/start {invite.code}") is True
    # и доступ остаётся на будущее, уже без ссылки
    assert await _pass_through(session, INVITEE) is True


async def test_middleware_rejects_bad_deeplink(session):
    assert await _pass_through(session, STRANGER, "/start bogus") is False
    assert await access.is_allowed(session, ADMIN, STRANGER) is False


async def test_middleware_ignores_plain_start_from_stranger(session):
    """/start без кода — не пропуск: иначе защита не стоила бы ничего."""
    assert await _pass_through(session, STRANGER, "/start") is False
