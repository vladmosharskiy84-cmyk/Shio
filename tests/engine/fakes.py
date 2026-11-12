"""Общие тестовые заглушки движка."""
from __future__ import annotations

from scanner.exchanges import ExchangeClient


def mkt(base, swap=True):
    if swap:
        return {"base": base, "quote": "USDT", "settle": "USDT", "swap": True, "linear": True, "active": True}
    return {"base": base, "quote": "USDT", "spot": True, "active": True}


class FakeExchange:
    """Имитирует ccxt.async_support биржу."""
    DATA = {
        # rate за 8ч; HOT — высокая ставка, SPIKE — всплеск (история низкая)
        "binance": {"HOT": (0.0010, "8h", 100.0), "SPIKE": (0.0030, "8h", 5.0), "BTC": (0.0001, "8h", 60000.0)},
        "bybit":   {"HOT": (0.0002, "8h", 100.1), "SPIKE": (0.0000, "8h", 5.0), "BTC": (0.0001, "8h", 60010.0)},
        "okx":     {"HOT": (-0.0003, "8h", 99.9), "BTC": (0.0001, "8h", 60005.0)},
        "whitebit": {"BTC": (0.0001, "8h", 60000.0)},
    }
    HIST = {"SPIKE": 0.00001}
    has = {"fetchFundingIntervals": False, "fetchFundingRates": True}
    markets = {}
    BOOK_DEPTH_USD = 1e7  # глубина каждой стороны стакана
    BOOKS: dict = {}      # (name, symbol) -> {"asks": ..., "bids": ...} — переопределение

    def __init__(self, name):
        self.name = name
        self.markets = {}

    async def load_markets(self):
        out = {}
        for b in self.DATA[self.name]:
            out[f"{b}/USDT:USDT"] = mkt(b)
            out[f"{b}/USDT"] = mkt(b, swap=False)
        return out

    async def fetch_funding_rates(self):
        return {f"{b}/USDT:USDT": {"fundingRate": r, "interval": i, "markPrice": p}
                for b, (r, i, p) in self.DATA[self.name].items()}

    async def fetch_tickers(self, symbols, params):
        suffix = ":USDT" if params["type"] == "swap" else ""
        return {f"{b}/USDT{suffix}": {"last": p, "quoteVolume": 5e7}
                for b, (r, i, p) in self.DATA[self.name].items()}

    async def fetch_funding_rate_history(self, symbol, since, limit):
        base = symbol.split("/")[0]
        rate = self.HIST.get(base, self.DATA[self.name][base][0])
        return [{"fundingRate": rate}] * limit

    async def fetch_order_book(self, symbol, limit=None):
        if (self.name, symbol) in self.BOOKS:
            return self.BOOKS[(self.name, symbol)]
        base = symbol.split("/")[0]
        price = self.DATA[self.name][base][2]
        amount = self.BOOK_DEPTH_USD / price
        return {"asks": [[price * 1.0001, amount]], "bids": [[price * 0.9999, amount]]}

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.3f}"

    async def close(self):
        pass


class FakeClientFactory:
    """client_factory для Collector: настоящий ExchangeClient поверх FakeExchange."""

    def __init__(self, exchange_cls=FakeExchange, overrides: dict | None = None):
        self.exchange_cls = exchange_cls
        self.overrides = overrides or {}  # name -> класс биржи
        self.clients: dict[str, ExchangeClient] = {}

    def __call__(self, name: str) -> ExchangeClient:
        client = ExchangeClient.__new__(ExchangeClient)
        client.name = name
        client.ex = self.overrides.get(name, self.exchange_cls)(name)
        self.clients[name] = client
        return client
