"""Контроль доступа: дешёвый отказ до замка и окончательное решение в
транзакции.

Два middleware вместо одного — это разделение по цене отказа, а не по вкусу.
Штатный поток чужих апдейтов обязан стоить один DEFERRED-SELECT вне замка;
внутрь транзакции пускается только редкий путь погашения инвайта.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update
from dishka import AsyncContainer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lesson_tracker.services import access

from .keys import ACCESS_DENIED

log = logging.getLogger(__name__)

_MAX_TRACKED_IDS = 256


class _DenialLog:
    """Первый отказ для каждого id пишем как WARNING, последующие — как DEBUG.

    Иначе спам-бот забил бы лог. Множество ограничено сверху: при переполнении
    очищаем целиком — снова получим всплеск WARNING'ов, что для личного бота
    приемлемее, чем неограниченный рост памяти.
    """

    def __init__(self) -> None:
        self._seen: set[int] = set()

    def log(self, key: int, message: str, *args: object) -> None:
        if len(self._seen) >= _MAX_TRACKED_IDS:
            self._seen.clear()
        if key in self._seen:
            log.debug(message, *args)
        else:
            self._seen.add(key)
            log.warning(message, *args)


def _invite_code_from_start(event: TelegramObject) -> str | None:
    """Код из deep-link `/start <код>`, если он там есть.

    Принимает и Update, и голое сообщение. Это не удобство: AccessMiddleware
    висит на `dp.update`, то есть получает именно Update, и версия, умевшая
    только Message, всегда возвращала None — единственный самостоятельный вход
    для приглашённого был мёртв, а тесты этого не видели, потому что дёргали
    middleware голым Message в обход диспетчера.
    """
    if isinstance(event, Update):
        event = event.message  # type: ignore[assignment]
    if not isinstance(event, Message) or not event.text:
        return None
    if not event.text.startswith("/start"):
        return None
    parts = event.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


class AccessGateMiddleware(BaseMiddleware):
    """Отсекает чужие апдейты ДО замка и до всякой транзакции.

    Проверка доступа жила внутри единицы работы, и это значило: каждый
    спам-месседж брал общий замок, открывал BEGIN IMMEDIATE (SELECT в
    is_allowed автобегинит транзакцию, а она у нас IMMEDIATE) и коммитил.
    Флаг ACCESS_DENIED прошлого раунда экономил только строку отметки — замок
    и транзакция оставались. Бот находится в поиске Telegram по имени (L10),
    поток чужих апдейтов штатен, и «✅ Оплата внесена» преподавателя стояла в
    очереди за спамом.

    Здесь тот же вопрос задаётся READONLY-соединением вне замка: отказ стоит
    один DEFERRED-SELECT и ничего больше. Редкий путь — посторонний с
    инвайт-кодом — пропускается внутрь: погашение должно остаться атомарным с
    обработкой апдейта, им занимается AccessMiddleware в транзакции.

    Регистрировать ПОСЛЕ ContainerMiddleware (нужен движок из контейнера) и
    ДО DbSessionMiddleware — в этом весь смысл.
    """

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids
        self._denials = _DenialLog()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        container: AsyncContainer = data["dishka_container"]
        engine = await container.get(AsyncEngine)
        if await access.is_allowed_readonly(engine, self.admin_ids, user.id):
            return await handler(event, data)

        if _invite_code_from_start(event) is not None:
            # Кандидат на погашение инвайта — редкость, ему можно внутрь:
            # действительность кода проверит AccessMiddleware в транзакции.
            return await handler(event, data)

        self._denials.log(
            user.id,
            "Отказано в доступе: user_id=%s username=%s тип=%s инвайт=False",
            user.id,
            user.username,
            type(event).__name__,
        )
        return None


class AccessMiddleware(BaseMiddleware):
    """Окончательное решение о доступе — внутри транзакции.

    Штатный поток чужих апдейтов сюда не доходит: их отсекает
    AccessGateMiddleware до замка. Здесь остаются два дела, которым нужна
    транзакция: погашение инвайта (атомарно с обработкой апдейта — упал
    обработчик, инвайт не сгорел) и повторная проверка допуска, закрывающая
    щель между READONLY-чтением гейта и этим моментом (/deny между ними —
    микросекунды, но проверка стоит один SELECT по уже открытой сессии).

    Всем недопущенным бот не отвечает ничего: сообщение вида «доступ запрещён»
    подтвердило бы, что бот жив, и приглашало бы долбиться дальше.

    Регистрировать ПОСЛЕ DbSessionMiddleware — берёт готовую сессию из data.
    """

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids
        # Свой на экземпляр, а не модульный синглтон: иначе состояние
        # рейт-лимита переживает пересборку диспетчера и течёт между тестами —
        # отказ, записанный в одном, глушит WARNING в другом, и проверка
        # «первый отказ виден в логе» проходит или падает от порядка запуска.
        self._denials = _DenialLog()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        session: AsyncSession = data["session"]
        allowed = await access.is_allowed(session, self.admin_ids, user.id)

        invite_attempted = False
        if not allowed:
            code = _invite_code_from_start(event)
            invite_attempted = code is not None
            if code and await access.redeem_invite(session, code, user.id, user.username):
                allowed = True
                log.info("Инвайт погашен: user_id=%s username=%s", user.id, user.username)

        if not allowed:
            self._denials.log(
                user.id,
                "Отказано в доступе: user_id=%s username=%s тип=%s инвайт=%s",
                user.id,
                user.username,
                type(event).__name__,
                invite_attempted,
            )
            # Сообщаем единице работы, что отмечать нечего: обработчик апдейт
            # не видел, и строка в processed_updates была бы платой за чужой
            # спам — записью в базу и блокировкой записи на каждое сообщение.
            data[ACCESS_DENIED] = True
            return None

        return await handler(event, data)
