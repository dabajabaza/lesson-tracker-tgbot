"""Тесты контроля доступа: белый список, админы, одноразовые приглашения.
Фикстура session — в conftest.py: личная файловая копия базы, собранной
настоящими миграциями (см. L8). Не in-memory — файл нужен и для копирования
шаблона, и для проб вторым соединением."""

from lesson_tracker.db.models import AllowedUser, Invite
from lesson_tracker.services import access
from lesson_tracker.timeutils import now_ts

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
#
# Всё, что ниже, идёт через настоящий диспетчер. Раньше здесь звали
# AccessMiddleware напрямую, подсовывая ему голый Message и словарь data,
# собранный руками. Тесты были зелёными, пока в проде не работал единственный
# самостоятельный вход для приглашённого: middleware висит на dp.update и
# получает Update, а разбор кода умел только Message. Проверка в вакууме не
# видит того, что стоит между ней и продом.


async def _reaches_handler(harness, uid: int, text: str = "привет") -> bool:
    """Дошёл ли апдейт до обработчика — по тому, ответил ли бот.

    Молчание и есть отказ: AccessMiddleware не отвечает чужим ничего, чтобы не
    подтверждать, что бот жив (см. L10).
    """
    harness.session.clear()
    await harness.send(text, user_id=uid)
    return bool(harness.session.calls)


async def test_middleware_drops_stranger(harness):
    assert await _reaches_handler(harness, STRANGER) is False


async def test_middleware_lets_admin_through(harness):
    assert await _reaches_handler(harness, 111) is True


async def test_middleware_lets_allowed_user_through(harness, sessionmaker):
    async with sessionmaker() as s:
        await access.allow_user(s, STRANGER, "vasya")
        await s.commit()

    assert await _reaches_handler(harness, STRANGER) is True


async def test_middleware_redeems_valid_deeplink(harness, sessionmaker):
    async with sessionmaker() as s:
        invite = await access.create_invite(s, created_by=111)
        code = invite.code
        await s.commit()

    assert await _reaches_handler(harness, INVITEE, f"/start {code}") is True
    # и доступ остаётся на будущее, уже без ссылки
    assert await _reaches_handler(harness, INVITEE) is True


async def test_middleware_rejects_bad_deeplink(harness, sessionmaker):
    assert await _reaches_handler(harness, STRANGER, "/start bogus") is False
    async with sessionmaker() as s:
        assert await access.is_allowed(s, ADMIN, STRANGER) is False


async def test_middleware_ignores_plain_start_from_stranger(harness):
    """/start без кода — не пропуск: иначе защита не стоила бы ничего."""
    assert await _reaches_handler(harness, STRANGER, "/start") is False


async def test_a_redeemed_invite_does_not_work_a_second_time(harness, sessionmaker):
    """Одноразовость держится атомарным UPDATE, а не проверкой в коде — но
    убедиться в этом стоит на живом пути, а не только на сервисе."""
    async with sessionmaker() as s:
        invite = await access.create_invite(s, created_by=111)
        code = invite.code
        await s.commit()

    assert await _reaches_handler(harness, INVITEE, f"/start {code}") is True
    assert await _reaches_handler(harness, 777, f"/start {code}") is False
