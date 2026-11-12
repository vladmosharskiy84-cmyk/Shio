import asyncio
import dataclasses

import pytest

from engine.collector import MarketSnapshot
from engine.config import load_settings
from engine.db import Db
from engine.guardian import GuardAction, guard
from engine.models import Leg, Position
from scanner.calc import PerpInfo, SpotInfo, liquidation_price, margin_buffer
from tests.engine.fakes import FakeNotifier, ScriptedBroker

PERP = "X/USDT:USDT"
SPOT = "X/USDT"
H = 3_600_000
NOW = 1_000_000_000_000
# шорт 7.5 по 100, маржа 250 (плечо 3): ликвидация 100 * (1 + 1/3 - 0.005) ≈ 132.83


def run(coro):
    return asyncio.run(coro)


class Broker(ScriptedBroker):
    def __init__(self, *a, topup_ok=True, lost=0.0, **kw):
        super().__init__(*a, **kw)
        self.topup_ok = topup_ok
        self.lost = lost
        self.topups: list[tuple] = []
        self.liquidated: list[tuple] = []

    async def add_margin(self, exchange, symbol, usd):
        self.topups.append((exchange, symbol, usd))
        return self.topup_ok

    async def on_liquidated(self, exchange, symbol):
        self.liquidated.append((exchange, symbol))
        return self.lost


@pytest.fixture
def settings():
    return load_settings("config.yaml")


@pytest.fixture
def db():
    d = Db(":memory:")
    d.init()
    return d


def make_pos(db, spot_amount=7.5, perp_amount=7.5):
    legs = [Leg(None, None, "bybit", SPOT, "spot", "long", spot_amount, 100.0, 1.0, spot_amount * 100.0),
            Leg(None, None, "okx", PERP, "perp", "short", perp_amount, 100.0, 3.0, perp_amount * 100.0 / 3)]
    p = Position(None, "spot-perp", "X", "calm", "open", 1000.0, 0, legs=legs)
    db.save_position(p)
    return p


def snap(mark=100.0, spot=None, rate=0.0001, perp=True, spot_present=True):
    perps = [PerpInfo("okx", "X", PERP, rate, 8.0, mark, 1e6, NOW + H)] if perp else []
    spots = [SpotInfo("bybit", "X", SPOT, mark if spot is None else spot, 1e6)] if spot_present else []
    return MarketSnapshot(ts=NOW, perps=perps, spots=spots)


def do_guard(positions, snapshot, broker, db, settings, notifier=None, last_marks=None, now=NOW):
    notifier = notifier if notifier is not None else FakeNotifier()
    marks = last_marks if last_marks is not None else {}
    out = run(guard(positions, snapshot, broker, db, notifier, settings, marks, now_ms=now))
    return out, notifier, marks


def kinds(actions):
    return [a.kind for a in actions]


def test_healthy_position_ok(db, settings):
    p = make_pos(db)
    broker = Broker()
    actions, notifier, marks = do_guard([p], snap(), broker, db, settings)
    assert actions == [GuardAction(p.id, "ok", actions[0].detail)]
    assert notifier.sent == [] and broker.calls == [] and broker.topups == []
    assert marks == {("okx", PERP): 100.0, ("bybit", SPOT): 100.0}


def test_warn_at_40_percent_once_per_hour(db, settings):
    p = make_pos(db)
    broker = Broker()
    notifier = FakeNotifier()
    # 121: запас (132.83 - 121) / 32.83 ≈ 0.36 — между 20% и 40%
    actions, _, _ = do_guard([p], snap(121.0), broker, db, settings, notifier)
    assert kinds(actions) == ["warn"]
    assert len(notifier.sent) == 1 and notifier.sent[0][1] == "warning"
    assert "ликвидац" in notifier.sent[0][0]
    actions, _, _ = do_guard([p], snap(121.0), broker, db, settings, notifier, now=NOW + H - 1)
    assert kinds(actions) == ["warn"] and len(notifier.sent) == 1
    actions, _, _ = do_guard([p], snap(121.0), broker, db, settings, notifier, now=NOW + H)
    assert kinds(actions) == ["warn"] and len(notifier.sent) == 2
    assert broker.topups == [] and broker.calls == []


def test_topup_at_20_percent(db, settings):
    p = make_pos(db)
    broker = Broker()
    # 127: запас ≈ 0.18; цель — 50%: ликв. 2*127-100 = 154, 1/плечо = 1.54-1+0.005 = 0.545
    actions, notifier, _ = do_guard([p], snap(127.0), broker, db, settings)
    assert kinds(actions) == ["topup"]
    assert len(broker.topups) == 1
    ex, sym, usd = broker.topups[0]
    assert (ex, sym) == ("okx", PERP)
    assert usd == pytest.approx(100 * 7.5 * 0.545 - 250.0)
    perp = db.get_position(p.id).legs[1]
    assert perp.margin == pytest.approx(408.75)
    lev = perp.entry_price * perp.amount / perp.margin
    assert margin_buffer(100.0, 127.0, liquidation_price(100.0, "short", lev)) == pytest.approx(0.5)
    assert db.get_position(p.id).status == "open"
    assert notifier.sent and "маржа" in notifier.texts()
    events = [r["event"] for r in db.conn.execute("SELECT event FROM audit_log")]
    assert "margin_topup" in events


def test_close_when_topup_fails(db, settings):
    p = make_pos(db)
    broker = Broker(topup_ok=False)
    actions, notifier, _ = do_guard([p], snap(127.0), broker, db, settings)
    assert kinds(actions) == ["close"]
    assert "угроза ликвидации" in actions[0].detail
    stored = db.get_position(p.id)
    assert stored.status == "closed" and stored.close_reason == "угроза ликвидации"
    assert [(c[0], c[3], c[4]) for c in broker.calls] == [("okx", "buy", 7.5), ("bybit", "sell", 7.5)]
    assert not any(l.open for l in stored.legs)


def test_gap_past_liquidation_marks_liquidated_and_pauses(db, settings):
    p = make_pos(db)
    broker = Broker(lost=250.0, prices={("bybit", SPOT): 140.0})
    # цена 140 уже за ликвидацией 132.83
    actions, notifier, _ = do_guard([p], snap(140.0), broker, db, settings)
    assert kinds(actions) == ["liquidated"]
    assert broker.liquidated == [("okx", PERP)]
    assert broker.topups == []
    # перп-нога не торговалась, закрыта только спот-нога
    assert [(c[0], c[3]) for c in broker.calls] == [("bybit", "sell")]
    stored = db.get_position(p.id)
    assert stored.status == "liquidated"
    spot, perp = stored.legs
    assert not perp.open and perp.amount == 0 and perp.margin == 0
    assert not spot.open
    # спот: (140-100)*7.5 = 300; комиссия закрытия 140*7.5*0.001 = 1.05; потеря маржи 250
    assert stored.realized_pnl == pytest.approx(300 - 1.05 - 250.0)
    assert db.get_kv("paused") is True
    assert notifier.sent[-1][1] == "alert" and "ликвидирован" in notifier.sent[-1][0]
    # итог в уведомлении о закрытии уже учитывает потерю маржи
    assert any("Итог: +48.95 USD" in t for t, _ in notifier.sent)
    events = [r["event"] for r in db.conn.execute("SELECT event FROM audit_log")]
    assert "liquidated" in events


def test_missing_symbol_uses_last_mark_and_warns(db, settings):
    p = make_pos(db)
    broker = Broker()
    notifier = FakeNotifier()
    marks = {("okx", PERP): 121.0, ("bybit", SPOT): 121.0}
    # монета пропала из данных: ни перпа, ни спота в снимке
    gone = snap(perp=False, spot_present=False)
    actions, _, _ = do_guard([p], gone, broker, db, settings, notifier, marks)
    # последняя известная цена 121 даёт запас ≈ 0.36 -> проверки продолжаются
    assert kinds(actions) == ["stale", "warn"]
    assert any(lvl == "warning" and "устарел" in t for t, lvl in notifier.sent)
    n = len(notifier.sent)
    actions, _, _ = do_guard([p], gone, broker, db, settings, notifier, marks, now=NOW + 60_000)
    assert kinds(actions) == ["stale", "warn"] and len(notifier.sent) == n  # не чаще раза в час
    # цены нет совсем — только stale, без падения
    q = make_pos(db)
    actions, _, _ = do_guard([q], gone, broker, db, settings, FakeNotifier(), {})
    assert kinds(actions) == ["stale"]
    assert broker.calls == [] and broker.topups == []
    # ставка None тоже не валит проверку
    actions, _, _ = do_guard([q], snap(rate=None), broker, db, settings)
    assert kinds(actions) == ["ok"]


def test_reversal_closes_in_auto_proposes_in_semi(db, settings):
    # шорт при отрицательной ставке платит -> ожидаемый доход за 24 ч < 0
    semi = dataclasses.replace(settings, mode="semi")
    p = make_pos(db)
    broker = Broker()
    notifier = FakeNotifier()
    actions, _, _ = do_guard([p], snap(rate=-0.0001), broker, db, semi, notifier)
    assert kinds(actions) == ["propose_close"]
    assert broker.calls == [] and db.get_position(p.id).status == "open"
    assert len(notifier.sent) == 1 and "ставка развернулась" in notifier.sent[0][0]
    do_guard([p], snap(rate=-0.0001), broker, db, semi, notifier, now=NOW + 1000)
    assert len(notifier.sent) == 1  # не чаще раза в час

    auto = dataclasses.replace(settings, mode="auto")
    actions, _, _ = do_guard([p], snap(rate=-0.0001), broker, db, auto)
    assert kinds(actions) == ["close"]
    stored = db.get_position(p.id)
    assert stored.status == "closed" and stored.close_reason == "ставка развернулась"


def test_drift_triggers_rebalance(db, settings):
    p = make_pos(db, spot_amount=7.5, perp_amount=7.0)  # перекос 0.5/7.5 ≈ 6.7% > 1%
    broker = Broker()
    actions, _, _ = do_guard([p], snap(), broker, db, settings)
    assert kinds(actions) == ["rebalance"]
    assert broker.calls == [("bybit", SPOT, "spot", "sell", 0.5)]
    stored = db.get_position(p.id)
    assert stored.legs[0].amount == pytest.approx(7.0) == stored.legs[1].amount
    assert stored.legs[0].fees_paid == pytest.approx(0.05)
    # перекос в пределах 1% — ничего не делаем
    q = make_pos(db, spot_amount=7.5, perp_amount=7.46)
    actions, _, _ = do_guard([q], snap(), broker, db, settings)
    assert kinds(actions) == ["ok"]


def test_error_in_one_position_does_not_stop_others(db, settings):
    class Flaky(Broker):
        async def on_liquidated(self, exchange, symbol):
            if not self.liquidated:
                self.liquidated.append((exchange, symbol))
                raise RuntimeError("сбой биржи")
            return await super().on_liquidated(exchange, symbol)

    a, b = make_pos(db), make_pos(db)
    broker = Flaky(prices={("bybit", SPOT): 140.0})
    actions, notifier, _ = do_guard([a, b], snap(140.0), broker, db, settings)
    assert [(x.position_id, x.kind) for x in actions] == [(a.id, "stale"), (b.id, "liquidated")]
    assert "ошибка проверки" in actions[0].detail
    assert db.get_position(b.id).status == "liquidated"
    events = [r["event"] for r in db.conn.execute("SELECT event FROM audit_log")]
    assert "guard_error" in events
    assert any(lvl == "alert" and "ошибка проверки" in t for t, lvl in notifier.sent)


def test_error_in_notifier_is_contained(db, settings):
    class Broken(FakeNotifier):
        async def send(self, text, level="info"):
            if "Связка X" in text:
                raise RuntimeError("нет связи")
            await super().send(text, level)

    a, b = make_pos(db), make_pos(db)
    b.base = "Y"
    db.update_position(b)
    notifier = Broken()
    out = run(guard([a, b], snap(121.0), Broker(), db, notifier, settings, {}, now_ms=NOW))
    assert [(x.position_id, x.kind) for x in out] == [(a.id, "stale"), (b.id, "warn")]
    assert "ошибка проверки" in out[0].detail
    assert [t for t, _ in notifier.sent] and all("Связка Y" in t for t, _ in notifier.sent)


def test_unpriced_spot_leg_still_tops_up_perp(db, settings):
    p = make_pos(db)
    broker = Broker()
    actions, notifier, _ = do_guard([p], snap(127.0, spot_present=False), broker, db, settings)
    assert kinds(actions) == ["stale", "topup"]
    assert len(broker.topups) == 1 and broker.calls == []
    assert any("нет цены" in t for t, _ in notifier.sent)


def test_topup_long_side(db, settings):
    legs = [Leg(None, None, "bybit", PERP, "perp", "long", 7.5, 100.0, 3.0, 250.0),
            Leg(None, None, "okx", PERP, "perp", "short", 7.5, 100.0, 3.0, 250.0)]
    p = Position(None, "perp-perp", "X", "calm", "open", 1000.0, 0, legs=legs)
    db.save_position(p)
    s = MarketSnapshot(ts=NOW, perps=[PerpInfo("bybit", "X", PERP, 0.0, 8.0, 73.0, 1e6, NOW + H),
                                      PerpInfo("okx", "X", PERP, 0.0, 8.0, 73.0, 1e6, NOW + H)])
    broker = Broker()
    # лонг: ликв. 100*(1-1/3+0.005) ≈ 67.17, запас при 73 ≈ 0.18; цель: ликв. 2*73-100 = 46
    actions, _, _ = do_guard([p], s, broker, db, settings)
    assert kinds(actions) == ["topup"]
    assert broker.topups == [("bybit", PERP, pytest.approx(158.75))]
    long = db.get_position(p.id).legs[0]
    lev = long.entry_price * long.amount / long.margin
    assert margin_buffer(100.0, 73.0, liquidation_price(100.0, "long", lev)) == pytest.approx(0.5)


def test_perp_leg_without_margin_is_stale(db, settings):
    p = make_pos(db)
    p.legs[1].margin = 0.0
    db.update_position(p)
    notifier = FakeNotifier()
    actions, _, _ = do_guard([p], snap(140.0), Broker(), db, settings, notifier)
    assert kinds(actions) == ["stale"]
    assert "маржа" in actions[0].detail and len(notifier.sent) == 1
    do_guard([p], snap(140.0), Broker(), db, settings, notifier, now=NOW + 1000)
    assert len(notifier.sent) == 1


def test_failed_rebalance_alerts_once_per_hour(db, settings):
    p = make_pos(db, spot_amount=7.5, perp_amount=7.0)
    broker = Broker(script=[0.0] * 6)
    notifier = FakeNotifier()
    actions, _, _ = do_guard([p], snap(), broker, db, settings, notifier)
    assert kinds(actions) == ["rebalance"] and len(notifier.sent) == 1
    assert notifier.sent[0][1] == "alert"
    actions, _, _ = do_guard([p], snap(), broker, db, settings, notifier, now=NOW + 30_000)
    assert kinds(actions) == ["rebalance"] and len(notifier.sent) == 1
