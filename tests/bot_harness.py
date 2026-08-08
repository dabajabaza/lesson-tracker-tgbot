"""Обвязка для прогона настоящего диспетчера без сети.

Заведомо фальшивый, но синтаксически корректный токен: Bot проверяет форму на
конструкторе, а к сети не ходит вовсе — её подменяет RecordingSession.
"""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.methods.get_me import GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

FAKE_BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105


class RecordingSession(BaseSession):
    """Двойник сессии aiogram: записывает исходящие вызовы API и возвращает
    ответы такой формы, чтобы устроить внутреннюю валидацию aiogram.

    Наследует BaseSession, а не изображает его по форме: тогда `Bot.session`
    принимает объект без нареканий, а `Bot.__call__` работает как есть.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        # Ответы на успешные вызовы, парой к методу: реконструировать id по
        # порядку вызовов нельзя — упавшие вызовы попадают в calls, но ответа
        # (и id) не получают.
        self.responses: list[tuple[TelegramMethod[Any], Any]] = []
        self.fail_on: dict[str, Exception] = {}
        self._next_message_id = 5000

    def _next_message(self, chat_id: int, text: str | None) -> Message:
        self._next_message_id += 1
        return Message(
            message_id=self._next_message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.calls.append(method)
        name = type(method).__name__
        if name in self.fail_on:
            raise self.fail_on[name]

        result: Any = True
        if isinstance(method, SendMessage | EditMessageText):
            assert isinstance(method.chat_id, int)
            result = self._next_message(method.chat_id, method.text)
        elif isinstance(method, GetMe):
            result = TgUser(id=1, is_bot=True, first_name="Bot", username="testbot")
        self.responses.append((method, result))
        return result

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        pass

    def calls_of(self, name: str) -> list[TelegramMethod[Any]]:
        return [m for m in self.calls if type(m).__name__ == name]

    def sent_texts(self) -> list[str]:
        """Тексты ВСЕХ вызовов с полем text — и правок, и упавших.

        Вызов записывается до того, как сработает fail_on, поэтому по этому
        списку нельзя отличить «пользователь увидел» от «мы пытались
        отправить». Там, где разница важна, берите calls_of("SendMessage").
        """
        return [text for m in self.calls if (text := getattr(m, "text", None)) is not None]

    def sent_message_ids(self, containing: str) -> list[int]:
        """id сообщений, реально отправленных SendMessage с данным текстом."""
        return [
            resp.message_id
            for method, resp in self.responses
            if isinstance(method, SendMessage) and containing in (method.text or "")
        ]

    def clear(self) -> None:
        self.calls.clear()
        self.responses.clear()


def make_update_message(
    text: str,
    *,
    user_id: int,
    chat_id: int | None = None,
    chat_type: str = "private",
    update_id: int = 1,
) -> Update:
    chat = Chat(id=chat_id if chat_id is not None else user_id, type=chat_type)
    user = TgUser(id=user_id, is_bot=False, first_name="Test")
    message = Message(
        message_id=update_id, date=datetime.now(UTC), chat=chat, from_user=user, text=text
    )
    return Update(update_id=update_id, message=message)


def make_update_callback(
    data: str,
    *,
    user_id: int,
    chat_id: int | None = None,
    chat_type: str = "private",
    message_id: int = 9000,
    update_id: int = 1,
) -> Update:
    origin = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id if chat_id is not None else user_id, type=chat_type),
        text="origin",
    )
    user = TgUser(id=user_id, is_bot=False, first_name="Test")
    callback = CallbackQuery(
        id=str(update_id), from_user=user, chat_instance="x", data=data, message=origin
    )
    return Update(update_id=update_id, callback_query=callback)


@dataclass
class BotHarness:
    """Гоняет настоящий Dispatcher — собранный той же функцией, что и в
    проде, — через поддельные апдейты и запоминает, что бот ответил бы."""

    bot: Bot
    dp: Dispatcher
    session: RecordingSession
    # Замок записи, с которым собран диспетчер. Тесты отправщика обязаны
    # использовать его же — свежий Lock() был бы вторым писателем.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _next_update_id: int = field(default=1)

    def _update_id(self) -> int:
        self._next_update_id += 1
        return self._next_update_id

    async def send(self, text: str, *, user_id: int = 1, chat_type: str = "private") -> None:
        await self.dp.feed_update(
            self.bot,
            make_update_message(
                text, user_id=user_id, chat_type=chat_type, update_id=self._update_id()
            ),
        )

    async def click(
        self,
        data: str,
        *,
        user_id: int = 1,
        chat_type: str = "private",
        message_id: int = 9000,
    ) -> None:
        await self.dp.feed_update(
            self.bot,
            make_update_callback(
                data,
                user_id=user_id,
                chat_type=chat_type,
                message_id=message_id,
                update_id=self._update_id(),
            ),
        )
