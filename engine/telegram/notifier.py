"""Уведомления в Telegram: уровни, повторы при сбое, подавление одинаковых тревог."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

from engine.db import Db
from engine.models import Proposal
from engine.notifier import LogNotifier, format_proposal
from engine.report import split_message
from engine.telegram.api import Button, TelegramApi

log = logging.getLogger(__name__)

T = TypeVar("T")

_PREFIX = {"info": "", "warn": "⚠️ ", "alert": "🚨 "}


class TelegramNotifier:
    """Журнал пишется всегда; в Telegram — с префиксом уровня. Исключения наружу не выходят."""

    def __init__(self, api: TelegramApi, chat_id: int, db: Db, dedupe_s: int,
                 clock: Callable[[], float] = time.time, retries: int = 3,
                 retry_delay_s: float = 1.0):
        self.api = api
        self.chat_id = chat_id
        self.db = db
        self.dedupe_s = dedupe_s
        self.clock = clock
        self.retries = retries
        self.retry_delay_s = retry_delay_s
        self._log = LogNotifier(db)
        self._last_sent: dict[tuple[str, str], float] = {}

    async def _retry(self, what: str, call: Callable[[], Awaitable[T]]) -> T | None:
        """Вызов с повторами; после исчерпания — запись в audit и None, без исключения."""
        error: Exception | None = None
        for attempt in range(max(self.retries, 1)):
            if attempt:
                await asyncio.sleep(self.retry_delay_s)
            try:
                return await call()
            except Exception as exc:  # noqa: BLE001 — сеть Telegram не должна ронять движок
                error = exc
        log.error("Telegram недоступен (%s), %d попыток: %r", what, self.retries, error)
        try:
            self.db.audit("telegram_error", {"what": what, "attempts": self.retries,
                                             "error": repr(error)})
        except Exception:  # noqa: BLE001
            log.exception("Не удалось записать telegram_error")
        return None

    async def send(self, text: str, level: str = "info") -> None:
        await self._log.send(text, level)
        key = (level, text)
        now = self.clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < self.dedupe_s:
            return
        body = _PREFIX.get(level, "") + text
        delivered = True
        for part in split_message(body):
            sent = await self._retry("send_message",
                                     lambda part=part: self.api.send_message(self.chat_id, part))
            if sent is None:
                delivered = False
                break
        if delivered:
            self._last_sent[key] = now

    async def proposal(self, p: Proposal, ttl_min: int, hold_days: float) -> None:
        text = format_proposal(p, ttl_min, hold_days)
        self.db.audit("notify", {"text": text, "level": "info"})
        buttons = [[Button("Открыть", f"approve:{p.id}"), Button("Пропустить", f"reject:{p.id}")]]
        message_id = await self._retry(
            "proposal", lambda: self.api.send_message(self.chat_id, text, buttons))
        if message_id is not None:
            self.db.set_kv(f"tg_msg:proposal:{p.id}", message_id)

    async def send_document(self, filename: str, data: bytes, caption: str) -> None:
        await self._retry("send_document", lambda: self.api.send_document(
            self.chat_id, filename, data, caption))
