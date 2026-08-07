"""Настройки отображения списка: сортировка и страница (п.15 ТЗ)."""

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import UiPref

DEFAULT_SORT = "name"
DEFAULT_PAGE = 0


class ViewPrefService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, owner_id: int) -> tuple[str, int]:
        p = await self._session.get(UiPref, owner_id)
        return (p.sort, p.page) if p else (DEFAULT_SORT, DEFAULT_PAGE)

    async def set(self, owner_id: int, sort: str, page: int) -> None:
        p = await self._session.get(UiPref, owner_id)
        if p is None:
            self._session.add(UiPref(owner_id=owner_id, sort=sort, page=page))
        else:
            p.sort = sort
            p.page = page
