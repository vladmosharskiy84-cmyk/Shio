"""Брокер: протокол и бумажная реализация на живых стаканах."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from engine.config import Settings
from engine.db import Db


@dataclass
class Fill:
    exchange: str
    symbol: str
    side: Literal["buy", "sell"]
    requested: float
    filled: float
    avg_price: float
    fee: float


class Broker(Protocol):
    async def round_amount(self, exchange: str, symbol: str, amount: float) -> float: ...

    async def market_ioc(self, exchange: str, symbol: str, kind: str, side: str,
                         amount: float, max_slippage: float) -> Fill: ...

    async def set_leverage(self, exchange: str, symbol: str, leverage: float) -> None: ...

    async def free_balance(self, exchange: str) -> float: ...

    async def add_margin(self, exchange: str, symbol: str, usd: float) -> bool: ...

    async def open_amounts(self, exchange: str) -> dict[str, float]: ...

    async def credit_funding(self, exchange: str, usd: float) -> None: ...

    async def on_liquidated(self, exchange: str, symbol: str) -> float: ...


class PaperBroker:
    """Симулирует торговлю по живому стакану; состояние хранится в kv базы."""

    def __init__(self, collector, settings: Settings, db: Db):
        self.collector = collector
        self.settings = settings
        self.db = db
        if self.db.get_kv("paper_balances") is None:
            self.db.set_kv("paper_balances", dict(settings.paper_balances))
        if self.db.get_kv("paper_holdings") is None:
            self.db.set_kv("paper_holdings", {})

    # --- состояние ---
    def _balances(self) -> dict:
        return self.db.get_kv("paper_balances", {})

    def _holdings(self) -> dict:
        return self.db.get_kv("paper_holdings", {})

    def _leverages(self) -> dict:
        return self.db.get_kv("paper_leverage", {})

    # --- Broker ---
    async def round_amount(self, exchange: str, symbol: str, amount: float) -> float:
        return await self.collector.round_amount(exchange, symbol, amount)

    async def set_leverage(self, exchange: str, symbol: str, leverage: float) -> None:
        lev = self._leverages()
        lev.setdefault(exchange, {})[symbol] = float(leverage)
        self.db.set_kv("paper_leverage", lev)

    async def free_balance(self, exchange: str) -> float:
        return float(self._balances().get(exchange, 0.0))

    async def credit_funding(self, exchange: str, usd: float) -> None:
        bal = self._balances()
        bal[exchange] = float(bal.get(exchange, 0.0)) + usd
        self.db.set_kv("paper_balances", bal)

    async def add_margin(self, exchange: str, symbol: str, usd: float) -> bool:
        holdings = self._holdings()
        h = holdings.get(exchange, {}).get(symbol)
        bal = self._balances()
        if h is None or h["kind"] != "perp" or float(bal.get(exchange, 0.0)) < usd:
            return False
        bal[exchange] = float(bal[exchange]) - usd
        h["margin"] += usd
        self.db.set_kv("paper_balances", bal)
        self.db.set_kv("paper_holdings", holdings)
        return True

    async def open_amounts(self, exchange: str) -> dict[str, float]:
        return {s: h["amount"] for s, h in self._holdings().get(exchange, {}).items()
                if h["amount"] != 0}

    async def on_liquidated(self, exchange: str, symbol: str) -> float:
        holdings = self._holdings()
        h = holdings.get(exchange, {}).pop(symbol, None)
        if h is None:
            return 0.0
        self.db.set_kv("paper_holdings", holdings)
        return float(h["margin"])

    async def market_ioc(self, exchange: str, symbol: str, kind: str, side: str,
                         amount: float, max_slippage: float) -> Fill:
        book = await self.collector.order_book(exchange, symbol)
        levels = book.get("asks" if side == "buy" else "bids") or []
        holdings = self._holdings()
        held = holdings.get(exchange, {}).get(symbol)
        held_amt = held["amount"] if held else 0.0
        sign = 1.0 if side == "buy" else -1.0

        reducing = (kind == "spot" and side == "sell") or (kind == "perp" and held_amt * sign < 0)
        want = amount
        if reducing:
            want = min(amount, abs(held_amt))

        filled, notional = 0.0, 0.0
        if levels and want > 0:
            best = levels[0][0]
            limit = best * (1 + max_slippage) if side == "buy" else best * (1 - max_slippage)
            for price, size in levels:
                if (side == "buy" and price > limit) or (side == "sell" and price < limit):
                    break
                take = min(want - filled, size)
                filled += take
                notional += take * price
                if filled >= want:
                    break
        if filled <= 0:
            return Fill(exchange, symbol, side, amount, 0.0, 0.0, 0.0)  # type: ignore[arg-type]
        avg = notional / filled
        cfg = self.settings.exchanges[exchange]
        fee = filled * avg * (cfg.spot_taker if kind == "spot" else cfg.perp_taker)

        bal = self._balances()
        free = float(bal.get(exchange, 0.0))
        ex_hold = holdings.setdefault(exchange, {})

        if kind == "spot":
            if side == "buy":
                cost = notional + fee
                if free < cost:
                    return Fill(exchange, symbol, side, amount, 0.0, 0.0, 0.0)  # type: ignore[arg-type]
                free -= cost
                total = held_amt + filled
                entry = ((held["entry"] * held_amt + notional) / total) if held else avg
                ex_hold[symbol] = {"kind": "spot", "amount": total, "entry": entry,
                                   "margin": 0.0, "leverage": 1.0}
            else:
                free += notional - fee
                self._reduce(ex_hold, symbol, held, filled)
        else:
            if reducing:
                pnl = (avg - held["entry"]) * filled if held_amt > 0 else (held["entry"] - avg) * filled
                released = held["margin"] * filled / abs(held_amt)
                free += released + pnl - fee
                self._reduce(ex_hold, symbol, held, filled, released)
            else:
                lev = float(self._leverages().get(exchange, {}).get(symbol, 1.0))
                margin = notional / lev
                if free < margin + fee:
                    return Fill(exchange, symbol, side, amount, 0.0, 0.0, 0.0)  # type: ignore[arg-type]
                free -= margin + fee
                old = abs(held_amt)
                entry = ((held["entry"] * old + notional) / (old + filled)) if held else avg
                ex_hold[symbol] = {"kind": "perp", "amount": held_amt + sign * filled, "entry": entry,
                                   "margin": (held["margin"] if held else 0.0) + margin,
                                   "leverage": lev}

        bal[exchange] = free
        self.db.set_kv("paper_balances", bal)
        self.db.set_kv("paper_holdings", holdings)
        return Fill(exchange, symbol, side, amount, filled, avg, fee)  # type: ignore[arg-type]

    @staticmethod
    def _reduce(ex_hold: dict, symbol: str, held: dict, filled: float,
                released: float = 0.0) -> None:
        direction = 1.0 if held["amount"] > 0 else -1.0
        left = held["amount"] - direction * filled
        if abs(left) < 1e-12:
            ex_hold.pop(symbol, None)
        else:
            ex_hold[symbol] = {**held, "amount": left, "margin": held["margin"] - released}
