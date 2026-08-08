"""Границы, за которыми внешние данные ломали обработчик.

Всё здесь — про значения, приходящие снаружи: callback_data подделывается
клиентом, аргумент /allow набирается руками, имя ученика придумывает человек.
Общий признак дефектов был один: проверялась форма, но не величина и не
содержимое, — и падение случалось уже в SQLite или в Excel у получателя.
"""

import io

import openpyxl
from sqlalchemy import select

from bot.export_data import rows_to_xlsx
from bot.handlers._common import as_int
from bot.models import AllowedUser, Student
from bot.services import PaymentService, StudentService
from tests.bot_harness import make_update_callback

ADMIN = 1
HUGE = "9" * 23  # влезает в лимит callback_data, но не в 64-битный INTEGER


def test_as_int_отвергает_число_шире_базы():
    assert as_int("5") == 5
    assert as_int("не число") is None
    assert as_int(HUGE) is None, "SQLite такое не примет — отвергать надо до него"


async def test_подделанный_огромный_id_не_роняет_обработчик(harness, session, sessionmaker):
    """Раньше 23-значный id проходил разбор и падал на привязке параметра:
    пользователь видел «Не получилось выполнить действие» и трейсбек в
    журнале вместо «Кнопка устарела»."""
    await StudentService(session).create(ADMIN, "Аня", 160000)
    await session.commit()

    await harness.dp.feed_update(
        harness.bot, make_update_callback(f"card:{HUGE}", user_id=ADMIN, update_id=9500)
    )

    answers = [m.text or "" for m in harness.session.calls_of("AnswerCallbackQuery")]
    assert any("устарела" in t for t in answers), f"ожидался внятный отказ, а было: {answers}"


async def test_огромный_id_в_allow_даёт_подсказку(harness, sessionmaker):
    await harness.send(f"/allow {HUGE}", user_id=ADMIN)

    sent = [m.text or "" for m in harness.session.calls_of("SendMessage")]
    assert any("Использование" in t for t in sent), f"ожидалась подсказка, а было: {sent}"
    async with sessionmaker() as s:
        assert list(await s.scalars(select(AllowedUser))) == []


def test_выгрузка_в_excel_не_исполняет_формулы():
    """Имя, начинающееся с «=», openpyxl записывает настоящей формулой:
    выгрузка превращалась в исполняемый документ, а кривое выражение — в
    предложение Excel «восстановить файл». Экран стоял только на пути CSV."""
    data = rows_to_xlsx([['=HYPERLINK("http://evil","клик")', "ок"]], "Лист")
    cell = openpyxl.load_workbook(io.BytesIO(data)).active.cell(row=1, column=1)

    assert cell.data_type == "s", "ячейка не должна быть формулой"


async def test_нулевая_стоимость_не_роняет_оплату(session):
    """Инвариант «стоимость больше нуля» держался только в обработчиках, а
    делит на неё сервис. Боевая база приехала из serverless-версии, так что
    унаследованная строка с нулём — не выдумка."""
    students = StudentService(session)
    assert await students.create(ADMIN, "Аня", 0) is None, "ноль нельзя принимать"

    session.add(Student(owner_id=ADMIN, name="Боря", name_lower="боря", price=0))
    await session.flush()
    broken = (await session.scalars(select(Student).where(Student.name == "Боря"))).one()

    assert await PaymentService(session, students).apply(ADMIN, broken.id, 1000) is None
