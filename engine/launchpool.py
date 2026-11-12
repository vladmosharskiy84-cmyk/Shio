"""Уведомления о новых Launchpool / Launchpad / Jumpstart / Megadrop на Bybit и OKX."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

KEYWORDS = ("launchpool", "launchpad", "jumpstart", "megadrop")
SEEN_KEY = "lp_seen"
SEEN_LIMIT = 500
BYBIT_URL = "https://api.bybit.com/v5/announcements/index?locale=en-US&type=new_crypto&limit=20"
OKX_URL = "https://www.okx.com/api/v5/support/announcements?annType=announcements-new-listings"
HTTP_TIMEOUT_S = 15


@dataclass
class Announcement:
    exchange: str
    id: str
    title: str
    url: str
    published_ms: int


class AnnouncementSource(Protocol):
    async def fetch(self) -> list[Announcement]: ...


class AiohttpJson:
    """Минимальный http-клиент: одна сессия aiohttp, создаётся при первом запросе."""

    def __init__(self):
        self._session = None

    async def get_json(self, url: str) -> dict:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S))
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _items(x) -> list:
    return x if isinstance(x, list) else []


def _parse(exchange: str, raw, ts_field: str) -> Announcement | None:
    raw = _dict(raw)
    title, url = raw.get("title"), raw.get("url")
    if not isinstance(title, str) or not isinstance(url, str) or not title or not url:
        return None
    try:
        published = int(raw.get(ts_field))
    except (TypeError, ValueError):
        return None
    return Announcement(exchange, url, title, url, published)


class BybitAnnouncements:
    def __init__(self, http):
        self.http = http

    async def fetch(self) -> list[Announcement]:
        data = _dict(await self.http.get_json(BYBIT_URL))
        rows = _items(_dict(data.get("result")).get("list"))
        return [a for a in (_parse("bybit", r, "publishTime") for r in rows) if a]


class OkxAnnouncements:
    def __init__(self, http):
        self.http = http

    async def fetch(self) -> list[Announcement]:
        data = _dict(await self.http.get_json(OKX_URL))
        out = []
        for block in _items(data.get("data")):
            for r in _items(_dict(block).get("details")):
                a = _parse("okx", r, "pTime")
                if a:
                    out.append(a)
        return out


def _matches(title: str) -> bool:
    low = title.lower()
    return any(k in low for k in KEYWORDS)


async def check_announcements(sources, db, notifier, now_ms: int) -> list[Announcement]:
    """Новые подходящие анонсы → уведомление. Первый запуск только запоминает найденное."""
    state = db.get_kv(SEEN_KEY)
    first_run = state is None
    seen: list[str] = list(state.get("ids", [])) if isinstance(state, dict) else []
    seen_set = set(seen)
    new: list[Announcement] = []
    for src in sources:
        try:
            found = await src.fetch()
        except Exception as exc:  # noqa: BLE001
            log.warning("Launchpool: источник %s недоступен: %s", type(src).__name__, exc)
            db.audit("launchpool_error", {"source": type(src).__name__, "error": repr(exc)})
            continue
        for a in found:
            if _matches(a.title) and a.id not in seen_set:
                seen_set.add(a.id)
                seen.append(a.id)
                new.append(a)
    db.set_kv(SEEN_KEY, {"ids": seen[-SEEN_LIMIT:]})
    if first_run:
        return []
    for a in new:
        await notifier.send(f"Новый пул на {a.exchange}: {a.title}\n{a.url}", "info")
    return new
