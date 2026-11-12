"""Сбор рыночных данных со всех наблюдаемых бирж."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from scanner.calc import Fees, PerpInfo, SpotInfo
from scanner.engine import find_opportunities
from scanner.exchanges import ExchangeClient

from .config import Settings

log = logging.getLogger(__name__)

_warned_no_funding: set[str] = set()  # по одному предупреждению на биржу за процесс


@dataclass
class MarketSnapshot:
    ts: int
    perps: list[PerpInfo] = field(default_factory=list)
    spots: list[SpotInfo] = field(default_factory=list)
    ok: dict[str, bool] = field(default_factory=dict)

    def perp(self, exchange: str, symbol: str) -> PerpInfo | None:
        for p in self.perps:
            if p.exchange == exchange and p.symbol == symbol:
                return p
        return None


class Collector:
    def __init__(self, settings: Settings, client_factory=ExchangeClient):
        self.settings = settings
        self._factory = client_factory
        self._clients: dict[str, ExchangeClient] = {}

    def _client(self, name: str):
        if name not in self._clients:
            self._clients[name] = self._factory(name)
        return self._clients[name]

    def _scanner_cfg(self) -> dict:
        s = self.settings
        return {"capital_usd": s.capital_usd, "perp_leverage": s.limits.max_leverage,
                "hold_days": s.hold_days, "min_perp_volume_usd": s.min_perp_volume_usd,
                "min_spot_volume_usd": s.min_spot_volume_usd,
                "max_price_diff_pct": s.max_price_diff_pct}

    async def _snapshot_one(self, name: str):
        client = self._client(name)
        if not client.ex.has.get("fetchFundingRates"):
            if name not in _warned_no_funding:
                _warned_no_funding.add(name)
                log.warning("%s: биржа не отдаёт ставки funding, пропускаю", name)
            return None
        perps, spots = await client.snapshot()
        if not perps and not spots:
            return None
        return perps, spots

    async def collect(self) -> MarketSnapshot:
        s = self.settings
        names = [n for n, e in s.exchanges.items() if e.watch]
        results = await asyncio.gather(*(self._snapshot_one(n) for n in names),
                                       return_exceptions=True)
        snap = MarketSnapshot(ts=int(time.time() * 1000))
        for name, res in zip(names, results):
            if isinstance(res, BaseException) or res is None:
                if isinstance(res, BaseException):
                    log.warning("%s: сбор данных не удался: %s", name, str(res)[:200])
                snap.ok[name] = False
                continue
            snap.ok[name] = True
            snap.perps += res[0]
            snap.spots += res[1]
        await self._enrich_history(snap)
        return snap

    async def _enrich_history(self, snap: MarketSnapshot) -> None:
        s = self.settings
        fees = {n: Fees(e.spot_taker, e.perp_taker) for n, e in s.exchanges.items()}
        spot_perp, perp_perp = find_opportunities(snap.perps, snap.spots, fees, self._scanner_cfg())
        top = s.history_check_top
        need: set[tuple[str, str]] = set()
        for op in spot_perp[:top]:
            need.add((op.extra["exchange"], f"{op.base}/USDT:USDT"))
        for op in perp_perp[:top]:
            need.add((op.extra["short"], f"{op.base}/USDT:USDT"))
            need.add((op.extra["long"], f"{op.base}/USDT:USDT"))
        jobs = []
        for ex, sym in need:
            perp = snap.perp(ex, sym)
            if perp is not None:
                jobs.append(self._client(ex).enrich_history(perp, s.history_periods))
        await asyncio.gather(*jobs, return_exceptions=True)

    async def order_book(self, exchange: str, symbol: str) -> dict:
        return await self._client(exchange).order_book(symbol)

    async def round_amount(self, exchange: str, symbol: str, amount: float) -> float:
        client = self._client(exchange)
        if not client.ex.markets:
            await client.ex.load_markets()
        return client.amount_to_precision(symbol, amount)

    async def close(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)
