"""Идемпотентность обработки апдейтов.

Telegram подтверждает доставку только следующим вызовом getUpdates, поэтому
процесс, умерший сразу после коммита, получает тот же update_id заново. Для
учёта денег повторное применение хуже потери, и эти тесты фиксируют границу:
отметка об апдейте живёт в одной транзакции с изменением, не раньше и не позже.
"""

import pytest
from sqlalchemy import select

from bot.middlewares import DbSessionMiddleware
from bot.models import ProcessedUpdate, Student


class FakeUpdate:
    """Минимальный двойник aiogram-Update: middleware нужен только update_id."""

    def __init__(self, update_id: int) -> None:
        self.update_id = update_id


async def _marks(sessionmaker) -> list[int]:
    async with sessionmaker() as s:
        return list(await s.scalars(select(ProcessedUpdate.update_id)))


async def _students(sessionmaker) -> list[str]:
    async with sessionmaker() as s:
        return list(await s.scalars(select(Student.name)))


def _add_student(name: str):
    """Обработчик, имитирующий мутацию: пишет и коммитит, как это делает repo."""

    async def handler(event, data):
        session = data["session"]
        session.add(
            Student(owner_id=1, name=name, name_lower=name.lower(), price=100)
        )
        await session.commit()
        return "готово"

    return handler


@pytest.mark.asyncio
async def test_первый_апдейт_применяется_и_отмечается(sessionmaker):
    mw = DbSessionMiddleware(sessionmaker)

    result = await mw._run(_add_student("Аня"), FakeUpdate(1001), {})

    assert result == "готово"
    assert await _students(sessionmaker) == ["Аня"]
    assert await _marks(sessionmaker) == [1001]


@pytest.mark.asyncio
async def test_повтор_того_же_апдейта_отбрасывается(sessionmaker):
    mw = DbSessionMiddleware(sessionmaker)
    await mw._run(_add_student("Аня"), FakeUpdate(1001), {})

    # Тот же update_id — именно это присылает Telegram после падения процесса.
    called = False

    async def handler(event, data):
        nonlocal called
        called = True
        return "не должно случиться"

    result = await mw._run(handler, FakeUpdate(1001), {})

    assert result is None, "повтор обязан быть отброшен до обработчика"
    assert called is False
    assert await _students(sessionmaker) == ["Аня"], "ученик не должен задвоиться"


@pytest.mark.asyncio
async def test_разные_апдейты_обрабатываются_оба(sessionmaker):
    mw = DbSessionMiddleware(sessionmaker)

    await mw._run(_add_student("Аня"), FakeUpdate(1), {})
    await mw._run(_add_student("Боря"), FakeUpdate(2), {})

    assert await _students(sessionmaker) == ["Аня", "Боря"]
    assert sorted(await _marks(sessionmaker)) == [1, 2]


@pytest.mark.asyncio
async def test_падение_обработчика_не_оставляет_отметку(sessionmaker):
    """Иначе потерянный апдейт уже нельзя было бы повторить: он числился бы
    применённым, хотя изменение откатилось."""
    mw = DbSessionMiddleware(sessionmaker)

    async def broken(event, data):
        session = data["session"]
        session.add(
            Student(owner_id=1, name="Вера", name_lower="вера", price=100)
        )
        raise RuntimeError("обработчик упал до коммита")

    with pytest.raises(RuntimeError):
        await mw._run(broken, FakeUpdate(2002), {})

    assert await _students(sessionmaker) == []
    assert await _marks(sessionmaker) == [], "апдейт не применён — отметки быть не должно"

    # И повтор того же апдейта проходит нормально: он не считается обработанным.
    result = await mw._run(_add_student("Вера"), FakeUpdate(2002), {})
    assert result == "готово"
    assert await _students(sessionmaker) == ["Вера"]


@pytest.mark.asyncio
async def test_читающий_апдейт_не_отмечается(sessionmaker):
    """Обработчик без коммита ничего не менял, поэтому его повтор безвреден.
    Отмечать такие апдейты незачем — таблица росла бы на ровном месте."""
    mw = DbSessionMiddleware(sessionmaker)

    async def readonly(event, data):
        return "экран показан"

    assert await mw._run(readonly, FakeUpdate(3003), {}) == "экран показан"
    assert await _marks(sessionmaker) == []


@pytest.mark.asyncio
async def test_событие_без_update_id_не_ломает_обработку(sessionmaker):
    """В aiogram middleware может получить и не-Update; отсутствие update_id
    не должно ронять цепочку."""
    mw = DbSessionMiddleware(sessionmaker)

    class Bare:
        pass

    assert await mw._run(_add_student("Гена"), Bare(), {}) == "готово"
    assert await _students(sessionmaker) == ["Гена"]
    assert await _marks(sessionmaker) == []
