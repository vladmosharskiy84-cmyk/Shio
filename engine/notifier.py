"""Уведомления: протокол и реализация «в журнал» (Telegram — на следующих этапах)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, Protocol

from engine.db import Db
from engine.models import Proposal

log = logging.getLogger(__name__)

Level = Literal["info", "warn", "alert"]

# модули движка исторически шлют и "warning" — понимаем оба написания
_LOG_LEVELS = {"info": logging.INFO, "warn": logging.WARNING, "warning": logging.WARNING,
               "alert": logging.ERROR}


class Notifier(Protocol):
    async def send(self, text: str, level: Level = "info") -> None: ...

    async def proposal(self, p: Proposal, ttl_min: int, hold_days: float) -> None: ...

    async def send_document(self, filename: str, data: bytes, caption: str) -> None: ...


def format_proposal(p: Proposal, ttl_min: int, hold_days: float) -> str:
    """Текст предложения связки (без подсказки, как подтвердить)."""
    legs = ", ".join(f"{l['side']} {l['kind']} на {l['exchange']}" for l in p.legs_plan)
    profile = "агрессивный" if p.profile == "aggressive" else "спокойный"
    return (f"Предложение #{p.id}: связка {p.kind} {p.base} ({legs}), профиль {profile}, "
            f"капитал {p.capital:.2f} USD, ожидаемая доходность {p.expected_apr:.1f}% годовых "
            f"(~{p.expected_profit:.2f} USD за {hold_days:g} дн.). Действует {ttl_min} мин.")


class LogNotifier:
    """Пишет уведомление в журнал Python и в audit_log базы (событие «notify»)."""

    def __init__(self, db: Db):
        self.db = db

    async def send(self, text: str, level: str = "info") -> None:
        log.log(_LOG_LEVELS.get(level, logging.INFO), text)
        self.db.audit("notify", {"text": text, "level": level})

    async def proposal(self, p: Proposal, ttl_min: int, hold_days: float) -> None:
        await self.send(f"{format_proposal(p, ttl_min, hold_days)} "
                        f"Подтвердить: python -m engine approve {p.id}", "info")

    async def send_document(self, filename: str, data: bytes, caption: str) -> None:
        """Без Telegram файл сохраняется в data/backups/, путь пишется в журнал."""
        folder = Path("data") / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / Path(filename).name
        path.write_bytes(data)
        await self.send(f"{caption}. Файл сохранён: {path}", "info")
