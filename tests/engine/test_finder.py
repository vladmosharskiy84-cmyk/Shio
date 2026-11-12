import asyncio
import logging

import pytest

from engine import collector as collector_mod
from engine.collector import Collector
from engine.config import load_settings
from engine.finder import classify_profile, find_candidates
from scanner.calc import Opportunity
from tests.engine.fakes import FakeClientFactory, FakeExchange

DATA = {
    "bybit": {"HOT": (0.0004, "8h", 100.0), "BTC": (0.0001, "8h", 60000.0)},
    "okx": {"HOT": (-0.0002, "8h", 100.0), "BTC": (0.0001, "8h", 60005.0)},
    "hyperliquid": {"BTC": (0.0001, "8h", 60000.0)},
    # у gate ставка лучшая, но gate не торговая биржа
    "gate": {"HOT": (0.0100, "8h", 100.0), "BTC": (0.0001, "8h", 60000.0)},
}


class Ex(FakeExchange):
    DATA = DATA
    BOOKS: dict = {}


@pytest.fixture
def settings():
    s = load_settings("config.yaml")
    for name, e in s.exchanges.items():
        e.watch = name in DATA
    return s


def run(coro):
    return asyncio.run(coro)


async def candidates(settings, exchange_cls=Ex, overrides=None):
    factory = FakeClientFactory(exchange_cls, overrides)
    col = Collector(settings, factory)
    try:
        snap = await col.collect()
        return snap, await find_candidates(snap, col, settings, lambda kind: 300.0), factory
    finally:
        await col.close()


def test_only_trade_exchange_legs(settings):
    snap, cands, _ = run(candidates(settings))
    assert snap.ok["gate"] is True and snap.perp("gate", "HOT/USDT:USDT") is not None
    assert cands
    for c in cands:
        assert all(settings.exchanges[leg["exchange"]].trade for leg in c.legs_plan)
    assert not any(leg["exchange"] == "gate" for c in cands for leg in c.legs_plan)
    top = cands[0]
    assert top.op.base == "HOT" and top.capital == 300.0
    assert [(l["kind"], l["side"]) for l in top.legs_plan] in (
        [("spot", "long"), ("perp", "short")], [("perp", "short"), ("perp", "long")])
    assert top.liquidity_ok is True
    assert [c.op.apr_net_capital for c in cands] == sorted(
        (c.op.apr_net_capital for c in cands), reverse=True)


def test_thin_book_marks_liquidity_false(settings):
    class Thin(Ex):
        BOOKS = {("bybit", "HOT/USDT"): {"asks": [[100.0, 0.1]], "bids": [[99.9, 0.1]]}}

    _, cands, _ = run(candidates(settings, Thin))
    sp = next(c for c in cands if c.op.kind == "spot-perp" and c.op.base == "HOT")
    assert sp.liquidity_ok is False
    pp = next(c for c in cands if c.op.kind == "perp-perp" and c.op.base == "HOT")
    assert pp.liquidity_ok is True


def test_depth_only_for_top_candidates(settings):
    settings.history_check_top = 1
    _, cands, factory = run(candidates(settings))
    assert len(cands) > 1
    assert cands[0].liquidity_ok is True
    assert all(c.liquidity_ok is False for c in cands[1:])


def op(apr, rate=0.002, hist=None):
    return Opportunity("spot-perp", "X", "", rate, 0, apr, 100, 1, 0.1, 0.0, hist, None, {})


def test_profile_spike_is_aggressive():
    assert classify_profile(op(30, hist=0.0002), 80) == "aggressive"  # история < 40% ставки
    assert classify_profile(op(30, hist=None), 80) == "aggressive"     # нет истории
    assert classify_profile(op(90, hist=0.002), 80) == "aggressive"    # доходность выше порога


def test_profile_stable_moderate_is_calm():
    assert classify_profile(op(30, hist=0.0018), 80) == "calm"


def test_failed_exchange_marked_not_ok(settings, caplog):
    class Broken(Ex):
        async def load_markets(self):
            raise RuntimeError("451 restricted location")

    class NoFunding(Ex):
        has = {"fetchFundingRates": False}

    collector_mod._warned_no_funding.clear()
    settings.exchanges["mexc"].watch = True
    with caplog.at_level(logging.WARNING):
        snap, cands, _ = run(candidates(settings, overrides={"okx": Broken, "mexc": NoFunding}))
        run(candidates(settings, overrides={"okx": Broken, "mexc": NoFunding}))
    assert snap.ok["okx"] is False and snap.ok["mexc"] is False
    assert snap.ok["bybit"] is True and snap.ok["hyperliquid"] is True
    assert snap.perp("bybit", "HOT/USDT:USDT") is not None
    assert snap.perp("okx", "HOT/USDT:USDT") is None
    assert sum("mexc" in r.getMessage() and "funding" in r.getMessage() for r in caplog.records) == 1


def test_history_enriched_and_clients_reused_and_closed(settings):
    async def go():
        factory = FakeClientFactory(Ex)
        col = Collector(settings, factory)
        snap = await col.collect()
        assert snap.perp("bybit", "HOT/USDT:USDT").hist_avg_rate_per_h is not None
        await col.order_book("bybit", "HOT/USDT")
        client = factory.clients["bybit"]
        assert await col.round_amount("bybit", "HOT/USDT", 1.23456) == 1.235
        assert factory.clients["bybit"] is client
        closed = []
        for c in factory.clients.values():
            async def close(c=c):
                closed.append(c.name)
            c.close = close
        await col.close()
        return closed, set(factory.clients)

    closed, names = run(go())
    assert set(closed) == names


def test_history_enrichment_limited_to_trade_exchanges(settings):
    # у gate (не торговая) запредельные ставки по всем монетам: раньше топ-N истории
    # уходил на gate, и торговые кандидаты оставались без истории
    data = {"bybit": {"HOT": (0.0004, "8h", 100.0)},
            "okx": {"HOT": (-0.0002, "8h", 100.0)},
            "gate": {"HOT": (0.0100, "8h", 100.0)}}

    class Ex3(FakeExchange):
        DATA = data
        BOOKS: dict = {}

    for name, e in settings.exchanges.items():
        e.watch = name in data
    settings.history_check_top = 1
    snap, _, _ = run(candidates(settings, Ex3))
    assert snap.perp("bybit", "HOT/USDT:USDT").hist_avg_rate_per_h is not None
    assert snap.perp("gate", "HOT/USDT:USDT").hist_avg_rate_per_h is None


class Hyper(Ex):
    """Hyperliquid: рынки к USDC (фьючерс HOT/USDC:USDC, спот HOT/USDC)."""
    HL = {"HOT": (-0.0004, "1h", 100.0)}

    async def load_markets(self):
        out = {}
        for b in self.HL:
            out[f"{b}/USDC:USDC"] = {"base": b, "quote": "USDC", "settle": "USDC", "swap": True,
                                     "linear": True, "active": True}
            out[f"{b}/USDC"] = {"base": b, "quote": "USDC", "spot": True, "active": True}
        return out

    async def fetch_funding_rates(self):
        return {f"{b}/USDC:USDC": {"fundingRate": r, "interval": i, "markPrice": p}
                for b, (r, i, p) in self.HL.items()}

    async def fetch_tickers(self, symbols, params):
        suffix = ":USDC" if params["type"] == "swap" else ""
        return {f"{b}/USDC{suffix}": {"last": p, "quoteVolume": 5e7}
                for b, (r, i, p) in self.HL.items()}

    async def fetch_funding_rate_history(self, symbol, since, limit):
        return [{"fundingRate": self.HL[symbol.split("/")[0]][0]}] * limit

    async def fetch_order_book(self, symbol, limit=None):
        price = self.HL[symbol.split("/")[0]][2]
        amount = self.BOOK_DEPTH_USD / price
        return {"asks": [[price * 1.0001, amount]], "bids": [[price * 0.9999, amount]]}


def test_hyperliquid_usdc_markets_trade_with_real_symbols(settings):
    snap, cands, _ = run(candidates(settings, overrides={"hyperliquid": Hyper}))
    hl = snap.perp("hyperliquid", "HOT/USDC:USDC")
    assert hl is not None and hl.hist_avg_rate_per_h is not None
    pp = next(c for c in cands if c.op.kind == "perp-perp" and c.op.base == "HOT"
              and any(l["exchange"] == "hyperliquid" for l in c.legs_plan))
    by_ex = {l["exchange"]: l for l in pp.legs_plan}
    assert by_ex["hyperliquid"]["symbol"] == "HOT/USDC:USDC"
    assert by_ex["hyperliquid"]["side"] == "long"  # ставка hyperliquid ниже
    other = next(l for ex, l in by_ex.items() if ex != "hyperliquid")
    assert other["symbol"] == "HOT/USDT:USDT"
    assert pp.liquidity_ok is True


def test_okx_contract_size_converted_to_base_units(settings):
    class Okx(Ex):
        async def load_markets(self):
            out = await super().load_markets()
            out["HOT/USDT:USDT"] = {**out["HOT/USDT:USDT"], "contractSize": 0.01}
            return out

        async def fetch_order_book(self, symbol, limit=None):
            # стакан в контрактах: 500 контрактов по 0.01 = 5 монет
            return {"asks": [[100.0, 500.0]], "bids": [[99.9, 300.0]]}

        def amount_to_precision(self, symbol, amount):
            return f"{int(amount)}"  # целое число контрактов

    async def go():
        col = Collector(settings, FakeClientFactory(Ex, {"okx": Okx}))
        try:
            book = await col.order_book("okx", "HOT/USDT:USDT")
            amount = await col.round_amount("okx", "HOT/USDT:USDT", 1.23456)
            spot_amount = await col.round_amount("okx", "HOT/USDT", 1.9)
            return book, amount, spot_amount
        finally:
            await col.close()

    book, amount, spot_amount = run(go())
    assert book["asks"] == [[100.0, pytest.approx(5.0)]]
    assert book["bids"] == [[99.9, pytest.approx(3.0)]]
    assert amount == pytest.approx(1.23)   # 123 контракта × 0.01
    assert spot_amount == 1.0              # у спота contractSize нет — без пересчёта
