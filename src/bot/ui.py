"""Буфер исходящих вызовов Telegram.

Обработчики больше не ходят в сеть сами: они складывают намерение отправить, а
сеть случается позже, когда транзакция уже закрыта (см. middlewares.py). Ради
этого шов и заведён — блокировка записи SQLite перестаёт держаться все ~300 мс
круга до Telegram и обратно, и потолок в единицы апдейтов в секунду исчезает.

Методы записи — обычные, не корутины. Это не мелочь стиля, а страховка: забытый
`await` на корутине хотя бы предупредит, а тихо не отправить сообщение здесь
невозможно в принципе.

Порядок вызовов сохраняется: буфер сливается по очереди, а не пачкой. Удаление
подсказки обязано случиться раньше отправки следующей, иначе экран мигает.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from .models import OutboxMessage, now_ts

log = logging.getLogger(__name__)

# Часовой «не доставлено»: отличает провал от законного None, который
# возвращают терпимые к ошибке вызовы (не изменившаяся правка, погасшие
# «часики»).
_FAILED = object()

# Сколько строка outbox «принадлежит» штатной отправке, прежде чем её увидит
# фоновый отправщик. С запасом больше круга до Telegram и тика поллера.
OUTBOX_GRACE = 60

# Бюджет загрузки документа. Секундный default сессии (__main__._SESSION_TIMEOUT
# = 15) — это per-request лимит ВСЕХ вызовов Bot API, ужатый ради быстрого
# обнаружения мёртвого long-poll. Мегабайтный .xlsx через прокси в него не
# влезает: выгрузка падала бы TimeoutError на каждой попытке, навсегда.
_UPLOAD_TIMEOUT = 120


def _dump(method: TelegramMethod[Any]) -> str:
    """Метод aiogram в JSON — только то, что вызывающий задал явно.

    exclude_unset обязателен, а не для компактности: незаданные поля у aiogram
    хранят не None, а sentinel `Default`, который просит подставить настройку
    бота в момент отправки. Он не сериализуем, да и не должен быть — при
    восстановлении поле снова окажется незаданным и снова возьмёт умолчание.
    """
    return method.model_dump_json(exclude_unset=True)


# raise — сбой отправки виден наверху;
# not_modified — «message is not modified» это успех: пользователь нажал ту же
#   кнопку второй раз, экран уже такой, какой нужно;
# quiet — вызов косметический (удаление подсказки), его провал ничего не значит.
OnError = Literal["raise", "not_modified", "quiet"]


@dataclass(slots=True)
class _LazyDocument:
    """Ещё не собранный документ: содержимое появится при отправке."""

    chat_id: int
    build: Callable[[], bytes]
    filename: str
    caption: str | None


@dataclass(slots=True)
class _Pending:
    method: TelegramMethod[Any] | _LazyDocument
    on_error: OnError = "raise"
    # Ключ FSM, которому после отправки нужно запомнить message_id ответа как
    # prompt_id. Единственное место, где id известен только после сети.
    prompt_for: StorageKey | None = None
    # Пережить ли этот вызов смерть процесса — см. models.OutboxMessage.
    durable: bool = False
    # id строки outbox, проставляется при записи в транзакцию.
    outbox_id: int | None = None
    # Чем заменить вызов, если он не прошёл. Отправкой, и только ею: сообщение
    # можно добавить в чат в любой момент, ничего не затерев, — в отличие от
    # правки, которая перерисовывает конкретный экран.
    fallback: SendMessage | None = None


class Responder:
    """Намерения области запроса. Один экземпляр на апдейт (dishka, REQUEST).

    Обычный класс, а не dataclass: dishka собирает объекты по аннотациям
    конструктора, и поля-списки он принял бы за зависимости, которые нужно
    откуда-то достать.
    """

    def __init__(self) -> None:
        self._queue: list[_Pending] = []
        self._delivered: list[int] = []
        # Заполняется сливом, читается middleware: (ключ FSM, id подсказки).
        self.prompt_updates: list[tuple[StorageKey, int]] = []

    def _message(
        self, chat_id: int, text: str, reply_markup: Any, parse_mode: str | None
    ) -> SendMessage:
        # parse_mode передаём только когда он задан: иначе поле стало бы
        # «явно None», то есть запретом на разметку, вместо умолчания бота.
        extra: dict[str, Any] = {"parse_mode": parse_mode} if parse_mode else {}
        return SendMessage(chat_id=chat_id, text=text, reply_markup=reply_markup, **extra)

    def reply(
        self,
        message: Message,
        text: str,
        *,
        reply_markup: Any = None,
        parse_mode: str | None = None,
        prompt_for: StorageKey | None = None,
    ) -> None:
        """Сообщение, рисующее экран: подсказка, меню, список, замечание о вводе.

        НЕ персистентно. Такой текст осмысленен только сейчас: доставленное
        через минуту «Введите стоимость занятия:» приходит в чат, где диалога
        уже нет, — пользователь успел нажать «❌ Отмена» и заняться другим, а
        его следующая фраза уходит в пустоту. Это ровно та беда, из-за которой
        из очереди убраны правки экрана; текст подсказки ничем от них не
        отличается.

        prompt_for — для потоков, где ответ сам становится подсказкой и его id
        нужен следующему шагу, чтобы её удалить.
        """
        self._queue.append(
            _Pending(
                self._message(message.chat.id, text, reply_markup, parse_mode),
                prompt_for=prompt_for,
            )
        )

    def confirm(
        self,
        message: Message,
        text: str,
        *,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        """Сообщение, несущее результат уже зафиксированной операции.

        Персистентно: обязано дойти, пусть и позже. Не увидев подтверждения,
        преподаватель вводит сумму заново — и оплата задваивается, а это для
        денег хуже, чем дубль сообщения.

        Отдельный метод, а не флаг у reply: разница здесь смысловая, и
        оставлять её на умолчании нельзя ни в ту, ни в другую сторону. Пока
        персистентным было всё подряд, очередь дожимала подсказки диалога;
        стоило бы сделать умолчанием обратное — молча терялись бы
        подтверждения оплат.
        """
        self._queue.append(
            _Pending(
                self._message(message.chat.id, text, reply_markup, parse_mode),
                durable=True,
            )
        )

    def edit(
        self, message: Message, text: str, reply_markup: Any = None, *, durable: bool = False
    ) -> None:
        """Правка сообщения, терпимая к повторному нажатию той же кнопки.

        НЕ персистентна, в отличие от отправки. Правка — это состояние экрана в
        конкретном сообщении, а не факт; воспроизведённая через минуту, она
        затирает то, куда пользователь успел уйти. Сценарий не гипотетический:
        списание не доставилось, человек нажал «⬅️ К списку», а отложенная
        правка вернула ему карточку ученика поверх списка — с устаревшим
        балансом и старыми кнопками. Очередь несёт сообщения, а не экраны.

        Не прошла — содержимое уходит тем же сливом отдельным сообщением
        (fallback). Так экран не затирается задним числом, а пользователь всё
        равно видит то, что должен: сорванная правка больше не оставляет ни
        невидимого диалога, ни устаревшей карточки.

        durable=True — когда правка несёт результат зафиксированной операции
        (списание, возврат, отмена). Тогда запасное сообщение ещё и попадает в
        очередь: правку нельзя воспроизвести позже, не отбросив человека на
        покинутый экран, а сообщение — можно.
        """
        fallback = self._message(message.chat.id, text, reply_markup, None)
        self._queue.append(
            _Pending(
                EditMessageText(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    text=text,
                    reply_markup=reply_markup,
                ),
                on_error="not_modified",
                durable=durable,
                fallback=fallback,
            )
        )

    def callback(
        self, cb: CallbackQuery, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        """Погасить «часики» на кнопке, при необходимости с текстом.

        Провал не важен: Telegram принимает ответ на нажатие считаные секунды,
        и если окно закрылось, повторять нечего — «часики» гаснут сами.
        """
        self._queue.append(
            _Pending(
                AnswerCallbackQuery(callback_query_id=cb.id, text=text, show_alert=show_alert),
                on_error="quiet",
            )
        )

    def document(
        self,
        message: Message,
        build: Callable[[], bytes],
        filename: str,
        *,
        caption: str | None = None,
    ) -> None:
        """Файл выгрузки. Собирается ЛЕНИВО, при сливе.

        build зовётся не здесь, а во время отправки, и в отдельном потоке.
        Раньше .xlsx собирался прямо в обработчике — то есть внутри общего
        замка записи и открытой транзакции: пара тысяч операций означала
        секунды, на которые вставали все остальные апдейты, фоновый отправщик и
        проба сторожа, да ещё и с занятым циклом событий. Именно от такого
        удержания замка уходил весь разворот «обработчик → коммит → отправка»,
        и держать в нём CPU-работу — то же самое, только без сети.

        В очередь не идёт: это мегабайты, которым нечего делать в базе, а
        повторить выгрузку пользователь может сам.

        Зато о провале он узнаёт — запасным сообщением. Раньше сбой поднимался
        наверх, но до человека не доходил: ответ на нажатие кнопки к тому
        моменту уже отправлен, а Telegram принимает его ровно один раз, так что
        обработчик ошибок молча получал отказ. Пользователь видел остановившиеся
        «часики» и ничего больше.
        """
        self._queue.append(
            _Pending(
                _LazyDocument(
                    chat_id=message.chat.id, build=build, filename=filename, caption=caption
                ),
                fallback=self._message(
                    message.chat.id,
                    "⚠️ Не получилось отправить файл. Попробуйте выгрузить ещё раз.",
                    None,
                    None,
                ),
            )
        )

    def delete(self, chat_id: int, message_id: int | None) -> None:
        """Убрать сообщение, если оно вообще было. Провал не важен."""
        if not message_id:
            return
        self._queue.append(
            _Pending(
                DeleteMessage(chat_id=chat_id, message_id=message_id),
                on_error="quiet",
            )
        )

    async def persist(self, session: AsyncSession) -> None:
        """Записать обещания доставки в ту же транзакцию, что и операцию.

        Зовётся middleware ПЕРЕД коммитом: либо изменение и обещание ответить
        зафиксированы вместе, либо ни то ни другое. Иначе смерть процесса между
        коммитом и отправкой оставляла бы применённую операцию без ответа —
        а именно этого разворот порядка и не должен был стоить.
        """
        # next_attempt_at со сдвигом: штатная отправка идёт сразу после коммита
        # и занимает сотни миллисекунд. Строка, доступная отправщику мгновенно,
        # попадала бы под тик поллера прямо в это окно — и пользователь получал
        # бы один и тот же ответ дважды в обычном режиме, а не после падения.
        # Отправщик обязан видеть только то, что штатный путь уже не удалит.
        due = now_ts() + OUTBOX_GRACE
        rows = []
        for item in self._queue:
            if not item.durable:
                continue
            # Персистентна всегда ОТПРАВКА: у правки берём её запасное
            # сообщение. Это не деталь реализации, а свойство: воспроизвести
            # позже можно только то, что ничего не затирает. Проверка тут же —
            # чтобы будущий durable-вызов другого типа сломался здесь, а не в
            # отправщике, который не смог бы такую строку оживить.
            method = item.fallback or item.method
            if not isinstance(method, SendMessage):
                raise TypeError(f"В очередь доставки годится только SendMessage, а не {method!r}")
            rows.append(
                (
                    item,
                    OutboxMessage(
                        method=type(method).__name__,
                        payload=_dump(method),
                        next_attempt_at=due,
                    ),
                )
            )
        if not rows:
            return
        session.add_all([row for _item, row in rows])
        # flush, а не commit: id нужен здесь и сейчас, чтобы после успешной
        # отправки было что удалить, а фиксирует по-прежнему единица работы.
        await session.flush()
        for item, row in rows:
            item.outbox_id = row.id

    def delivered(self) -> list[int]:
        """id строк outbox, чьи сообщения ушли. Их удаляет middleware."""
        return self._delivered

    def discard(self) -> None:
        """Выбросить накопленное, не отправляя.

        Зовётся при откате: апдейт не состоялся, значит и говорить о нём
        пользователю нечего.
        """
        self._queue.clear()

    async def flush(self, bot: Bot) -> None:
        """Отправить накопленное по порядку.

        Буфер опустошается ДО отправки: повторный слив (например, из
        обработчика ошибок) не должен отправить то же самое дважды.

        Ошибка одного вызова не отменяет остальные: транзакция давно закрыта, и
        бросить всё на первом сбое означало бы просто не отправить то, что ещё
        могло уйти. Ловим Exception целиком, а не TelegramAPIError — апдейт уже
        применён, и никакая беда доставки не имеет права его отменить.

        Наверх отсюда ничего не поднимается. Раньше поднималось — и это была
        ошибка: сорванная правка экрана превращалась в «Не получилось выполнить
        действие» поверх успешно применённого списания, то есть приглашала
        списать урок второй раз. Вместо эскалации у вызова есть запасной
        вариант: содержимое уходит отдельным сообщением. Оно ничего не затирает
        и доходит до человека без помощи обработчика ошибок — которому,
        к слову, ответить уже нечем, если «часики» на кнопке погашены.
        """
        queued, self._queue = self._queue, []
        for item in queued:
            result = await self._deliver(bot, item)
            if result is _FAILED:
                continue
            if item.outbox_id is not None:
                self._delivered.append(item.outbox_id)
            if item.prompt_for is not None and isinstance(result, Message):
                self.prompt_updates.append((item.prompt_for, result.message_id))

    async def _deliver(self, bot: Bot, item: _Pending) -> Any:
        """Отправить намерение, при неудаче — его запасной вариант."""
        try:
            return await self._send(bot, item)
        except Exception:
            log.warning(
                "Не удалось отправить %s%s",
                type(item.method).__name__,
                "" if item.fallback is None else " — пробую отдельным сообщением",
                exc_info=True,
            )
        if item.fallback is None:
            return _FAILED
        try:
            return await bot(item.fallback)
        except Exception:
            log.warning(
                "Запасное сообщение тоже не ушло%s",
                " — останется в очереди" if item.outbox_id else "",
                exc_info=True,
            )
            return _FAILED

    @staticmethod
    async def _send(bot: Bot, item: _Pending) -> Any:
        method = item.method
        if isinstance(method, _LazyDocument):
            # to_thread: сборка книги openpyxl — чистый CPU, и в цикле событий
            # ей делать нечего. Замка здесь уже нет, но соседние апдейты всё
            # равно ждали бы своей очереди на исполнение.
            data = await asyncio.to_thread(method.build)
            method = SendDocument(
                chat_id=method.chat_id,
                document=BufferedInputFile(data, method.filename),
                caption=method.caption,
            )
            # Отдельный бюджет: документ — единственный тяжёлый вызов, и общий
            # per-request default сессии (15 с, ужат ради быстрого обнаружения
            # мёртвого long-poll — см. __main__._SESSION_TIMEOUT) мегабайтной
            # загрузке через прокси заведомо мал.
            kwargs: dict[str, Any] = {"request_timeout": _UPLOAD_TIMEOUT}
        else:
            kwargs = {}
        try:
            return await bot(method, **kwargs)
        except TelegramBadRequest as exc:
            if item.on_error == "not_modified" and "not modified" in str(exc).lower():
                return None
            if item.on_error == "quiet":
                return None
            raise
        except TelegramAPIError:
            if item.on_error == "quiet":
                return None
            raise
