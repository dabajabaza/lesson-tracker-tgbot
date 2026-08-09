"""DI-контейнер (dishka): приложение и область запроса.

Единица работы: одна область запроса = одна транзакция. Сессию отдаёт
request-скоуп провайдер, но КОММИТИТ её не он, а DbSessionMiddleware — внутри
общего замка записи. Это осознанное расхождение с chain-health: там коммит
живёт в провайдере. У нас замок обязан накрывать коммит: уехав в закрытие
скоупа, коммит оказался бы за пределами замка, и следующий апдейт успевал бы
прочитать старый баланс до фиксации предыдущего — классический lost update.

Провайдер остаётся страховкой: откат при исключении, чтобы недописанный хвост
не утёк, каким бы путём ни умер обработчик.
"""

from collections.abc import AsyncIterable

from aiogram.fsm.storage.base import BaseStorage
from dishka import AsyncContainer, Provider, Scope, make_async_container, provide
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .bot.storage import SqlAlchemyStorage
from .bot.ui import Responder
from .config import Config
from .db.engine import create_db
from .services import HistoryService, PaymentService, StudentService, ViewPrefService


class AppProvider(Provider):
    scope = Scope.APP

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

    @provide
    def config(self) -> Config:
        return self._config

    @provide
    async def db(
        self, config: Config
    ) -> AsyncIterable[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
        """Движок закрывается вместе с контейнером.

        Генератор, а не обычный провайдер: финализатор dishka вызывает только у
        генераторов. Пока его не было, `container.close()` возвращался с
        открытым пулом — соединения aiosqlite и их рабочие потоки никто не
        закрывал, WAL на выходе не чекпойнтился, а в тестах каждый закрытый
        контейнер оставлял по потоку и по открытому дескриптору базы.
        """
        engine, sessionmaker = create_db(config.db_url)
        yield engine, sessionmaker
        await engine.dispose()

    @provide
    def engine(self, db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> AsyncEngine:
        return db[0]

    @provide
    def sessionmaker(
        self, db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
    ) -> async_sessionmaker[AsyncSession]:
        return db[1]


class RequestProvider(Provider):
    scope = Scope.REQUEST

    @provide
    async def session(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> AsyncIterable[AsyncSession]:
        """Сессия на область запроса. Без коммита — его делает middleware.

        dishka финализирует генераторы через asend: исключение обработчика
        приходит как ЗНАЧЕНИЕ yield, а не бросается внутрь. try/except вокруг
        yield его бы не увидел.
        """
        async with session_factory() as session:
            exception = yield session
            if exception is not None:
                await session.rollback()

    # Сервисы: dishka собирает их по типам аргументов конструктора, поэтому
    # PaymentService и HistoryService получают тот же StudentService, что и
    # обработчик, — а тот ту же сессию. Один экземпляр на область запроса.
    students = provide(StudentService)
    payments = provide(PaymentService)
    history = provide(HistoryService)
    prefs = provide(ViewPrefService)

    # Буфер исходящих вызовов Telegram: обработчики складывают в него намерения,
    # middleware сливает после коммита. Один на область запроса — как сессия.
    responder = provide(Responder)

    @provide
    def fsm_storage(self, session: AsyncSession) -> BaseStorage:
        """FSM-хранилище поверх ТОЙ ЖЕ сессии, что и бизнес-логика.

        Единственный писатель в SQLite на весь апдейт — ради этого затевался
        переезд: 07.08.2026 отдельная FSM-сессия упёрлась в блокировку записи
        сессии запроса, и бот лёг с «database is locked».
        """
        return SqlAlchemyStorage(session)


def build_container(config: Config) -> AsyncContainer:
    return make_async_container(AppProvider(config), RequestProvider())
