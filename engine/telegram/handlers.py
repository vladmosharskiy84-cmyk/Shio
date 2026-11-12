"""Обработчики команд и кнопок Telegram.

Чистые функции: только пишут строки в таблицу commands (исполняет их Runner)
и возвращают текст ответа. Отвечают исключительно владельцу.
"""
from __future__ import annotations

from dataclasses import dataclass

from engine.report import build_status, format_positions, format_status
from engine.telegram.api import Button

HOUR_MS = 3_600_000
DEDUP_MS = 60_000
ACCEPTED = "Принято, бот выполнит в течение нескольких секунд."
HELP = (
    "Команды:\n"
    "/status - состояние бота и капитала\n"
    "/positions - открытые позиции и запас до ликвидации\n"
    "/pause - не открывать новые связки\n"
    "/resume - снова открывать новые связки\n"
    "/stop - закрыть все позиции (с подтверждением)\n"
    "/close <номер> - закрыть одну позицию (с подтверждением)\n"
    "/help - этот список"
)
HINT = "Не понял. Список команд: /help"
CANCEL = Button("Отмена", "cancel")


@dataclass
class Reply:
    text: str
    buttons: list[list[Button]] | None = None


def _is_owner(chat_id: int, db, settings, now_ms: int) -> bool:
    owner = settings.telegram.chat_id
    if owner is not None and int(chat_id) == owner:
        return True
    key = f"tg_foreign:{chat_id}"
    last = db.get_kv(key)
    if last is None or now_ms - last >= HOUR_MS:
        db.set_kv(key, now_ms)
        db.audit("telegram_foreign", {"chat_id": chat_id})
    return False


def _parse_id(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.isdigit() else None


def _dedup(db, key: str, now_ms: int) -> bool:
    """True, если такая же команда уже ушла меньше минуты назад; иначе помечает её."""
    last = db.get_kv(key)
    if last is not None and 0 <= now_ms - last < DEDUP_MS:
        return True
    db.set_kv(key, now_ms)
    return False


def handle_message(text: str, chat_id: int, db, settings, now_ms: int) -> Reply | None:
    if not _is_owner(chat_id, db, settings, now_ms):
        return None
    parts = (text or "").strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return Reply(HELP)
    if cmd == "/status":
        return Reply(format_status(build_status(db, settings, now_ms), cli_hint=False))
    if cmd == "/positions":
        return Reply(format_positions(db, settings, now_ms))
    if cmd in ("/pause", "/resume"):
        db.add_command(cmd[1:])
        return Reply(ACCEPTED)
    if cmd == "/stop":
        return Reply("Закрыть все позиции?",
                     [[Button("Да, закрыть всё", "stop:confirm"), CANCEL]])
    if cmd == "/close":
        return _ask_close(arg, db)
    return Reply(HINT)


def _ask_close(arg: str, db) -> Reply:
    pid = _parse_id(arg)
    if pid is None:
        return Reply("Укажите номер позиции: /close <номер>. Номера видно в /positions.")
    try:
        pos = db.get_position(pid)
    except KeyError:
        return Reply(f"Позиция {pid} не найдена.")
    if pos.status not in ("open", "closing"):
        return Reply(f"Позиция {pid} уже не открыта.")
    return Reply(f"Закрыть позицию {pid} ({pos.base})?",
                 [[Button("Да, закрыть", f"close:confirm:{pid}"), CANCEL]])


def handle_callback(data: str, chat_id: int, db, settings, now_ms: int) -> Reply | None:
    if not _is_owner(chat_id, db, settings, now_ms):
        return None
    data = data or ""
    if data == "cancel":
        return Reply("Отменено.")
    if data == "stop:confirm":
        if _dedup(db, "tg_done:stop", now_ms):
            return Reply("Уже выполняется.")
        db.add_command("stop_all")
        return Reply(ACCEPTED)
    if data.startswith("close:confirm:"):
        pid = _parse_id(data.removeprefix("close:confirm:"))
        if pid is None:
            return Reply(HINT)
        if _dedup(db, f"tg_done:close:{pid}", now_ms):
            return Reply("Уже выполняется.")
        db.add_command("close", str(pid))
        return Reply(ACCEPTED)
    action, _, raw = data.partition(":")
    if action in ("approve", "reject"):
        return _decide(action, _parse_id(raw), db, now_ms)
    return Reply(HINT)


def _decide(action: str, pid: int | None, db, now_ms: int) -> Reply:
    prop = None if pid is None else db.get_proposal(pid)
    if prop is None:
        return Reply("Предложение не найдено.")
    if prop.expires_at <= now_ms:
        return Reply("Предложение истекло.")
    mark = f"tg_done:proposal:{pid}"
    if prop.status != "pending" or db.get_kv(mark) is not None:
        return Reply("Предложение уже обработано.")
    db.add_command(action, str(pid))
    db.set_kv(mark, now_ms)
    return Reply("Принято: открываю…" if action == "approve" else "Пропущено.")
