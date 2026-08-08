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
import sqlite3

from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db import READONLY
from bot.models import FsmRecord
from bot.services import StudentService
from tests.bot_harness import make_update_callback
from tests.reading import balances_by_name

A, B = 111, 222  # два преподавателя; оба в TEST_ADMIN_IDS


def _write_lock_free(db_path) -> bool:
    """Свободна ли база для записи прямо сейчас.

    Голый sqlite3 мимо SQLAlchemy — отдельным соединением, с нулевым ожиданием
    и в обход пула, чтобы ответ был про состояние файла, а не про то, что успел
    закэшировать движок. В режиме WAL BEGIN IMMEDIATE не мешает читателям и
    упирается ровно в чужую блокировку записи — то, что и требуется измерить.
    """
    con = sqlite3.connect(db_path, timeout=0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


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

    assert await balances_by_name(sessionmaker) == {"Аня": -1, "Боря": -1}


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

    assert await balances_by_name(sessionmaker) == {"Аня": -5}


async def test_во_время_сети_база_свободна_для_записи(harness, session, sessionmaker, db_path):
    """Главное свойство, ради которого разворачивался порядок.

    Пока отправка стояла внутри транзакции, блокировка записи держалась весь
    круг до Telegram и обратно — сотни миллисекунд на апдейт, и потолок в
    единицы апдейтов в секунду независимо от нагрузки. Теперь обработчик только
    складывает намерения (см. ui.Responder), а сеть случается после коммита.

    Проверяется буквально: на каждом вызове Telegram спрашиваем у самой базы,
    свободна ли она для записи. Хотя бы один занятый ответ означает, что сеть
    снова уехала внутрь транзакции.
    """
    students = StudentService(session)
    a = await students.create(A, "Аня", 160000)
    await session.commit()

    free: list[bool] = []
    original = harness.session.make_request

    async def probing(*args, **kwargs):
        free.append(_write_lock_free(db_path))
        return await original(*args, **kwargs)

    harness.session.make_request = probing

    await harness.dp.feed_update(
        harness.bot, make_update_callback(f"charge:{a.id}", user_id=A, update_id=9200)
    )

    assert free, "апдейт обязан был сходить в Telegram"
    assert all(free), "во время вызова Telegram транзакция апдейта должна быть уже закрыта"


async def test_readonly_соединение_не_берёт_блокировку_записи(engine, db_path):
    """Пометка READONLY (db.py) открывает транзакцию как DEFERRED.

    С контрольным случаем рядом: без пометки то же самое чтение блокировку
    берёт. Пара нужна целиком — сама по себе первая половина прошла бы и на
    движке, который вообще не открывает транзакций.
    """
    stmt = select(FsmRecord.state).where(FsmRecord.key == "нет-такого")

    async with engine.connect() as conn:
        ro = await conn.execution_options(**{READONLY: True})
        await ro.execute(stmt)
        assert _write_lock_free(db_path), "читающее соединение не должно держать запись"

    async with engine.connect() as conn:
        await conn.execute(stmt)
        assert not _write_lock_free(db_path), (
            "без пометки транзакция обязана оставаться IMMEDIATE — иначе пометка ничего не значит"
        )


async def test_хранилище_диспетчера_читает_помеченным_соединением(harness):
    """Хранилище диспетчера читает raw_state ДО общего замка (см. storage.py).

    Пока его транзакция открывалась как IMMEDIATE, каждый апдейт забирал
    блокировку записи снаружи всякой сериализации: замок переставал что-либо
    гарантировать, а под нагрузкой апдейт умирал с «database is locked» там,
    где нет ни отката, ни отметки идемпотентности.

    Проверяется по фактически отправленному SQL, а не по флагу в объекте:
    пометку легко потерять по дороге, и заметить это должен тест, а не прод.
    """
    engine = await harness.dp.storage._container.get(AsyncEngine)  # noqa: SLF001
    seen: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, *_args):  # noqa: ANN001, ANN202
        if statement.startswith("BEGIN"):
            seen.append(statement)

    try:
        await harness.dp.storage.get_state(StorageKey(bot_id=1, chat_id=A, user_id=A))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert seen == ["BEGIN"], f"ожидался DEFERRED, а ушло: {seen}"
