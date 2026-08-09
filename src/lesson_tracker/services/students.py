"""Ученики: чтение, создание, переименование стоимости.

Мультитенантность держится на том, что owner_id — обязательный аргумент каждого
метода, а не поле сервиса: сервис живёт в области запроса, но принадлежность
данных определяется отправителем апдейта, и делать её неявной опасно.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lesson_tracker.db.models import Student
from lesson_tracker.domain.constants import MIN_PRICE

from ._operations import record_operation


class StudentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, owner_id: int, sid: int) -> Student | None:
        return await self._session.scalar(
            select(Student).where(Student.owner_id == owner_id, Student.id == sid)
        )

    # Не `list`: имя метода затеняет встроенный list внутри тела класса,
    # и аннотации соседних методов начинают резолвиться в этот метод.
    async def list_all(self, owner_id: int, sort: str = "name") -> list[Student]:
        rows = list(
            await self._session.scalars(select(Student).where(Student.owner_id == owner_id))
        )
        # Учеников немного — сортируем в Python (п.15 ТЗ).
        if sort == "bal":
            rows.sort(key=lambda s: s.name.lower())
            rows.sort(key=lambda s: -s.balance)
        elif sort == "due":
            rows.sort(key=lambda s: s.name.lower())
            rows.sort(key=lambda s: s.balance)
        else:
            rows.sort(key=lambda s: s.name.lower())
        return rows

    async def search(self, owner_id: int, query: str) -> list[Student]:
        q = query.strip().lower()
        return [s for s in await self.list_all(owner_id, "name") if q in s.name_lower]

    async def find_by_name(self, owner_id: int, name: str) -> Student | None:
        return await self._session.scalar(
            select(Student).where(
                Student.owner_id == owner_id, Student.name_lower == name.strip().lower()
            )
        )

    async def create(self, owner_id: int, name: str, price: int) -> Student | None:
        """Создаёт ученика; None, если имя пустое или у этого владельца занято
        (без учёта регистра)."""
        clean = (name or "").strip()
        if not clean or price < MIN_PRICE:
            return None
        if await self.find_by_name(owner_id, clean):
            return None
        s = Student(owner_id=owner_id, name=clean, name_lower=clean.lower(), price=price)
        # Гонка на UniqueConstraint(owner_id, name_lower) между проверкой выше и
        # вставкой. Сервисы не коммитят (единица работы фиксируется в
        # middleware), поэтому нарушение констрейнта ловим в момент записи:
        # SAVEPOINT откатывает только вставку, транзакция запроса живёт дальше.
        # На SQLite это работает лишь потому, что в db.py отключено собственное
        # управление транзакциями драйвера — иначе RELEASE SAVEPOINT фиксирует
        # запись и общий откат её уже не отменяет.
        try:
            async with self._session.begin_nested():
                self._session.add(s)
        except IntegrityError:
            return None
        return s

    async def change_price(self, owner_id: int, sid: int, new_price: int) -> Student | None:
        """Изменение стоимости (п.12): только для будущих оплат, история не
        пересчитывается."""
        s = await self.get(owner_id, sid)
        if not s or new_price < MIN_PRICE:
            return None
        await record_operation(
            self._session,
            s,
            type="price_change",
            lessons_delta=0,
            balance_after=s.balance,
            remainder_after=s.remainder,
            new_price=new_price,
        )
        s.price = new_price
        return s
