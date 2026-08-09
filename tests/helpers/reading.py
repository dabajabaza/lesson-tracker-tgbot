"""Чтение состояния из БД и из записанных вызовов — для утверждений в тестах.

Сведено сюда не ради экономии строк. `_students` жил в четырёх файлах и в трёх
из них возвращал РАЗНОЕ: где-то пары «имя — цена», где-то «имя — баланс», где-то
просто имена. Читая утверждение `assert await _students(sm) == [("Лера", 0)]`,
понять, ноль это баланс или цена, можно было только сходив в начало файла.

Поэтому имена здесь говорят, что именно вернётся, а не «ученики».
"""

from sqlalchemy import select

from lesson_tracker.db.models import FsmRecord, OutboxMessage, ProcessedUpdate, Student


async def student_prices(sessionmaker) -> list[tuple[str, int]]:
    """Пары «имя — стоимость занятия», в порядке создания."""
    async with sessionmaker() as s:
        return [(st.name, st.price) for st in await s.scalars(select(Student))]


async def student_balances(sessionmaker) -> list[tuple[str, int]]:
    """Пары «имя — баланс занятий»."""
    async with sessionmaker() as s:
        return [(st.name, st.balance) for st in await s.scalars(select(Student))]


async def balances_by_name(sessionmaker) -> dict[str, int]:
    """Балансы словарём — когда порядок не важен, а имена нужны."""
    async with sessionmaker() as s:
        return {st.name: st.balance for st in await s.scalars(select(Student))}


async def student_names(sessionmaker) -> list[str]:
    async with sessionmaker() as s:
        return list(await s.scalars(select(Student.name)))


async def fsm_state(sessionmaker) -> str | None:
    """Состояние единственного диалога, если он есть."""
    async with sessionmaker() as s:
        records = list(await s.scalars(select(FsmRecord)))
        return records[0].state if records else None


async def processed_update_ids(sessionmaker) -> list[int]:
    async with sessionmaker() as s:
        return sorted(await s.scalars(select(ProcessedUpdate.update_id)))


async def queued_messages(sessionmaker) -> list[OutboxMessage]:
    """Необслуженные обещания доставки, в порядке записи."""
    async with sessionmaker() as s:
        return list(await s.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))


def last_callback_answer(harness) -> str:
    """Текст последнего ответа на нажатие кнопки.

    Утверждает и сам факт ответа: без него у пользователя вечно «крутится»
    индикатор, и это отдельная ошибка, которую легко проглядеть за проверкой
    текста.
    """
    answers = harness.session.calls_of("AnswerCallbackQuery")
    assert answers, "на колбэк обязан быть ответ, иначе кнопка «крутится»"
    return answers[-1].text or ""


def last_edit(harness) -> str:
    edits = harness.session.calls_of("EditMessageText")
    assert edits, "обработчик должен был отредактировать сообщение"
    return edits[-1].text
