import pytest

from engine.collector import MarketSnapshot
from engine.db import Db
from engine.ledger import accrue, day_pnl, equity
from engine.models import Leg, Position
from scanner.calc import PerpInfo, SpotInfo

SYM = "X/USDT:USDT"
H = 3_600_000
DAY = 86_400_000


@pytest.fixture
def db():
    d = Db(":memory:")
    d.init()
    return d


def perp_leg(side="short", amount=10.0, entry=2.0, pending_rate=0.001, pending_ts=1000):
    return Leg(None, None, "bybit", SYM, "perp", side, amount, entry, 3.0, 5.0,
               pending_rate=pending_rate, pending_ts=pending_ts)


def pos(db, *legs, status="open"):
    p = Position(None, "spot-perp", "X", "calm", status, 100.0, 0, legs=list(legs))
    db.save_position(p)
    return p


def snap(rate=0.001, next_ts=1000 + 8 * H, mark=2.0, spot=None, ts=0):
    perps = [] if rate is None else [PerpInfo("bybit", "X", SYM, rate, 8.0, mark, 1e6, next_ts)]
    spots = [] if spot is None else [SpotInfo("bybit", "X", "X/USDT", spot, 1e6)]
    return MarketSnapshot(ts=ts, perps=perps, spots=spots)


def test_short_receives_positive_funding(db):
    leg = perp_leg("short")
    p = pos(db, leg)
    out = accrue(snap(), [p], db, 1000)
    assert out == [(leg, pytest.approx(0.02))]
    assert leg.funding_received == pytest.approx(0.02)
    assert leg.last_funding_ts == 1000
    assert db.funding_total() == pytest.approx(0.02)
    assert db.get_position(p.id).legs[0].funding_received == pytest.approx(0.02)


def test_long_pays_positive_funding(db):
    leg = perp_leg("long")
    out = accrue(snap(), [pos(db, leg)], db, 1000)
    assert out[0][1] == pytest.approx(-0.02)


def test_not_due_yet_and_pending_rolls_forward(db):
    leg = perp_leg()
    p = pos(db, leg)
    assert accrue(snap(), [p], db, 999) == []
    accrue(snap(rate=0.002, next_ts=9000), [p], db, 1000)
    assert (leg.pending_rate, leg.pending_ts) == (0.002, 9000)
    assert db.get_position(p.id).legs[0].pending_ts == 9000


def test_pending_none_is_filled_from_snapshot(db):
    leg = perp_leg(pending_rate=None, pending_ts=None)
    p = pos(db, leg)
    assert accrue(snap(next_ts=5000), [p], db, 10) == []
    assert (leg.pending_rate, leg.pending_ts) == (0.001, 5000)


def test_no_double_count_on_same_ts(db):
    leg = perp_leg()
    p = pos(db, leg)
    stale = snap(next_ts=1000)  # биржа ещё отдаёт ту же выплату
    assert len(accrue(stale, [p], db, 1000)) == 1
    assert accrue(stale, [p], db, 1001) == []
    assert db.funding_total() == pytest.approx(0.02)
    assert leg.funding_received == pytest.approx(0.02)


def test_interval_change_no_double_count(db):
    leg = perp_leg(pending_ts=8 * H)
    p = pos(db, leg)
    paid = []
    # 8ч-сетка -> следующая выплата через 1ч (сетка сменилась)
    paid += accrue(snap(next_ts=9 * H), [p], db, 8 * H)
    # время прыгнуло назад до уже оплаченного ts
    paid += accrue(snap(next_ts=8 * H), [p], db, 8 * H + 1)
    paid += accrue(snap(next_ts=8 * H), [p], db, 8 * H + 2)
    # теперь обычная 1ч-сетка
    paid += accrue(snap(next_ts=10 * H), [p], db, 9 * H)
    paid += accrue(snap(next_ts=11 * H), [p], db, 10 * H)
    assert [leg.last_funding_ts] == [10 * H]
    n = db.conn.execute("SELECT COUNT(*) FROM funding_payments").fetchone()[0]
    assert n == len(paid) == 3
    assert leg.funding_received == pytest.approx(0.06)


def test_missing_next_ts_skips(db):
    leg = perp_leg()
    p = pos(db, leg)
    assert accrue(snap(next_ts=None), [p], db, 5000) == []
    assert (leg.pending_rate, leg.pending_ts) == (0.001, 1000)
    assert db.funding_total() == 0


def test_missing_perp_in_snapshot_skips(db):
    leg = perp_leg()
    assert accrue(snap(rate=None), [pos(db, leg)], db, 5000) == []
    assert db.funding_total() == 0


def test_mark_falls_back_to_entry(db):
    leg = perp_leg(entry=3.0)
    out = accrue(snap(mark=None), [pos(db, leg)], db, 1000)
    assert out[0][1] == pytest.approx(0.03)


def test_only_open_perp_legs_of_open_or_closing(db):
    spot = Leg(None, None, "bybit", "X/USDT", "spot", "long", 10.0, 2.0, 1.0, 0.0,
               pending_rate=0.001, pending_ts=1000)
    closed_leg = perp_leg()
    closed_leg.open = False
    ok = perp_leg()
    p_open = pos(db, spot, closed_leg, ok)
    p_closing = pos(db, perp_leg(), status="closing")
    p_closed = pos(db, perp_leg(), status="closed")
    p_opening = pos(db, perp_leg(), status="opening")
    out = accrue(snap(), [p_open, p_closing, p_closed, p_opening], db, 1000)
    assert len(out) == 2
    assert {id(l) for l, _ in out} == {id(ok), id(p_closing.legs[0])}


def test_equity_balances_spot_and_perp(db):
    spot = Leg(None, None, "bybit", "X/USDT", "spot", "long", 10.0, 2.0, 1.0, 0.0)
    short = perp_leg("short", entry=2.0)
    p = pos(db, spot, short)
    # spot 10*2.5=25 ; perp margin 5 + (2.0-2.5)*10=-5 -> 0 ; balances 70
    e = equity(db, {"bybit": 50.0, "okx": 20.0}, [p], snap(mark=2.5, spot=2.5))
    assert e == pytest.approx(95.0)


def test_equity_long_and_fallbacks(db):
    spot = Leg(None, None, "bybit", "X/USDT", "spot", "long", 10.0, 2.0, 1.0, 0.0)
    long_ = perp_leg("long", entry=2.0)
    closed = perp_leg("long")
    closed.open = False
    p = pos(db, spot, long_, closed)
    # no spot / perp in snapshot: spot at entry 20, perp margin 5 + 0
    assert equity(db, {"bybit": 10.0}, [p], snap(rate=None)) == pytest.approx(35.0)
    # long gains: (3-2)*10 = 10
    assert equity(db, {}, [pos(db, perp_leg("long"))], snap(mark=3.0)) == pytest.approx(15.0)


def test_day_pnl_relative_to_day_start(db):
    d0 = 20_000 * DAY + 5 * H
    assert day_pnl(db, d0, 100.0) == 0.0
    assert db.get_kv("day_start_equity") == {"day": "2024-10-04", "equity": 100.0}
    assert day_pnl(db, d0 + H, 103.5) == pytest.approx(3.5)
    assert day_pnl(db, d0 + 2 * H, 98.0) == pytest.approx(-2.0)
    assert day_pnl(db, d0 + DAY, 110.0) == 0.0  # новые сутки UTC
    assert day_pnl(db, d0 + DAY + H, 111.0) == pytest.approx(1.0)
