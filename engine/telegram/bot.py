"""Привязка к aiogram 3: единственное место, где импортируется aiogram."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from engine.notifier import LogNotifier, Notifier
from engine.telegram.api import Button, TelegramApi
from engine.telegram.handlers import handle_callback, handle_message
from engine.telegram.notifier import TelegramNotifier

log = logging.getLogger(__name__)

TOKEN_ENV = "SHIO_TELEGRAM_TOKEN"
CALLBACK_TOAST = "Принято"


class AiogramApi:
    """Реализация TelegramApi поверх aiogram.Bot."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_message(self, chat_id: int, text: str,
                           buttons: list[list[Button]] | None = None) -> int:
        markup = None
        if buttons:
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row]
                for row in buttons])
        msg = await self.bot.send_message(chat_id, text, reply_markup=markup)
        return msg.message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        # без reply_markup: кнопки у сообщения пропадают
        await self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.bot.answer_callback_query(callback_id, text=text or None)

    async def send_document(self, chat_id: int, filename: str, data: bytes, caption: str) -> None:
        await self.bot.send_document(chat_id, BufferedInputFile(data, filename=filename),
                                     caption=caption)


def build_notifier(settings, db) -> tuple[Notifier, str | None]:
    """TelegramNotifier, если Telegram включён и есть токен; иначе LogNotifier и предупреждение."""
    cfg = settings.telegram
    backups = Path(settings.db_path).parent / "backups"
    if not cfg.enabled:
        log.info("Telegram выключен (telegram.enabled: false), уведомления идут в журнал.")
        return LogNotifier(db, backups), None
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        log.warning("Telegram не настроен: не задана переменная окружения %s. "
                    "Двигатель работает с уведомлениями в журнал.", TOKEN_ENV)
        return LogNotifier(db, backups), None
    try:
        api = AiogramApi(Bot(token))
    except Exception as exc:  # noqa: BLE001 — например, TokenValidationError
        log.warning("Telegram не настроен: токен в %s не подошёл (%s). "
                    "Двигатель работает с уведомлениями в журнал.", TOKEN_ENV, exc)
        return LogNotifier(db, backups), None
    return TelegramNotifier(api, cfg.chat_id, db, cfg.dedupe_s, backup_dir=backups), token


# --- маршрутизация (чистые функции, тестируются без aiogram и сети) ---
async def route_message(api: TelegramApi, text: str, chat_id: int, user_id: int | None, db,
                        settings, now_ms: int) -> None:
    reply = handle_message(text, chat_id, user_id, db, settings, now_ms)
    if reply is not None:
        await api.send_message(chat_id, reply.text, reply.buttons)


async def route_callback(api: TelegramApi, callback_id: str, data: str, chat_id: int,
                         user_id: int | None, message_id: int | None, message_text: str | None,
                         db, settings, now_ms: int) -> None:
    reply = handle_callback(data, chat_id, user_id, db, settings, now_ms)
    # Колбэк закрываем всегда, чтобы у кнопки не крутились «часики»; чужому — без текста.
    # Старый запрос Telegram отклоняет («query is too old») — это не повод терять ответ.
    try:
        await api.answer_callback(callback_id, CALLBACK_TOAST if reply is not None else "")
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось ответить на нажатие кнопки: %r", exc)
    if reply is None:
        return
    if reply.buttons or message_id is None:
        await api.send_message(chat_id, reply.text, reply.buttons)
        return
    combined = f"{message_text}\n\n{reply.text}" if message_text else reply.text
    try:
        await api.edit_message(chat_id, message_id, combined)  # кнопки убираются
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось отредактировать сообщение с кнопками: %r", exc)
        await api.send_message(chat_id, reply.text)


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


_UPDATE_KEYS = ("message", "edited_message", "channel_post", "callback_query",
                "my_chat_member", "chat_member")


def chat_id_from_update(update: Any) -> int | None:
    """chat_id из Update, Message или CallbackQuery (объект aiogram или dict); иначе None."""
    chat = _get(update, "chat")
    if chat is not None and _get(chat, "id") is not None:
        return int(_get(chat, "id"))
    for key in _UPDATE_KEYS:
        inner = _get(update, key)
        if inner is not None:
            found = chat_id_from_update(inner)
            if found is not None:
                return found
    # CallbackQuery без сообщения (например, слишком старого): пишем id пользователя
    user = _get(update, "from_user")
    if user is not None and _get(user, "id") is not None and _get(update, "message") is None:
        return int(_get(user, "id"))
    return None


@dataclass
class Sender:
    """Кто написал боту (для python -m engine telegram-id)."""
    chat_id: int
    user_id: int | None
    name: str | None
    username: str | None
    chat_type: str | None


def _event_part(update: Any, name: str) -> Any:
    """Поле name у события или у вложенного Message/CallbackQuery из Update."""
    found = _get(update, name)
    if found is not None:
        return found
    for key in _UPDATE_KEYS:
        inner = _get(update, key)
        if inner is not None:
            found = _event_part(inner, name)
            if found is not None:
                return found
    return None


def sender_from_update(update: Any) -> Sender | None:
    chat_id = chat_id_from_update(update)
    if chat_id is None:
        return None
    user = _event_part(update, "from_user")
    names = [n for n in (_get(user, "first_name"), _get(user, "last_name")) if n]
    chat = _event_part(update, "chat")
    return Sender(chat_id, _get(user, "id"), " ".join(names) or None, _get(user, "username"),
                  _get(chat, "type"))


# --- запуск ---
def _now_ms() -> int:
    return int(time.time() * 1000)


def _sender_id(event) -> int | None:
    user = getattr(event, "from_user", None)
    return getattr(user, "id", None)


def _build_dispatcher(api: TelegramApi, settings, db) -> Dispatcher:
    dp = Dispatcher()

    @dp.message()
    async def _on_message(message) -> None:
        if message.text is None:
            return
        await route_message(api, message.text, message.chat.id, _sender_id(message), db,
                            settings, _now_ms())

    @dp.callback_query()
    async def _on_callback(query) -> None:
        msg = query.message
        user_id = _sender_id(query)
        chat_id = msg.chat.id if msg is not None else user_id
        await route_callback(api, query.id, query.data or "", chat_id, user_id,
                             getattr(msg, "message_id", None), getattr(msg, "text", None),
                             db, settings, _now_ms())

    return dp


async def _poll_until(bot: Bot, dp: Dispatcher, stop: asyncio.Event) -> None:
    """Опрашивает Telegram, пока не сработает stop. aiogram сам повторяет при сетевых ошибках."""
    polling = asyncio.ensure_future(dp.start_polling(bot, handle_signals=False,
                                                     close_bot_session=False))
    waiter = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if polling.done():
            polling.result()  # если опрос упал — исключение уйдёт наверх, задачу перезапустят
            return
        try:
            await dp.stop_polling()
        except RuntimeError:  # опрос ещё не начался (Ctrl+C на старте)
            polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)
    finally:
        for task in (polling, waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(polling, waiter, return_exceptions=True)
        await bot.session.close()


async def run_telegram(token: str, settings, db, notifier: TelegramNotifier,
                       stop: asyncio.Event) -> None:
    """Принимает команды и нажатия кнопок владельца до сигнала stop."""
    bot = Bot(token)
    dp = _build_dispatcher(AiogramApi(bot), settings, db)
    await _poll_until(bot, dp, stop)


def _build_discovery_dispatcher(found: list[Sender], stop: asyncio.Event) -> Dispatcher:
    """Диспетчер, который только запоминает первого написавшего и ничего не отвечает."""
    dp = Dispatcher()

    async def _remember(event) -> None:
        sender = sender_from_update(event)
        if sender is not None and not found:
            found.append(sender)
            stop.set()

    dp.message.register(_remember)
    dp.callback_query.register(_remember)
    return dp


async def discover_chat_id(token: str, timeout_s: float = 120.0) -> Sender | None:
    """Первый, кто напишет боту (chat_id, имя, username). В Telegram ничего не отвечает."""
    bot = Bot(token)
    stop = asyncio.Event()
    found: list[Sender] = []
    dp = _build_discovery_dispatcher(found, stop)

    async def _timeout() -> None:
        await asyncio.sleep(timeout_s)
        stop.set()

    timer = None
    try:
        # старые сообщения (в том числе чужие) не в счёт
        await bot.delete_webhook(drop_pending_updates=True)
        timer = asyncio.ensure_future(_timeout())
        await _poll_until(bot, dp, stop)
    finally:
        if timer is not None:
            timer.cancel()
        await bot.session.close()
    return found[0] if found else None
