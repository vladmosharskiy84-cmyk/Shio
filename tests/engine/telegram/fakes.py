"""Подделка Telegram-клиента: записывает отправки, умеет падать заданное число раз."""
from __future__ import annotations

from engine.telegram.api import Button


class TelegramBadRequest(Exception):
    """Как одноимённая ошибка aiogram: Telegram отверг запрос (повтор не поможет)."""


class FakeTelegramApi:
    """fail_times первых вызовов падают; error — фабрика исключения (по умолчанию сетевая)."""

    def __init__(self, fail_times: int = 0, error=None):
        self.fail_times = fail_times
        self.error = error or (lambda: ConnectionError("telegram недоступен"))
        self.attempts = 0
        self.messages: list[dict] = []
        self.documents: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[tuple[str, str]] = []
        self._next_id = 100

    def _maybe_fail(self) -> None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error()

    async def send_message(self, chat_id: int, text: str,
                           buttons: list[list[Button]] | None = None) -> int:
        self._maybe_fail()
        self._next_id += 1
        self.messages.append({"chat_id": chat_id, "text": text, "buttons": buttons,
                              "message_id": self._next_id})
        return self._next_id

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self._maybe_fail()
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def answer_callback(self, callback_id: str, text: str) -> None:
        self._maybe_fail()
        self.answers.append((callback_id, text))

    async def send_document(self, chat_id: int, filename: str, data: bytes, caption: str) -> None:
        self._maybe_fail()
        self.documents.append({"chat_id": chat_id, "filename": filename, "data": data,
                               "caption": caption})
