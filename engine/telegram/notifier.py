"""Уведомления в Telegram: уровни, очередь доставки, повторы, подавление одинаковых тревог.

Доставка отвязана от главного цикла: send / proposal / send_document сразу пишут журнал и
audit, а сообщение кладут в ограниченную очередь. Её разбирает фоновая задача (запускается
при первой отправке в работающем цикле asyncio) — с повторами, паузой после сбоя
(«автомат», breaker) и учётом TelegramRetryAfter. Поэтому недоступный Telegram никогда
не задерживает Runner дольше document_wait_s (только send_document ждёт результат).

Сбои делятся на временные (сеть, таймаут, 5xx, flood control) и постоянные (Telegram
отверг запрос: 4xx и всё непонятное). После временного сбоя сообщение возвращается в
начало очереди и ждёт конца паузы; после постоянного — выбрасывается с записью в журнал
и audit. Слишком старые сообщения (info — час, тревоги — 12 ч) тоже выбрасываются.
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
_URGENT = ("warn", "alert")

QUEUE_MAX = 100            # сообщений в очереди, дальше — выбрасываем (см. _drop_one)
BREAKER_S = 60.0           # пауза доставки после временного сбоя
RETRY_AFTER_CAP_S = 30.0   # дольше на TelegramRetryAfter не ждём
MESSAGE_TIMEOUT_S = 15.0   # предел на один вызов send_message (сессия aiogram — 10 с)
DOCUMENT_TIMEOUT_S = 130.0  # предел на один send_document (aiogram — request_timeout 120 с)
DOCUMENT_WAIT_S = 2.0      # сколько send_document ждёт результата, прежде чем вернуть False
MAX_AGE_S = {"info": 3600.0, "warn": 12 * 3600.0, "alert": 12 * 3600.0}  # потом — выбросить
DOCUMENT_KEEP_S = 86400.0  # итоги отправленных файлов храним сутки

_TOKEN_RE = re.compile(r"bot\d+:[\w-]+")

# имена классов ошибок aiogram (сам aiogram здесь не импортируется)
_TRANSIENT_NAMES = {"TelegramNetworkError", "TelegramServerError", "TelegramRetryAfter",
                    "RestartingTelegram"}
_PERMANENT_NAMES = {"TelegramEntityTooLarge"}  # наследник TelegramNetworkError, но повтор не поможет


def redact(text: str) -> str:
    """Убирает токен бота (он бывает в URL внутри текста ошибки)."""
    return _TOKEN_RE.sub("bot<токен скрыт>", text)


def is_transient(exc: BaseException) -> bool:
    """Временный сбой (сеть, таймаут, 5xx, flood control) — стоит повторить позже."""
    names = {c.__name__ for c in type(exc).__mro__}
    if names & _PERMANENT_NAMES:
        return False
    if names & _TRANSIENT_NAMES:
        return True
    if isinstance(exc, (TimeoutError, OSError)):  # asyncio.TimeoutError, ConnectionError, ...
        return True
    try:
        import aiohttp
        if isinstance(exc, aiohttp.ClientError):
            return True
    except ImportError:  # pragma: no cover
        pass
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500


@dataclass(eq=False)
class _Item:
    kind: str                 # "message" | "proposal" | "document"
    level: str
    text: str = ""            # для document — подпись
    buttons: list[list[Button]] | None = None
    key: tuple[str, str] | None = None  # ключ подавления повторов (уровень, текст)
    ts: float = 0.0           # self.clock() в момент постановки в очередь
    proposal_id: int | None = None
    expires_ms: int | None = None  # срок предложения
    parts_sent: int = 0       # сколько частей длинного сообщения уже доставлено
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
        self._outage = False      # идёт серия временных сбоев (audit — один раз на серию)
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
        self._enqueue(_Item("proposal", "info", text, buttons, ts=self.clock(), proposal_id=p.id,
                            expires_ms=p.expires_at))

    async def send_document(self, filename: str, data: bytes, caption: str) -> bool:
        """True — файл доставлен в Telegram. False — ещё не доставлен: либо отправка идёт
        (повторный вызов с тем же именем файла узнает её итог, а не пошлёт файл второй раз),
        либо не удалась — тогда файл сохранён локально в backup_dir.

        Одна попытка на вызов (Runner сам повторяет через 20 мин). Ждёт не дольше
        document_wait_s, чтобы не задерживать Runner.
        """
        loop = asyncio.get_running_loop()
        self._prune_documents()
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
        """Переполнение. Порядок: самое старое обычное info-сообщение, затем самое старое
        предложение, затем самое старое из оставшегося (тревоги, файлы)."""
        victim = next((it for it in self._items if it.kind == "message" and it.level == "info"),
                      None)
        if victim is None:
            victim = next((it for it in self._items if it.kind == "proposal"), None)
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

    def _drop(self, item: _Item, why: str, error: str | None = None) -> None:
        """Выбросить сообщение с записью в журнал и audit."""
        log.warning("Не доставлено в Telegram (%s): %s", why, item.filename or item.text[:200])
        detail = {"reason": why, "kind": item.kind, "level": item.level,
                  "text": item.filename or item.text[:200]}
        if error is not None:
            detail["error"] = error
        try:
            self.db.audit("telegram_dropped", detail)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось записать telegram_dropped")
        self._discard(item, why)

    def _prune_documents(self) -> None:
        now = self.clock()
        for name, it in list(self._documents.items()):
            if it.future is not None and it.future.done() and now - it.ts > DOCUMENT_KEEP_S:
                del self._documents[name]

    def _ensure_worker(self) -> None:
        loop = asyncio.get_running_loop()
        if self._worker is not None and not self._worker.done() and self._worker.get_loop() is loop:
            if self._wake is not None:
                self._wake.set()
            return
        self._wake = asyncio.Event()
        self._wake.set()
        self._worker = loop.create_task(self._work(self._wake), name="telegram-notifier")

    def _take_next(self) -> _Item:
        """Следующее сообщение; тревоги не ждут файлов, стоящих перед ними."""
        head = self._items[0]
        if head.kind == "document":
            urgent = next((it for it in self._items
                           if it.kind != "document" and it.level in _URGENT), None)
            if urgent is not None:
                self._items.remove(urgent)
                return urgent
        return self._items.popleft()

    def _stale(self, item: _Item) -> str | None:
        """Причина не отправлять сообщение (устарело / предложение истекло) или None."""
        if item.kind == "document":
            return None
        if item.kind == "proposal" and item.expires_ms is not None \
                and self.clock() * 1000 >= item.expires_ms:
            return "истекло"
        if self.clock() - item.ts > MAX_AGE_S.get(item.level, MAX_AGE_S["info"]):
            return "устарело"
        return None

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
            item = self._take_next()
            stale = self._stale(item)
            if stale == "истекло":
                log.info("Предложение #%s истекло, пока ждало отправки в Telegram — пропускаю",
                         item.proposal_id)
                self._discard(item, stale)
                continue
            if stale is not None:
                self._drop(item, stale)
                continue
            self._current = item
            requeue = False
            try:
                outcome = await self._deliver(item)
            except asyncio.CancelledError:
                if item.kind == "document":
                    self._document_failed(item)
                else:
                    log.warning("Не доставлено в Telegram (остановка): %s", item.text[:200])
                raise
            except Exception:  # noqa: BLE001 — разборщик очереди не должен умирать
                log.exception("Telegram: ошибка доставки")
                outcome = "permanent"
            finally:
                self._current = None
            if outcome == "transient" and item.kind != "document":
                requeue = True
                self._items.appendleft(item)  # не теряем: ждёт конца паузы первым
            if not requeue and item.key is not None:
                self._queued_keys.discard(item.key)
            if outcome == "ok":
                self._outage = False
            elif outcome == "transient" and self.breaker_s > 0:
                self._paused_until = time.monotonic() + self.breaker_s
                log.warning("Telegram недоступен: доставка приостановлена на %g с, "
                            "в очереди %d сообщ.", self.breaker_s, len(self._items))

    # --- доставка ---
    async def _deliver(self, item: _Item) -> str:
        """"ok", "transient" (повторить позже) или "permanent" (выброшено)."""
        if item.kind == "document":
            async def call() -> bool:
                await self.api.send_document(self.chat_id, item.filename, item.data, item.text)
                return True

            _, error = await self._retry("send_document", call, self.document_timeout_s, 1)
            if error is None:
                if item.future is not None and not item.future.done():
                    item.future.set_result(True)
                item.data = b""
                return "ok"
            self._document_failed(item)
            return "transient" if is_transient(error) else "permanent"

        what = "proposal" if item.kind == "proposal" else "send_message"
        parts = split_message(item.text)
        message_id = None
        while item.parts_sent < len(parts):
            i = item.parts_sent
            kb = item.buttons if i == len(parts) - 1 else None  # кнопки — под последней частью
            message_id, error = await self._retry(
                what, lambda part=parts[i], kb=kb: self.api.send_message(self.chat_id, part, kb),
                self.message_timeout_s, self.retries)
            if error is not None:  # не доставлено — и в подавление повторов не попадает
                if is_transient(error):
                    return "transient"
                self._drop(item, "Telegram отказал", redact(repr(error)))
                return "permanent"
            item.parts_sent += 1
        if item.key is not None:
            self._last_sent[item.key] = item.ts
        if item.proposal_id is not None and message_id is not None:
            self.db.set_kv(f"tg_msg:proposal:{item.proposal_id}", message_id)
        return "ok"

    def _document_failed(self, item: _Item) -> None:
        if item.future is None or item.future.done():
            return
        try:
            path = save_backup_file(self.backup_dir, item.filename, item.data)
            log.warning("Файл %s не ушёл в Telegram, сохранён локально: %s", item.filename, path)
        except OSError:
            log.exception("Не удалось сохранить файл %s локально", item.filename)
        item.data = b""
        if not item.future.get_loop().is_closed():
            item.future.set_result(False)

    async def _retry(self, what: str, call: Callable[[], Awaitable[T]], timeout: float,
                     attempts: int) -> tuple[T | None, Exception | None]:
        """Вызов с повторами (постоянную ошибку не повторяем). Возвращает (результат, ошибка)."""
        error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await asyncio.wait_for(call(), timeout), None
            except Exception as exc:  # noqa: BLE001 — сеть Telegram не должна ронять движок
                error = exc
            if not is_transient(error):
                break
            if attempt < attempts - 1:
                retry_after = getattr(error, "retry_after", None)  # TelegramRetryAfter
                if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
                    await self._sleep(min(float(retry_after), RETRY_AFTER_CAP_S))
                else:
                    await self._sleep(self.retry_delay_s)
        text = redact(repr(error))
        transient = is_transient(error)
        log.error("Telegram %s (%s), попыток %d: %s",
                  "недоступен" if transient else "отказал", what, attempt + 1, text)
        if not transient or not self._outage:  # на серию временных сбоев — одна запись
            try:
                self.db.audit("telegram_error", {"what": what, "attempts": attempt + 1,
                                                 "error": text, "transient": transient})
            except Exception:  # noqa: BLE001
                log.exception("Не удалось записать telegram_error")
        if transient:
            self._outage = True
        return None, error
