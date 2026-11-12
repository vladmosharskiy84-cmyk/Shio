import asyncio

import pytest

from engine.config import load_settings
from engine.db import Db
from engine.executor import close_pair, open_pair
from engine.models import Position
from tests.engine.fakes import FakeNotifier, ScriptedBroker

PERP = "X/USDT:USDT"
SPOT = "X/USDT"
PLAN = [
    {"exchange": "bybit", "symbol": SPOT, "kind": "spot", "side": "long"},
    {"exchange": "okx", "symbol": PERP, "kind": "perp", "side": "short"},
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def settings():
    return load_settings("config.yaml")


@pytest.fixture
def db():
    d = Db(":memory:")
    d.init()
    return d


def do_open(broker, db, settings, notifier=None, capital=1000.0):
    notifier = notifier or FakeNotifier()
    pos = run(open_pair(PLAN, capital, "X", "spot-perp", "calm", broker, db, notifier, settings))
    return pos, notifier


def test_opens_both_legs_equal_amount(db, settings):
    broker = ScriptedBroker()
    pos, _ = do_open(broker, db, settings)
    assert pos.status == "open"
    spot, perp = pos.legs
    # капитал 1000, плечо 3: номинал = 1000 / (1 + 1/3) = 750; цена 100 -> 7.5
    assert spot.amount == perp.amount == pytest.approx(7.5)
    assert spot.entry_price == 100.0
    assert spot.margin == pytest.approx(750.0) and spot.leverage == 1.0
    assert perp.margin == pytest.approx(250.0) and perp.leverage == settings.limits.max_leverage
    assert spot.fees_paid == pytest.approx(0.75)
    assert [c[3] for c in broker.calls] == ["buy", "sell"]
    assert broker.leverages[("okx", PERP)] == settings.limits.max_leverage
    assert db.get_position(pos.id).status == "open"
    assert db.get_position(pos.id).legs[0].amount == pytest.approx(7.5)


def test_status_opening_written_before_first_order(db, settings):
    seen = {}

    def hook():
        ps = db.positions()
        seen["statuses"] = [p.status for p in ps]
        seen["legs"] = [(l.amount, l.entry_price) for l in ps[0].legs]

    do_open(ScriptedBroker(hook=hook), db, settings)
    assert seen["statuses"] == ["opening"]
    assert seen["legs"] == [(0.0, 0.0), (0.0, 0.0)]


def test_second_leg_fails_three_times_unwinds_first(db, settings):
    broker = ScriptedBroker(script=[1.0, 0.0, 0.0, 0.0])
    pos, notifier = do_open(broker, db, settings)
    assert pos.status == "failed"
    assert run(broker.open_amounts("bybit")) == {}
    assert run(broker.open_amounts("okx")) == {}
    assert len([c for c in broker.calls if c[0] == "okx"]) == settings.limits.leg_retries
    assert "закрыта первая нога" in notifier.texts()
    assert notifier.sent[-1][1] == "alert"
    assert db.get_position(pos.id).status == "failed"


def test_partial_second_leg_shrinks_first(db, settings):
    # вторая нога: 40% и затем 0 + 0 -> итог 40%; первая сокращается до 3.0
    broker = ScriptedBroker(script=[1.0, 0.4, 0.0, 0.0])
    pos, _ = do_open(broker, db, settings)
    assert pos.status == "open"
    spot, perp = pos.legs
    assert spot.amount == perp.amount == pytest.approx(3.0)
    assert run(broker.open_amounts("bybit")) == {SPOT: pytest.approx(3.0)}
    assert run(broker.open_amounts("okx")) == {PERP: pytest.approx(-3.0)}


def test_second_leg_retry_completes_remainder(db, settings):
    broker = ScriptedBroker(script=[1.0, 0.5, 1.0])
    pos, _ = do_open(broker, db, settings)
    assert pos.status == "open"
    assert pos.legs[0].amount == pos.legs[1].amount == pytest.approx(7.5)


def test_thin_book_opens_nothing(db, settings):
    broker = ScriptedBroker(script=[0.0])
    pos, notifier = do_open(broker, db, settings)
    assert pos.status == "failed"
    assert len(broker.calls) == 1
    assert run(broker.open_amounts("bybit")) == {} and run(broker.open_amounts("okx")) == {}
    assert notifier.sent


def test_zero_ref_price_fails_without_orders(db, settings):
    broker = ScriptedBroker(prices={("okx", PERP): 0.0})
    pos, notifier = do_open(broker, db, settings)
    assert pos.status == "failed" and broker.calls == [] and notifier.sent


def test_close_pair_records_realized_pnl(db, settings):
    broker = ScriptedBroker()
    pos, notifier = do_open(broker, db, settings)
    pos.legs[1].funding_received = 2.0
    db.update_position(pos)
    broker.prices = {("bybit", SPOT): 110.0, ("okx", PERP): 105.0}
    closed = run(close_pair(pos, "тест", broker, db, notifier, settings))
    assert closed.status == "closed" and closed.close_reason == "тест"
    assert closed.closed_at is not None and not any(l.open for l in closed.legs)
    # цена: спот (110-100)*7.5 = 75; перп-шорт (100-105)*7.5 = -37.5; фандинг 2
    # комиссии: открытие 0.75+0.75, закрытие 110*7.5*.001 + 105*7.5*.001
    fees = 0.75 + 0.75 + 0.825 + 0.7875
    assert closed.realized_pnl == pytest.approx(75 - 37.5 + 2 - fees)
    assert broker.calls[-2][0] == "okx" and broker.calls[-1][0] == "bybit"  # обратный порядок
    assert run(broker.open_amounts("bybit")) == {} and run(broker.open_amounts("okx")) == {}
    assert db.get_position(pos.id).status == "closed"


def test_close_pair_failure_keeps_closing_and_alerts(db, settings):
    broker = ScriptedBroker()
    pos, notifier = do_open(broker, db, settings)
    broker.script = [0.0, 0.0, 0.0]
    out = run(close_pair(pos, "тест", broker, db, notifier, settings))
    assert out.status == "closing"
    assert db.get_position(pos.id).status == "closing"
    assert notifier.sent[-1][1] == "alert"
    assert out.legs[1].open
