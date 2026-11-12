import asyncio

import pytest

from engine.config import load_settings
from engine.db import Db
from engine.models import Leg, Position
from engine.reconciler import reconcile
from tests.engine.fakes import FakeNotifier, ScriptedBroker

PERP = "X/USDT:USDT"
SPOT = "X/USDT"


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


def mkpos(db, status, spot=7.5, perp=7.5, reason=None, entry=100.0):
    legs = [
        Leg(None, None, "bybit", SPOT, "spot", "long", spot, entry, 1.0, spot * 100.0,
            open=spot > 0),
        Leg(None, None, "okx", PERP, "perp", "short", perp, entry, 3.0, perp * 100.0 / 3,
            open=perp > 0),
    ]
    pos = Position(None, "spot-perp", "X", "calm", status, 1000.0, 1, close_reason=reason, legs=legs)
    db.save_position(pos)
    return db.positions()[-1]


def hold(broker, spot=0.0, perp=0.0):
    if spot:
        broker.amounts.setdefault("bybit", {})[SPOT] = spot
    if perp:
        broker.amounts.setdefault("okx", {})[PERP] = -perp


def audits(db, event):
    return db.conn.execute("SELECT detail FROM audit_log WHERE event=?", (event,)).fetchall()


def test_orphan_leg_after_restart_closed(db, settings):
    pos = mkpos(db, "opening", spot=7.5, perp=0.0)
    broker, notifier = ScriptedBroker(), FakeNotifier()
    hold(broker, spot=7.5)
    issues = run(reconcile(broker, db, notifier, settings))
    got = db.get_position(pos.id)
    assert got.status == "failed"
    assert got.close_reason == "незавершённое открытие"
    assert broker.calls == [("bybit", SPOT, "spot", "sell", 7.5)]
    assert run(broker.open_amounts("bybit")) == {}
    assert issues and audits(db, "reconcile_repair")


def test_orphan_leg_amount_taken_from_broker(db, settings):
    pos = mkpos(db, "opening", spot=0.0, perp=0.0)  # крах до сохранения исполнения
    broker = ScriptedBroker()
    hold(broker, spot=3.0)
    run(reconcile(broker, db, FakeNotifier(), settings))
    assert broker.calls == [("bybit", SPOT, "spot", "sell", 3.0)]
    assert db.get_position(pos.id).status == "failed"


def test_complete_opening_pair_promoted_to_open(db, settings):
    pos = mkpos(db, "opening")
    broker = ScriptedBroker()
    hold(broker, spot=7.5, perp=7.5)
    run(reconcile(broker, db, FakeNotifier(), settings))
    assert db.get_position(pos.id).status == "open"
    assert broker.calls == []


def test_unknown_exchange_position_reported_not_touched(db, settings):
    broker, notifier = ScriptedBroker(), FakeNotifier()
    broker.amounts["bybit"] = {"ZZZ/USDT": 5.0}
    issues = run(reconcile(broker, db, notifier, settings))
    assert len(issues) == 1 and "неизвестная позиция" in issues[0]
    assert audits(db, "reconcile_unknown")
    assert ("warn" in [lvl for _, lvl in notifier.sent])
    assert broker.calls == []


def test_missing_on_exchange_marked_closed(db, settings):
    pos = mkpos(db, "open")
    notifier = FakeNotifier()
    run(reconcile(ScriptedBroker(), db, notifier, settings))
    got = db.get_position(pos.id)
    assert got.status == "closed" and got.close_reason == "не найдена на бирже"
    assert all(not l.open and l.amount == 0 for l in got.legs)
    assert notifier.sent


def test_open_with_one_leg_missing_closed_with_alert(db, settings):
    pos = mkpos(db, "open")
    broker, notifier = ScriptedBroker(), FakeNotifier()
    hold(broker, spot=7.5)
    run(reconcile(broker, db, notifier, settings))
    got = db.get_position(pos.id)
    assert got.status == "closed" and got.close_reason == "нога не найдена на бирже"
    assert broker.calls == [("bybit", SPOT, "spot", "sell", 7.5)]
    assert "alert" in [lvl for _, lvl in notifier.sent]


def test_closing_with_leftover_leg_is_closed(db, settings):
    pos = mkpos(db, "closing", spot=0.0, perp=7.5, reason="ручное закрытие")
    broker = ScriptedBroker()
    hold(broker, perp=7.5)
    run(reconcile(broker, db, FakeNotifier(), settings))
    got = db.get_position(pos.id)
    assert got.status == "closed" and got.close_reason == "ручное закрытие"
    assert broker.calls == [("okx", PERP, "perp", "buy", 7.5)]


def test_closing_with_nothing_left_becomes_closed(db, settings):
    pos = mkpos(db, "closing", reason="ручное закрытие")
    broker = ScriptedBroker()
    run(reconcile(broker, db, FakeNotifier(), settings))
    got = db.get_position(pos.id)
    assert got.status == "closed"
    assert all(not l.open and l.amount == 0 for l in got.legs)
    assert broker.calls == []


def test_closing_liquidated_nothing_left_stays_liquidated(db, settings):
    pos = mkpos(db, "closing", reason="ликвидация")
    run(reconcile(ScriptedBroker(), db, FakeNotifier(), settings))
    assert db.get_position(pos.id).status == "liquidated"


def test_open_amounts_error_leaves_positions_untouched(db, settings):
    on_okx = mkpos(db, "open")
    broker, notifier = ScriptedBroker(), FakeNotifier()
    real = broker.open_amounts

    async def flaky(exchange):
        if exchange == "okx":
            raise RuntimeError("timeout")
        return await real(exchange)

    broker.open_amounts = flaky
    issues = run(reconcile(broker, db, notifier, settings))
    assert any("не удалось сверить" in i and "okx" in i for i in issues)
    assert audits(db, "reconcile_error")
    assert db.get_position(on_okx.id).status == "open"
    assert broker.calls == []


def test_only_trade_exchanges_queried(db, settings):
    asked = []
    broker = ScriptedBroker()
    real = broker.open_amounts

    async def spy(exchange):
        asked.append(exchange)
        return await real(exchange)

    broker.open_amounts = spy
    run(reconcile(broker, db, FakeNotifier(), settings))
    assert sorted(asked) == ["bybit", "hyperliquid", "okx"]


def test_orphan_leg_without_entry_price_has_no_fabricated_pnl(db, settings):
    pos = mkpos(db, "opening", spot=0.0, perp=0.0, entry=0.0)
    broker = ScriptedBroker()
    hold(broker, spot=3.0)
    run(reconcile(broker, db, FakeNotifier(), settings))
    got = db.get_position(pos.id)
    assert got.status == "failed"
    assert got.realized_pnl == pytest.approx(-0.3)  # только комиссия закрытия


def test_unknown_entry_price_reported(db, settings):
    mkpos(db, "opening", spot=0.0, perp=0.0, entry=0.0)
    broker = ScriptedBroker(prices={"ref_error": RuntimeError("нет цены")})
    hold(broker, spot=3.0)
    issues = run(reconcile(broker, db, FakeNotifier(), settings))
    assert any("цена входа неизвестна" in i for i in issues)
    assert audits(db, "reconcile_entry_price_unknown")
    assert broker.calls  # всё равно закрыто


def test_closing_leftover_liquidation_ends_liquidated(db, settings):
    pos = mkpos(db, "closing", spot=0.0, perp=7.5, reason="ликвидация")
    broker = ScriptedBroker()
    hold(broker, perp=7.5)
    run(reconcile(broker, db, FakeNotifier(), settings))
    assert db.get_position(pos.id).status == "liquidated"


def test_closing_incomplete_stays_closing_with_alert(db, settings):
    pos = mkpos(db, "closing", spot=0.0, perp=7.5, reason="ручное закрытие")
    broker, notifier = ScriptedBroker(script=[0.0] * 20), FakeNotifier()
    hold(broker, perp=7.5)
    run(reconcile(broker, db, notifier, settings))
    assert db.get_position(pos.id).status == "closing"
    assert "alert" in [lvl for _, lvl in notifier.sent]


def test_non_trade_exchange_leg_reported_and_skipped(db, settings):
    pos = mkpos(db, "open")
    pos.legs[0].exchange = "binance"
    db.update_position(pos)
    issues = run(reconcile(ScriptedBroker(), db, FakeNotifier(), settings))
    assert any("не торговая" in i for i in issues)
    assert db.get_position(pos.id).status == "open"


def test_open_amount_mismatch_synced_with_warning(db, settings):
    pos = mkpos(db, "open", spot=7.5, perp=7.5)
    broker, notifier = ScriptedBroker(), FakeNotifier()
    hold(broker, spot=7.5, perp=7.0)
    issues = run(reconcile(broker, db, notifier, settings))
    got = db.get_position(pos.id)
    assert got.status == "open" and got.legs[1].amount == pytest.approx(7.0)
    assert issues and "warn" in [lvl for _, lvl in notifier.sent]
    assert broker.calls == []
