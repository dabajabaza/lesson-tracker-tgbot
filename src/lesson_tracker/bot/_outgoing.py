"""Типы очереди исходящих: что именно Responder складывает до отправки.

Отдельно от ui.py, потому что это словарь понятий очереди, а не логика
доставки: сюда смотрят и Responder, когда кладёт намерение, и runtime/outbox.py,
когда восстанавливает недоставленное. Логика отправки — в ui.py.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import SendMessage, TelegramMethod

# Часовой «не доставлено»: отличает провал от законного None, который
# возвращают терпимые к ошибке вызовы (не изменившаяся правка, погасшие
# «часики»).
FAILED = object()

# Бюджет загрузки документа. Секундный default сессии (__main__._SESSION_TIMEOUT
# = 15) — это per-request лимит ВСЕХ вызовов Bot API, ужатый ради быстрого
# обнаружения мёртвого long-poll. Мегабайтный .xlsx через прокси в него не
# влезает: выгрузка падала бы TimeoutError на каждой попытке, навсегда.
UPLOAD_TIMEOUT = 120


def dump(method: TelegramMethod[Any]) -> str:
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
class LazyDocument:
    """Ещё не собранный документ: содержимое появится при отправке."""

    chat_id: int
    build: Callable[[], bytes]
    filename: str
    caption: str | None


@dataclass(slots=True)
class Pending:
    method: TelegramMethod[Any] | LazyDocument
    on_error: OnError = "raise"
    # Ключ FSM, которому после отправки нужно запомнить message_id ответа как
    # prompt_id. Единственное место, где id известен только после сети.
    prompt_for: StorageKey | None = None
    # Пережить ли этот вызов смерть процесса — см. db.models.OutboxMessage.
    durable: bool = False
    # id строки outbox, проставляется при записи в транзакцию.
    outbox_id: int | None = None
    # Чем заменить вызов, если он не прошёл. Отправкой, и только ею: сообщение
    # можно добавить в чат в любой момент, ничего не затерев, — в отличие от
    # правки, которая перерисовывает конкретный экран.
    fallback: SendMessage | None = None
