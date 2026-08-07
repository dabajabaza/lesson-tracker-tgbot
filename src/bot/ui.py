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


class Responder:
    """Намерения области запроса. Один экземпляр на апдейт (dishka, REQUEST).

    Обычный класс, а не dataclass: dishka собирает объекты по аннотациям
    конструктора, и поля-списки он принял бы за зависимости, которые нужно
    откуда-то достать.
    """

    def __init__(self) -> None:
        self._queue: list[_Pending] = []
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
        self._queue.append(
            _Pending(
                SendMessage(
                    chat_id=message.chat.id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                ),
                prompt_for=prompt_for,
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
        """
        queued, self._queue = self._queue, []
        for item in queued:
            result = await self._send(bot, item)
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
