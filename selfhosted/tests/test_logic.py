"""Тесты бизнес-логики и чистых функций на настоящем SQLite (in-memory)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot import repo
from bot.csv_export import to_csv
from bot.export_data import history_rows, students_rows
from bot.models import Base
from bot.money import MAX_MONEY, format_money, parse_money, parse_money_strict, to_rubles
from bot.render import lessons_word, render_operation, status_emoji

A, B = 111, 222  # owner id двух разных пользователей


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# ---------- чистые функции ----------

def test_money_format_and_parse():
    assert format_money(160000) == "1 600 ₽"
    assert format_money(160050) == "1 600,50 ₽"
    assert format_money(-160000) == "-1 600 ₽"
    assert to_rubles(160050) == "1600,50"
    assert parse_money("1 600,50") == 160050
    assert parse_money("1600.5") == 160050
    assert parse_money("abc") is None
    assert parse_money("12,345") is None


def test_money_limits():
    assert parse_money_strict("0").error == "zero"
    assert parse_money_strict("1" * 30).error == "range"
    assert parse_money("10000001") is None            # > 10 млн ₽
    assert parse_money("10000000") == MAX_MONEY        # ровно лимит проходит


def test_status_and_plural():
    assert status_emoji(3) == "🟢" and status_emoji(2) == "🟡"
    assert status_emoji(1) == "🟠" and status_emoji(0) == "🔴" and status_emoji(-1) == "🔴"
    assert lessons_word(1) == "занятие"
    assert lessons_word(2) == "занятия"
    assert lessons_word(5) == "занятий"
    assert lessons_word(11) == "занятий"


def test_csv_formula_injection():
    assert to_csv([["=1+1", "-1", "+7 999"]]) == "'=1+1;-1;'+7 999"
    assert to_csv([["a;b", 'c"d']]) == '"a;b";"c""d"'


# ---------- бизнес-логика ----------

async def test_payment_example_from_spec(session):
    # ТЗ п.7: цена 1600, оплата 6500 → 4 занятия + остаток 100 ₽
    anya = await repo.create_student(session, A, "Аня", 160000)
    await session.commit()
    res = await repo.apply_payment(session, A, anya.id, 650000)
    await session.commit()
    assert res.lessons == 4 and res.remainder == 10000
    assert anya.balance == 4 and anya.remainder == 10000
    assert anya.last_payment_amount == 650000 and anya.last_payment_lessons == 4
    # следующая оплата 1500: 100 + 1500 = 1600 → +1 занятие, остаток 0
    res2 = await repo.apply_payment(session, A, anya.id, 150000)
    await session.commit()
    assert res2.lessons == 1 and res2.remainder == 0 and res2.prev_remainder == 10000
    assert anya.balance == 5


async def test_charge_refund_and_debt(session):
    s = await repo.create_student(session, A, "Коля", 100000)
    await session.commit()
    await repo.charge_lesson(session, A, s.id)
    await session.commit()
    assert s.balance == -1  # долг (баланс уходит в минус)
    await repo.refund_lesson(session, A, s.id)
    await session.commit()
    assert s.balance == 0


async def test_change_price_future_only(session):
    s = await repo.create_student(session, A, "Ева", 160000)
    await session.commit()
    await repo.apply_payment(session, A, s.id, 320000)  # +2 занятия
    await session.commit()
    await repo.change_price(session, A, s.id, 200000)
    await session.commit()
    assert s.price == 200000
    assert s.balance == 2  # уже оплаченные занятия не пересчитываются


async def test_undo_stack_and_stale(session):
    s = await repo.create_student(session, A, "Аня", 160000)
    await session.commit()
    await repo.apply_payment(session, A, s.id, 650000)      # op1: +4, ост 100
    await repo.change_price(session, A, s.id, 180000)        # op2: цена 1800
    await session.commit()

    # отмена показанной операции (смена цены) по её id
    op = await repo.peek_last_operation(session, A)
    assert op.type == "price_change"
    r = await repo.undo_last_operation(session, A, op.id)
    await session.commit()
    assert r.status == "done" and s.price == 160000

    # устаревшая отмена: между показом и подтверждением появилась новая операция
    stale = await repo.peek_last_operation(session, A)      # теперь payment
    stale_id = stale.id
    await repo.charge_lesson(session, A, s.id)               # новая операция
    await session.commit()
    r2 = await repo.undo_last_operation(session, A, stale_id)
    await session.commit()
    assert r2.status == "stale"
    assert s.balance == 3  # 4 (оплата) − 1 (списание), откат не выполнен


async def test_multitenant_isolation(session):
    a_anya = await repo.create_student(session, A, "Аня", 160000)
    await session.commit()
    # у другого владельца имя «Аня» свободно
    b_anya = await repo.create_student(session, B, "Аня", 300000)
    await session.commit()
    assert b_anya is not None and b_anya.id != a_anya.id
    # у A имя занято без учёта регистра
    assert await repo.create_student(session, A, "аня", 999) is None
    # ученик A невидим для B и наоборот
    assert await repo.get_student(session, B, a_anya.id) is None
    assert await repo.get_student(session, A, b_anya.id) is None
    # операции изолированы
    await repo.apply_payment(session, A, a_anya.id, 160000)
    await session.commit()
    assert await repo.peek_last_operation(session, B) is None


async def test_sorts_and_search(session):
    s1 = await repo.create_student(session, A, "Борис", 200000)
    s2 = await repo.create_student(session, A, "Аня", 160000)
    await session.commit()
    await repo.apply_payment(session, A, s2.id, 800000)  # Аня: +5
    await session.commit()
    by_name = await repo.list_students(session, A, "name")
    assert [s.name for s in by_name] == ["Аня", "Борис"]
    by_bal = await repo.list_students(session, A, "bal")
    assert by_bal[0].name == "Аня"  # больше остаток — выше
    by_due = await repo.list_students(session, A, "due")
    assert by_due[0].name == "Борис"  # меньше остаток — «скоро оплата»
    found = await repo.search_students(session, A, "ор")
    assert [s.name for s in found] == ["Борис"]


async def test_export_rows(session):
    s = await repo.create_student(session, A, "Аня", 160000)
    await session.commit()
    await repo.apply_payment(session, A, s.id, 650000)
    await session.commit()
    students = await repo.list_students(session, A, "name")
    ops = await repo.get_all_operations(session, A)
    scsv = to_csv(students_rows(students))
    assert "Аня;1600,00;4" in scsv
    hcsv = to_csv(history_rows(ops, {s.id: s.name}))
    assert "Оплата;6500,00;4" in hcsv


def test_render_operation_undone_marker():
    class Op:
        type = "charge"
        lessons_delta = -1
        balance_after = 2
        remainder_after = 0
        amount = None
        new_price = None
        created_at = 1_700_000_000
        undone = True
        snapshot_before = {"price": 160000}
    line = render_operation(Op())
    assert line.startswith("❌") and "(отменено)" in line
