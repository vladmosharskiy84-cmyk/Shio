"""Общие тестовые заглушки движка."""
from __future__ import annotations

from engine.broker import Fill
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


class FakeNotifier:
    """Запоминает уведомления как кортежи (текст, уровень)."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.proposals: list[tuple] = []  # (Proposal, ttl_min, hold_days)
        self.document_failures = 0  # сколько первых send_document вернут False
        self.documents: list[tuple[str, bytes, str]] = []  # (имя файла, данные, подпись)
        self.buttons: list = []  # кнопки отправок, у которых они были

    async def send(self, text: str, level: str = "info", buttons=None) -> None:
        self.sent.append((text, level))
        if buttons:
            self.buttons.append(buttons)

    async def proposal(self, p, ttl_min: int, hold_days: float) -> None:
        self.proposals.append((p, ttl_min, hold_days))

    async def send_document(self, filename: str, data: bytes, caption: str) -> bool:
        if self.document_failures > 0:
            self.document_failures -= 1
            return False
        self.documents.append((filename, data, caption))
        return True

    def texts(self) -> str:
        return "\n".join(t for t, _ in self.sent)


class ScriptedBroker:
    """Брокер с заранее заданными результатами market_ioc (по порядку вызовов).

    script: элементы — доля исполнения (float) либо готовый Fill; после исчерпания — 1.0.
    Элемент-исключение — market_ioc бросает его.
    hook: вызывается перед market_ioc с номером hook_on_call (по умолчанию первым).
    """

    def __init__(self, script=None, prices: dict | None = None, default_price: float = 100.0,
                 fee_rate: float = 0.001, hook=None, hook_on_call: int = 1):
        self.script = list(script or [])
        self.prices = prices or {}
        self.default_price = default_price
        self.fee_rate = fee_rate
        self.hook = hook
        self.hook_on_call = hook_on_call
        self.calls: list[tuple] = []  # (exchange, symbol, kind, side, amount)
        self.leverages: dict = {}
        self.amounts: dict[str, dict[str, float]] = {}

    def _price(self, exchange, symbol):
        return self.prices.get((exchange, symbol), self.default_price)

    async def ref_price(self, exchange, symbol, side):
        if isinstance(self.prices.get("ref_error"), Exception):
            raise self.prices["ref_error"]
        return self._price(exchange, symbol)

    async def round_amount(self, exchange, symbol, amount):
        return round(amount, 3)

    async def set_leverage(self, exchange, symbol, leverage):
        self.leverages[(exchange, symbol)] = leverage

    async def market_ioc(self, exchange, symbol, kind, side, amount, max_slippage):
        if self.hook is not None and len(self.calls) + 1 == self.hook_on_call:
            self.hook()
        self.calls.append((exchange, symbol, kind, side, amount))
        item = self.script.pop(0) if self.script else 1.0
        if isinstance(item, Exception):
            raise item
        if isinstance(item, Fill):
            fill = item
        else:
            price = self._price(exchange, symbol)
            filled = amount * item
            fill = Fill(exchange, symbol, side, amount, filled, price if filled else 0.0,
                        filled * price * self.fee_rate)
        sign = 1.0 if side == "buy" else -1.0
        ex = self.amounts.setdefault(exchange, {})
        ex[symbol] = ex.get(symbol, 0.0) + sign * fill.filled
        return fill

    async def free_balance(self, exchange):
        return 1e9

    async def add_margin(self, exchange, symbol, usd):
        return True

    async def open_amounts(self, exchange):
        return {s: a for s, a in self.amounts.get(exchange, {}).items() if abs(a) > 1e-9}

    async def credit_funding(self, exchange, usd):
        pass

    async def on_liquidated(self, exchange, symbol):
        return 0.0
