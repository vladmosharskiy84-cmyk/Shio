"""Уведомления в Telegram: уровни, очередь доставки, повторы, подавление одинаковых тревог.

Доставка отвязана от главного цикла: send / proposal / send_document сразу пишут журнал и
audit, а сообщение кладут в ограниченную очередь. Её разбирает фоновая задача (запускается
при первой отправке в работающем цикле asyncio) — с повторами, паузой после сбоя
(«автомат», breaker) и учётом TelegramRetryAfter. Поэтому недоступный Telegram никогда
не задерживает Runner дольше document_wait_s (только send_document ждёт результат).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from engine.db import Db
from engine.models import Proposal
from engine.notifier import LogNotifier, format_proposal, save_backup_file
from engine.report import split_message
from engine.telegram.api import Button, TelegramApi

log = logging.getLogger(__name__)

T = TypeVar("T")

_PREFIX = {"info": "", "warn": "⚠️ ", "alert": "🚨 "}

QUEUE_MAX = 100            # сообщений в очереди, дальше — выбрасываем (см. _drop_one)
BREAKER_S = 60.0           # пауза доставки после исчерпания повторов
RETRY_AFTER_CAP_S = 30.0   # дольше на TelegramRetryAfter не ждём
MESSAGE_TIMEOUT_S = 15.0   # предел на один вызов send_message (сессия aiogram — 10 с)
DOCUMENT_TIMEOUT_S = 130.0  # предел на один send_document (aiogram — request_timeout 120 с)
DOCUMENT_WAIT_S = 2.0      # сколько send_document ждёт результата, прежде чем вернуть False

_TOKEN_RE = re.compile(r"bot\d+:[\w-]+")


def redact(text: str) -> str:
    """Убирает токен бота (он бывает в URL внутри текста ошибки)."""
    return _TOKEN_RE.sub("bot<токен скрыт>", text)


@dataclass(eq=False)
class _Item:
    kind: str                 # "message" | "proposal" | "document"
    level: str
    text: str = ""            # для document — подпись
    buttons: list[list[Button]] | None = None
    key: tuple[str, str] | None = None  # ключ подавления повторов (уровень, текст)
    ts: float = 0.0           # self.clock() в момент постановки в очередь
    proposal_id: int | None = None
    filename: str = ""
    data: bytes = b""
    future: asyncio.Future | None = None  # результат send_document


class TelegramNotifier:
    """Журнал пишется всегда; в Telegram — с префиксом уровня. Исключения наружу не выходят."""

    def __init__(self, api: TelegramApi, chat_id: int, db: Db, dedupe_s: int,
                 clock: Callable[[], float] = time.time, retries: int = 3,
                 retry_delay_s: float = 1.0, backup_dir: str | Path = "data/backups",
                 queue_max: int = QUEUE_MAX, breaker_s: float = BREAKER_S,
                 document_wait_s: float = DOCUMENT_WAIT_S,
                 message_timeout_s: float = MESSAGE_TIMEOUT_S,
                 document_timeout_s: float = DOCUMENT_TIMEOUT_S):
        self.api = api
        self.chat_id = chat_id
        self.db = db
        self.dedupe_s = dedupe_s
        self.clock = clock
        self.retries = max(retries, 1)
        self.retry_delay_s = retry_delay_s
        self.queue_max = max(queue_max, 1)
        self.breaker_s = breaker_s
        self.document_wait_s = document_wait_s
        self.message_timeout_s = message_timeout_s
        self.document_timeout_s = document_timeout_s
        self._log = LogNotifier(db, backup_dir)
        self.backup_dir = Path(backup_dir)
        self._last_sent: dict[tuple[str, str], float] = {}
        self._queued_keys: set[tuple[str, str]] = set()
        self._items: deque[_Item] = deque()
        self._current: _Item | None = None
        self._documents: dict[str, _Item] = {}  # имя файла -> отправка в очереди или с итогом
        self._worker: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._paused_until = 0.0  # time.monotonic(), до которого доставка на паузе
        self._sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep  # подменяется в тестах

    # --- интерфейс Notifier ---
    async def send(self, text: str, level: str = "info",
                   buttons: list[list[Button]] | None = None) -> None:
        if level == "warning":  # модули движка пишут и так
            level = "warn"
        await self._log.send(text, level)
        key = (level, text)
        last = self._last_sent.get(key)
        if key in self._queued_keys or (last is not None and self.clock() - last < self.dedupe_s):
            return
        body = _PREFIX.get(level, "") + text
        if not split_message(body):
            return  # пустой текст в Telegram не отправить
        self._enqueue(_Item("message", level, body, buttons, key, self.clock()))

    async def proposal(self, p: Proposal, ttl_min: int, hold_days: float) -> None:
        text = format_proposal(p, ttl_min, hold_days)
        log.info("%s", text)
        self.db.audit("notify", {"text": text, "level": "info"})
        buttons = [[Button("Открыть", f"approve:{p.id}"), Button("Пропустить", f"reject:{p.id}")]]
        self._enqueue(_Item("proposal", "info", text, buttons, ts=self.clock(), proposal_id=p.id))

    async def send_document(self, filename: str, data: bytes, caption: str) -> bool:
        """True — файл доставлен в Telegram. False — ещё не доставлен: либо отправка идёт
        (повторный вызов с тем же именем файла узнает её итог, а не пошлёт файл второй раз),
        либо не удалась — тогда файл сохранён локально в backup_dir.

        Ждёт не дольше document_wait_s, чтобы не задерживать Runner.
        """
        loop = asyncio.get_running_loop()
        item = self._documents.get(filename)
        if item is not None and item.future is not None and item.future.get_loop() is not loop:
            item = None  # отправка из прошлого цикла asyncio — её итога уже не узнать
        if item is not None and item.future.done() and not item.future.result():
            item = None  # прошлая попытка не удалась — отправляем заново
        if item is None:
            item = _Item("document", "info", caption, ts=self.clock(), filename=filename,
                         data=data, future=loop.create_future())
            self._documents[filename] = item
            self._enqueue(item)
        done, _ = await asyncio.wait({item.future}, timeout=self.document_wait_s)
        if not done:
            log.info("Файл %s ещё отправляется в Telegram", filename)
            return False
        if self._documents.get(filename) is item:
            del self._documents[filename]
        return bool(item.future.result())

    # --- управление очередью ---
    async def flush(self, timeout: float | None = None) -> bool:
        """Дождаться, пока очередь опустеет. False — не успели за timeout."""
        async def wait_empty() -> None:
            while self._items or self._current is not None:
                if self._items:
                    self._ensure_worker()
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(wait_empty(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def aclose(self, timeout: float = 5.0) -> None:
        """Остановка: даём очереди до timeout секунд, остальное — в журнал; закрываем сессию."""
        if self._items or self._current is not None:
            await self.flush(timeout)
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        while self._items:
            self._discard(self._items.popleft(), "остановка бота")
        close = getattr(self.api, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось закрыть сессию Telegram: %s", redact(repr(exc)))

    def _enqueue(self, item: _Item) -> None:
        while len(self._items) >= self.queue_max:
            self._drop_one()
        self._items.append(item)
        if item.key is not None:
            self._queued_keys.add(item.key)
        try:
            self._ensure_worker()
        except RuntimeError:  # нет работающего цикла — разберём при следующей отправке
            pass

    def _drop_one(self) -> None:
        """Переполнение: выбрасываем самое старое info-сообщение; тревоги (warn/alert) и файлы —
        только если info в очереди не осталось (тогда — самое старое из всех)."""
        victim = next((it for it in self._items if it.level == "info" and it.kind != "document"),
                      None)
        if victim is None:
            victim = self._items[0]
            log.error("Очередь Telegram переполнена, выбрасываю самое старое: %s",
                      victim.filename or victim.text[:200])
        else:
            log.warning("Очередь Telegram переполнена, выбрасываю: %s", victim.text[:200])
        self._items.remove(victim)
        self._discard(victim, "очередь переполнена")

    def _discard(self, item: _Item, why: str) -> None:
        """Сообщение не будет доставлено: снять отметку очереди, файл сохранить локально."""
        if item.key is not None:
            self._queued_keys.discard(item.key)
        if item.kind == "document":
            self._document_failed(item)
        elif why == "остановка бота":
            log.warning("Не доставлено в Telegram (%s): %s", why, item.text[:200])

    def _ensure_worker(self) -> None:
        loop = asyncio.get_running_loop()
        if self._worker is not None and not self._worker.done() and self._worker.get_loop() is loop:
            if self._wake is not None:
                self._wake.set()
            return
        self._wake = asyncio.Event()
        self._wake.set()
        self._worker = loop.create_task(self._work(self._wake), name="telegram-notifier")

    async def _work(self, wake: asyncio.Event) -> None:
        while True:
            if not self._items:
                wake.clear()
                await wake.wait()
                continue
            pause = self._paused_until - time.monotonic()
            if pause > 0:
                await asyncio.sleep(pause)
                continue
            item = self._items.popleft()
            self._current = item
            try:
                ok = await self._deliver(item)
            except asyncio.CancelledError:
                if item.kind == "document":
                    self._document_failed(item)
                raise
            except Exception:  # noqa: BLE001 — разборщик очереди не должен умирать
                log.exception("Telegram: ошибка доставки")
                ok = False
            finally:
                self._current = None
                if item.key is not None:
                    self._queued_keys.discard(item.key)
            if not ok and self.breaker_s > 0:
                self._paused_until = time.monotonic() + self.breaker_s
                log.warning("Telegram недоступен: доставка приостановлена на %g с, "
                            "в очереди %d сообщ.", self.breaker_s, len(self._items))

    # --- доставка ---
    async def _deliver(self, item: _Item) -> bool:
        if item.kind == "document":
            async def call() -> bool:
                await self.api.send_document(self.chat_id, item.filename, item.data, item.text)
                return True

            if await self._retry("send_document", call, self.document_timeout_s):
                if item.future is not None and not item.future.done():
                    item.future.set_result(True)
                return True
            self._document_failed(item)
            return False

        what = "proposal" if item.kind == "proposal" else "send_message"
        parts = split_message(item.text)
        message_id = None
        for i, part in enumerate(parts):
            kb = item.buttons if i == len(parts) - 1 else None  # кнопки — под последней частью
            message_id = await self._retry(
                what, lambda part=part, kb=kb: self.api.send_message(self.chat_id, part, kb),
                self.message_timeout_s)
            if message_id is None:
                return False  # не доставлено — и в подавление повторов не попадает
        if item.key is not None:
            self._last_sent[item.key] = item.ts
        if item.proposal_id is not None and message_id is not None:
            self.db.set_kv(f"tg_msg:proposal:{item.proposal_id}", message_id)
        return True

    def _document_failed(self, item: _Item) -> None:
        if item.future is None or item.future.done():
            return
        try:
            path = save_backup_file(self.backup_dir, item.filename, item.data)
            log.warning("Файл %s не ушёл в Telegram, сохранён локально: %s", item.filename, path)
        except OSError:
            log.exception("Не удалось сохранить файл %s локально", item.filename)
        if not item.future.get_loop().is_closed():
            item.future.set_result(False)

    async def _retry(self, what: str, call: Callable[[], Awaitable[T]],
                     timeout: float) -> T | None:
        """Вызов с повторами; после исчерпания — запись в журнал и audit и None, без исключения."""
        error: Exception | None = None
        for attempt in range(self.retries):
            try:
                return await asyncio.wait_for(call(), timeout)
            except Exception as exc:  # noqa: BLE001 — сеть Telegram не должна ронять движок
                error = exc
            if attempt < self.retries - 1:
                retry_after = getattr(error, "retry_after", None)  # TelegramRetryAfter
                if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
                    await self._sleep(min(float(retry_after), RETRY_AFTER_CAP_S))
                else:
                    await self._sleep(self.retry_delay_s)
        text = redact(repr(error))
        log.error("Telegram недоступен (%s), %d попыток: %s", what, self.retries, text)
        try:
            self.db.audit("telegram_error", {"what": what, "attempts": self.retries,
                                             "error": text})
        except Exception:  # noqa: BLE001
            log.exception("Не удалось записать telegram_error")
        return None
