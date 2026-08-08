"""Подключение к БД. По умолчанию SQLite (aiosqlite); URL можно указать любой,
поддерживаемый SQLAlchemy async — в т.ч. PostgreSQL (asyncpg) при переезде (п.19 ТЗ)."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Помечает соединение как заведомо читающее: транзакция откроется как DEFERRED
# и блокировку записи не тронет. Ставится через execution_options — см.
# storage.ReadOnlyFsmView, единственного законного потребителя.
READONLY = "lesson_tracker_readonly"


def create_db(db_url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url, echo=False)
    if db_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            # WAL + busy_timeout: устойчивость к «database is locked» при
            # параллельных апдейтах.
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
            # Отключаем собственное управление транзакциями у драйвера pysqlite.
            # Без этого SAVEPOINT сломан: драйвер не открывает настоящую
            # транзакцию, RELEASE SAVEPOINT фактически фиксирует запись, и
            # последующий session.rollback() её уже не отменяет. Проверено:
            # ученик, созданный через begin_nested, переживал откат — то есть
            # «одна область запроса = одна транзакция» переставала быть правдой
            # ровно там, где транзакция нужнее всего. См. tests/test_unit_of_work.
            dbapi_conn.isolation_level = None

        @event.listens_for(engine.sync_engine, "begin")
        def _sqlite_begin(conn):  # noqa: ANN001
            # Раз драйвер больше не начинает транзакции сам, начинаем явно.
            #
            # IMMEDIATE, то есть забирая блокировку записи на входе. Обычный
            # BEGIN (DEFERRED) берёт её только на первой записи, уже прочитав
            # данные. Два таких писателя читают общий снимок, и второй получает
            # не ожидание, а мгновенный «database is locked»: ждать нечего, его
            # снимок устарел, busy_timeout тут не помогает вовсе. С IMMEDIATE
            # опоздавший честно ждёт на busy_timeout.
            #
            # Внутри процесса до этого не доходит — пишущие апдейты сериализует
            # замок в DbSessionMiddleware. IMMEDIATE прикрывает писателя со
            # стороны: миграцию на старте, ручной скрипт над боевой базой.
            #
            # Кроме соединений, помеченных READONLY. Им нужен DEFERRED: в WAL
            # читатель не берёт блокировку записи вовсе, а IMMEDIATE взял бы её
            # и на чистом SELECT. Ровно это и происходило с хранилищем
            # диспетчера (storage.ReadOnlyFsmView): оно читает raw_state ДО
            # того, как апдейт войдёт в общий замок, и каждый апдейт молча
            # забирал блокировку записи снаружи всякой сериализации. Замок
            # переставал что-либо гарантировать, а сверх busy_timeout апдейт
            # умирал с «database is locked» там, где нет ни отката, ни отметки
            # идемпотентности.
            if conn.get_execution_options().get(READONLY):
                conn.exec_driver_sql("BEGIN")
            else:
                conn.exec_driver_sql("BEGIN IMMEDIATE")

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessionmaker
