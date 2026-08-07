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

import logging
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
from aiogram.types import CallbackQuery, InputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from .models import OutboxMessage

log = logging.getLogger(__name__)


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
class _Pending:
    method: TelegramMethod[Any]
    on_error: OnError = "raise"
    # Ключ FSM, которому после отправки нужно запомнить message_id ответа как
    # prompt_id. Единственное место, где id известен только после сети.
    prompt_for: StorageKey | None = None
    # Пережить ли этот вызов смерть процесса — см. models.OutboxMessage.
    durable: bool = False
    # id строки outbox, проставляется при записи в транзакцию.
    outbox_id: int | None = None


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

    def answer(
        self,
        message: Message,
        text: str,
        *,
        reply_markup: Any = None,
        parse_mode: str | None = None,
        prompt_for: StorageKey | None = None,
    ) -> None:
        """Новое сообщение в тот же чат.

        prompt_for — для потоков, где ответ сам становится подсказкой и его id
        нужен следующему шагу, чтобы её удалить.
        """
        # parse_mode передаём только когда он задан: иначе поле стало бы
        # «явно None», то есть запретом на разметку, вместо умолчания бота.
        extra: dict[str, Any] = {"parse_mode": parse_mode} if parse_mode else {}
        self._queue.append(
            _Pending(
                SendMessage(chat_id=message.chat.id, text=text, reply_markup=reply_markup, **extra),
                prompt_for=prompt_for,
                durable=True,
            )
        )

    def edit(self, message: Message, text: str, reply_markup: Any = None) -> None:
        """Правка сообщения, терпимая к повторному нажатию той же кнопки."""
        self._queue.append(
            _Pending(
                EditMessageText(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    text=text,
                    reply_markup=reply_markup,
                ),
                on_error="not_modified",
                durable=True,
            )
        )

    def callback(
        self, cb: CallbackQuery, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        """Погасить «часики» на кнопке, при необходимости с текстом."""
        self._queue.append(
            _Pending(AnswerCallbackQuery(callback_query_id=cb.id, text=text, show_alert=show_alert))
        )

    def document(
        self, message: Message, document: InputFile, *, caption: str | None = None
    ) -> None:
        self._queue.append(
            _Pending(SendDocument(chat_id=message.chat.id, document=document, caption=caption))
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

    def pending(self) -> int:
        """Сколько намерений ждёт отправки — для тестов и диагностики."""
        return len(self._queue)

    async def persist(self, session: AsyncSession) -> None:
        """Записать обещания доставки в ту же транзакцию, что и операцию.

        Зовётся middleware ПЕРЕД коммитом: либо изменение и обещание ответить
        зафиксированы вместе, либо ни то ни другое. Иначе смерть процесса между
        коммитом и отправкой оставляла бы применённую операцию без ответа —
        а именно этого разворот порядка и не должен был стоить.
        """
        rows = [
            (item, OutboxMessage(method=type(item.method).__name__, payload=_dump(item.method)))
            for item in self._queue
            if item.durable
        ]
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

        Ошибка одного вызова не отменяет остальные. Раньше исключение отсюда
        рвало транзакцию и это было осмысленно; теперь транзакция давно
        закрыта, и бросить всё на первом сбое означало бы просто не отправить
        то, что ещё могло уйти. Персистентные вызовы останутся в outbox и
        будут дожаты, о прочих — запись в лог.

        Ловим Exception целиком, а не TelegramAPIError: на этом этапе апдейт
        уже применён и зафиксирован, и никакая беда доставки не должна его
        отменять — тем более превращаться в «попробуйте ещё раз» для
        операции, которая на самом деле удалась.
        """
        queued, self._queue = self._queue, []
        for item in queued:
            try:
                result = await self._send(bot, item)
            except Exception:
                log.warning(
                    "Не удалось отправить %s%s",
                    type(item.method).__name__,
                    " — останется в очереди" if item.outbox_id else "",
                    exc_info=True,
                )
                continue
            if item.outbox_id is not None:
                self._delivered.append(item.outbox_id)
            if item.prompt_for is not None and isinstance(result, Message):
                self.prompt_updates.append((item.prompt_for, result.message_id))

    @staticmethod
    async def _send(bot: Bot, item: _Pending) -> Any:
        try:
            return await bot(item.method)
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
