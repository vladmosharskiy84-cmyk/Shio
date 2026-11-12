import asyncio

import pytest

from engine.broker import PaperBroker
from engine.config import load_settings
from engine.db import Db

SYM = "X/USDT:USDT"
SPOT = "X/USDT"


class FakeCollector:
    def __init__(self, books):
        self.books = books

    async def order_book(self, exchange, symbol):
        return self.books.get((exchange, symbol), {"bids": [], "asks": []})

    async def round_amount(self, exchange, symbol, amount):
        return round(amount, 2)


BOOK = {"bids": [[99.0, 5.0], [98.0, 5.0]], "asks": [[100.0, 1.0], [100.05, 1.0], [101.0, 10.0]]}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def settings():
    s = load_settings("config.yaml")
    s.paper_balances = {"bybit": 1000.0, "okx": 500.0}
    return s


@pytest.fixture
def db():
    d = Db(":memory:")
    d.init()
    return d


def make(settings, db, books=None):
    col = FakeCollector(books if books is not None else {("bybit", SYM): BOOK, ("bybit", SPOT): BOOK})
    return PaperBroker(col, settings, db)


def test_buy_walks_asks_and_charges_taker(settings, db):
    b = make(settings, db)
    f = run(b.market_ioc("bybit", SPOT, "spot", "buy", 2.0, 0.001))
    assert f.filled == 2.0
    assert f.avg_price == pytest.approx(100.025)
    rate = settings.exchanges["bybit"].spot_taker
    assert f.fee == pytest.approx(2.0 * 100.025 * rate)
    assert run(b.free_balance("bybit")) == pytest.approx(1000 - 200.05 - f.fee)


def test_ioc_partial_when_slippage_exceeded(settings, db):
    b = make(settings, db)
    f = run(b.market_ioc("bybit", SPOT, "spot", "buy", 5.0, 0.001))
    assert f.filled == 2.0 < f.requested
    assert f.requested == 5.0


def test_empty_book_fills_zero(settings, db):
    b = make(settings, db, books={})
    f = run(b.market_ioc("bybit", SYM, "perp", "sell", 1.0, 0.001))
    assert f.filled == 0 and f.fee == 0
    assert run(b.free_balance("bybit")) == 1000.0


def test_balance_decreases_by_margin_plus_fee(settings, db):
    b = make(settings, db)
    run(b.set_leverage("bybit", SYM, 2.0))
    f = run(b.market_ioc("bybit", SYM, "perp", "sell", 2.0, 0.001))
    assert f.filled == 2.0
    assert f.avg_price == pytest.approx(99.0)
    margin = 2.0 * 99.0 / 2.0
    assert run(b.free_balance("bybit")) == pytest.approx(1000 - margin - f.fee)
    assert f.fee == pytest.approx(2.0 * 99.0 * settings.exchanges["bybit"].perp_taker)


def test_insufficient_balance_fills_zero(settings, db):
    settings.paper_balances = {"bybit": 50.0}
    b = make(settings, db)
    f = run(b.market_ioc("bybit", SPOT, "spot", "buy", 1.0, 0.001))
    assert f.filled == 0 and f.fee == 0
    assert run(b.free_balance("bybit")) == 50.0
    assert run(b.open_amounts("bybit")) == {}


def test_open_amounts_tracks_holdings(settings, db):
    b = make(settings, db)
    run(b.market_ioc("bybit", SYM, "perp", "sell", 2.0, 0.001))
    run(b.market_ioc("bybit", SPOT, "spot", "buy", 1.0, 0.001))
    assert run(b.open_amounts("bybit")) == {SYM: -2.0, SPOT: 1.0}
    assert run(b.open_amounts("okx")) == {}


def test_state_survives_new_instance(settings, db):
    b = make(settings, db)
    run(b.market_ioc("bybit", SYM, "perp", "sell", 2.0, 0.001))
    bal = run(b.free_balance("bybit"))
    b2 = make(settings, db)
    assert run(b2.free_balance("bybit")) == pytest.approx(bal)
    assert run(b2.open_amounts("bybit")) == {SYM: -2.0}
    assert bal < 1000.0


def test_credit_funding(settings, db):
    b = make(settings, db)
    run(b.credit_funding("bybit", 1.5))
    run(b.credit_funding("bybit", -0.5))
    assert run(b.free_balance("bybit")) == pytest.approx(1001.0)
    assert run(make(settings, db).free_balance("bybit")) == pytest.approx(1001.0)


def test_add_margin(settings, db):
    b = make(settings, db)
    assert run(b.add_margin("bybit", SYM, 10.0)) is False  # нет позиции
    run(b.market_ioc("bybit", SYM, "perp", "sell", 2.0, 0.001))
    bal = run(b.free_balance("bybit"))
    assert run(b.add_margin("bybit", SYM, 10.0)) is True
    assert run(b.free_balance("bybit")) == pytest.approx(bal - 10.0)
    assert db.get_kv("paper_holdings")["bybit"][SYM]["margin"] == pytest.approx(198.0 + 10.0)
    assert run(b.add_margin("bybit", SYM, 1e9)) is False
    assert run(b.free_balance("bybit")) == pytest.approx(bal - 10.0)


def test_on_liquidated_loses_margin(settings, db):
    b = make(settings, db)
    run(b.market_ioc("bybit", SYM, "perp", "sell", 2.0, 0.001))
    bal = run(b.free_balance("bybit"))
    assert run(b.on_liquidated("bybit", SYM)) == pytest.approx(198.0)
    assert run(b.open_amounts("bybit")) == {}
    assert run(b.free_balance("bybit")) == pytest.approx(bal)
    assert run(b.on_liquidated("bybit", SYM)) == 0.0


def test_spot_sell_capped_at_holdings(settings, db):
    b = make(settings, db)
    run(b.market_ioc("bybit", SPOT, "spot", "buy", 1.0, 0.001))
    f = run(b.market_ioc("bybit", SPOT, "spot", "sell", 3.0, 0.001))
    assert f.filled == 1.0
    assert f.avg_price == 99.0
    assert run(b.open_amounts("bybit")) == {}
    f2 = run(b.market_ioc("bybit", SPOT, "spot", "sell", 1.0, 0.001))
    assert f2.filled == 0


def test_perp_close_realizes_pnl(settings, db):
    books = {("bybit", SYM): BOOK}
    b = make(settings, db, books)
    run(b.market_ioc("bybit", SYM, "perp", "sell", 2.0, 0.001))  # short @99
    bal_after_open = run(b.free_balance("bybit"))
    books[("bybit", SYM)] = {"bids": [[89.0, 10.0]], "asks": [[90.0, 10.0]]}
    f = run(b.market_ioc("bybit", SYM, "perp", "buy", 5.0, 0.001))  # capped at 2
    assert f.filled == 2.0 and f.avg_price == 90.0
    pnl = (99.0 - 90.0) * 2.0
    assert run(b.free_balance("bybit")) == pytest.approx(bal_after_open + 198.0 + pnl - f.fee)
    assert run(b.open_amounts("bybit")) == {}


def test_perp_partial_reduce_releases_margin_proportionally(settings, db):
    b = make(settings, db)
    run(b.market_ioc("bybit", SYM, "perp", "sell", 2.0, 0.001))
    run(b.market_ioc("bybit", SYM, "perp", "buy", 1.0, 0.001))
    h = db.get_kv("paper_holdings")["bybit"][SYM]
    assert h["amount"] == pytest.approx(-1.0)
    assert h["margin"] == pytest.approx(99.0)


def test_round_amount_delegates(settings, db):
    assert run(make(settings, db).round_amount("bybit", SYM, 1.23456)) == 1.23


def test_ref_price_best_ask_bid_and_empty(settings, db):
    b = make(settings, db)
    assert run(b.ref_price("bybit", SYM, "buy")) == 100.0
    assert run(b.ref_price("bybit", SYM, "sell")) == 99.0
    assert run(b.ref_price("okx", SYM, "buy")) == 0.0


def test_perp_reduce_loss_clamped_at_released_margin(settings, db):
    # шорт 2 по 99 с плечом 2: маржа 99; закрытие по 200 — убыток 202 больше маржи
    books = {("bybit", SYM): BOOK}
    b = make(settings, db, books)
    run(b.set_leverage("bybit", SYM, 2.0))
    run(b.market_ioc("bybit", SYM, "perp", "sell", 2.0, 0.001))
    before = run(b.free_balance("bybit"))
    books[("bybit", SYM)] = {"bids": [[199.0, 10.0]], "asks": [[200.0, 10.0]]}
    # половина: освобождается 49.5 маржи, убыток 101 — баланс не уменьшается
    f = run(b.market_ioc("bybit", SYM, "perp", "buy", 1.0, 0.001))
    assert f.filled == 1.0
    assert run(b.free_balance("bybit")) == pytest.approx(before)
    f = run(b.market_ioc("bybit", SYM, "perp", "buy", 1.0, 0.001))
    assert run(b.free_balance("bybit")) == pytest.approx(before)
    assert run(b.open_amounts("bybit")) == {}
