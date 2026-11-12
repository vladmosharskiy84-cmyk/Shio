"""Внешний сторож: пинг healthchecks.io (или аналога) из тика heartbeat."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

LOG_EVERY_S = 600  # сбои пинга пишем в журнал не чаще раза в 10 минут


class HealthPinger:
    def __init__(self, url: str | None,
                 http_get: Callable[[str, float], Awaitable[int]] | None = None,
                 timeout_s: float = 10.0):
        self.url = url or None
        self.timeout_s = timeout_s
        self._http_get = http_get or self._default_get
        self._session = None
        self._last_log = float("-inf")

    @classmethod
    def from_env(cls) -> "HealthPinger":
        url = os.environ.get("SHIO_HEALTHCHECK_URL", "").strip()
        return cls(url or None)

    async def _default_get(self, url: str, timeout: float) -> int:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession()
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            return resp.status

    def _host(self) -> str:
        return urlsplit(self.url or "").hostname or "?"

    def _warn(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_log >= LOG_EVERY_S:
            self._last_log = now
            log.warning("Сторож (%s): пинг не удался: %s", self._host(), reason)

    async def ping(self, fail: bool = False) -> bool:
        if not self.url:
            return False
        target = self.url + "/fail" if fail else self.url
        try:
            status = await asyncio.wait_for(self._http_get(target, self.timeout_s),
                                            self.timeout_s)
        except asyncio.TimeoutError:
            self._warn("таймаут")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._warn(type(exc).__name__)  # текст ошибки может содержать URL
            return False
        if 200 <= status < 300:
            return True
        self._warn(f"HTTP {status}")
        return False

    async def aclose(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
