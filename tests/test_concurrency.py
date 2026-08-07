"""Одновременные апдейты не должны терять операции.

SQLite допускает одного писателя. aiogram обрабатывает апдейты конкурентно даже
в одном процессе, поэтому два человека, нажавшие кнопку в одну и ту же
миллисекунду, — это два конкурирующих писателя в один файл.

До введения глобального замока пер-пользовательский замок сериализовал только
апдейты ОДНОГО владельца. Два разных владельца проходили мимо него, обе
транзакции открывались как DEFERRED, читали общий снимок, и вторая получала
мгновенный «database is locked» на попытке записи — не по таймауту, а потому
что снимок уже испорчен первой. Операция терялась, пользователь видел
«Не получилось выполнить действие».
"""

import asyncio

from sqlalchemy import select

from bot.models import Student
from bot.services import StudentService
from tests.bot_harness import make_update_callback

A, B = 111, 222  # два преподавателя; оба в TEST_ADMIN_IDS


async def _balances(sessionmaker) -> dict[str, int]:
    async with sessionmaker() as s:
        return {st.name: st.balance for st in await s.scalars(select(Student))}


async def test_одновременные_апдейты_разных_владельцев_не_теряются(harness, session, sessionmaker):
    students = StudentService(session)
    a = await students.create(A, "Аня", 160000)
    b = await students.create(B, "Боря", 160000)
    await session.commit()

    async def charge(uid: int, sid: int, update_id: int) -> None:
        await harness.dp.feed_update(
            harness.bot,
            make_update_callback(f"charge:{sid}", user_id=uid, update_id=update_id),
        )

    # Оба апдейта в одном цикле событий, одновременно. Пер-пользовательский
    # замок их не сериализует: владельцы разные.
    await asyncio.gather(charge(A, a.id, 9001), charge(B, b.id, 9002))

    assert await _balances(sessionmaker) == {"Аня": -1, "Боря": -1}


async def test_пачка_одновременных_апдейтов_одного_владельца(harness, session, sessionmaker):
    """Двойной тап по одной кнопке: обе операции обязаны примениться и
    сложиться, а не потеряться в гонке read-modify-write."""
    students = StudentService(session)
    a = await students.create(A, "Аня", 160000)
    await session.commit()

    await asyncio.gather(
        *(
            harness.dp.feed_update(
                harness.bot,
                make_update_callback(f"charge:{a.id}", user_id=A, update_id=9100 + i),
            )
            for i in range(5)
        )
    )

    assert await _balances(sessionmaker) == {"Аня": -5}
