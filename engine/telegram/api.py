"""Узкий интерфейс к Telegram: нотификатор и обработчики зависят от него, а не от aiogram."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Button:
    text: str
    data: str  # callback_data


class TelegramApi(Protocol):
    async def send_message(self, chat_id: int, text: str,
                           buttons: list[list[Button]] | None = None) -> int:
        """Отправить сообщение, вернуть его message_id."""
        ...

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None: ...

    async def answer_callback(self, callback_id: str, text: str) -> None: ...

    async def send_document(self, chat_id: int, filename: str, data: bytes,
                            caption: str) -> None: ...
