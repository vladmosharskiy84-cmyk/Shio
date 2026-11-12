"""Загрузка публичных данных с бирж через ccxt (async) и приведение к общему виду."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import ccxt.async_support as ccxt

from .calc import PerpInfo, SpotInfo, history_stats, parse_interval_hours

log = logging.getLogger(__name__)


def _usdt_linear_perp(m: dict) -> bool:
    return bool(m.get("swap") and m.get("linear") and m.get("quote") == "USDT"
                and m.get("settle") == "USDT" and m.get("active") is not False)


def _usdt_spot(m: dict) -> bool:
    return bool(m.get("spot") and m.get("quote") == "USDT" and m.get("active") is not False)


def ticker_volume_usd(t: Optional[dict]) -> Optional[float]:
    if not t:
        return None
    qv = t.get("quoteVolume")
    if qv:
        return float(qv)
    bv, last = t.get("baseVolume"), t.get("last") or t.get("close")
    if bv and last:
        return float(bv) * float(last)
    return None


def normalize_perps(exchange: str, markets: dict, funding: dict, tickers: dict,
                    intervals: Optional[dict] = None) -> list[PerpInfo]:
    """markets/funding/tickers — словари в формате ccxt, ключ = символ."""
    intervals = intervals or {}
    out = []
    for sym, fr in funding.items():
        m = markets.get(sym)
        if not m or not _usdt_linear_perp(m):
            continue
        rate = fr.get("fundingRate")
        if rate is None:
            continue
        interval = fr.get("interval") or (intervals.get(sym) or {}).get("interval")
        t = tickers.get(sym)
        price = fr.get("markPrice") or (t or {}).get("last") or (t or {}).get("close")
        out.append(PerpInfo(
            exchange=exchange, base=m["base"], symbol=sym,
            funding_rate=float(rate), interval_h=parse_interval_hours(interval),
            mark_price=float(price) if price else None,
            volume_usd=ticker_volume_usd(t),
            next_funding_ts=fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp"),
        ))
    return out


def normalize_spots(exchange: str, markets: dict, tickers: dict) -> list[SpotInfo]:
    out = []
    for sym, t in tickers.items():
        m = markets.get(sym)
        if not m or not _usdt_spot(m):
            continue
        price = t.get("last") or t.get("close")
        out.append(SpotInfo(exchange, m["base"], sym, float(price) if price else None, ticker_volume_usd(t)))
    return out


class ExchangeClient:
    def __init__(self, name: str):
        self.name = name
        self.ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 20000})

    async def close(self):
        await self.ex.close()

    async def _safe(self, coro, what: str, default):
        try:
            return await coro
        except Exception as e:  # сеть, гео-блок, изменения API — не валим весь скан
            log.warning("%s: не удалось получить %s: %s", self.name, what, str(e)[:200])
            return default

    async def snapshot(self) -> tuple[list[PerpInfo], list[SpotInfo]]:
        markets = await self._safe(self.ex.load_markets(), "рынки", None)
        if not markets:
            return [], []
        funding, swap_t, spot_t = await asyncio.gather(
            self._safe(self.ex.fetch_funding_rates(), "ставки funding", {}),
            self._safe(self.ex.fetch_tickers(None, {"type": "swap"}), "тикеры фьючерсов", {}),
            self._safe(self.ex.fetch_tickers(None, {"type": "spot"}), "тикеры спота", {}),
        )
        intervals = {}
        if self.ex.has.get("fetchFundingIntervals") is True:
            intervals = await self._safe(self.ex.fetch_funding_intervals(), "интервалы funding", {})
        perps = normalize_perps(self.name, markets, funding or {}, swap_t or {}, intervals or {})
        spots = normalize_spots(self.name, markets, spot_t or {})
        return perps, spots

    async def enrich_history(self, perp: PerpInfo, periods: int) -> None:
        hist = await self._safe(
            self.ex.fetch_funding_rate_history(perp.symbol, None, periods),
            f"историю {perp.symbol}", [])
        rates = [h.get("fundingRate") for h in (hist or [])[-periods:]]
        perp.hist_avg_rate_per_h, perp.hist_positive_share = history_stats(rates, perp.interval_h)

    async def order_book(self, symbol: str, limit: int = 20) -> dict:
        return await self.ex.fetch_order_book(symbol, limit)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Округлить количество по правилам биржи (рынки должны быть загружены)."""
        return float(self.ex.amount_to_precision(symbol, amount))
