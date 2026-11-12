"""Сторож зависания: если цикл движка не проходит шаг 5 минут, процесс завершается.

Отдельный поток (не asyncio): срабатывает, даже если цикл событий встал намертво.
Docker (restart: always) после выхода сразу запускает движок заново.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

STALL_LIMIT_S = 300  # столько секунд без завершённого шага — считаем, что движок завис
CHECK_EVERY_S = 10


class StallWatchdog:
    def __init__(self, limit_s: float = STALL_LIMIT_S,
                 clock: Callable[[], float] = time.monotonic,
                 exit_fn: Callable[[int], object] = os._exit,
                 check_every_s: float = CHECK_EVERY_S):
        self.limit_s = limit_s
        self.clock = clock
        self.exit_fn = exit_fn
        self.check_every_s = check_every_s
        self._last = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def feed(self) -> None:
        """Шаг цикла завершён: движок жив."""
        self._last = self.clock()

    def check(self) -> bool:
        """True — лимит превышен, процесс завершён (exit_fn(1))."""
        idle = self.clock() - self._last
        if idle < self.limit_s:
            return False
        log.critical("Движок завис %d с — перезапуск", int(idle))
        for handler in logging.getLogger().handlers:  # os._exit не сбрасывает буферы журнала
            try:
                handler.flush()
            except Exception:  # noqa: BLE001
                pass
        self.exit_fn(1)
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.check_every_s):
            if self.check():
                return

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self.feed()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="shio-stall", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
