"""Уведомления: протокол и реализация «в журнал» (Telegram — на следующих этапах)."""
from __future__ import annotations

import logging
from typing import Literal, Protocol

from engine.db import Db

log = logging.getLogger(__name__)

Level = Literal["info", "warn", "alert"]

# модули движка исторически шлют и "warning" — понимаем оба написания
_LOG_LEVELS = {"info": logging.INFO, "warn": logging.WARNING, "warning": logging.WARNING,
               "alert": logging.ERROR}


class Notifier(Protocol):
    async def send(self, text: str, level: Level = "info") -> None: ...


class LogNotifier:
    """Пишет уведомление в журнал Python и в audit_log базы (событие «notify»)."""

    def __init__(self, db: Db):
        self.db = db

    async def send(self, text: str, level: str = "info") -> None:
        log.log(_LOG_LEVELS.get(level, logging.INFO), text)
        self.db.audit("notify", {"text": text, "level": level})
