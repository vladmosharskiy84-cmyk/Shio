import pytest

from engine.db import Db
from engine.models import Command, Leg, Position, Proposal


def make_position() -> Position:
    legs = [
        Leg(None, None, "bybit", "BTC/USDT", "spot", "long", 0.1, 50000.0, 1.0, 5000.0),
        Leg(None, None, "bybit", "BTC/USDT:USDT", "perp", "short", 0.1, 50010.0, 2.0, 2500.0,
            fees_paid=1.5, last_funding_ts=1000, pending_rate=0.0001, pending_ts=2000),
    ]
    return Position(None, "spot-perp", "BTC", "calm", "open", 7500.0, 123456, legs=legs)


def make_proposal(created=1000, expires=2000) -> Proposal:
    return Proposal(None, created, expires, "ETH", "spot-perp", "calm",
                    [{"exchange": "okx", "note": "тест"}], 1000.0, 35.0, 12.0, "pending")


@pytest.fixture
def db():
    d = Db(":memory:")
    d.init()
    return d


def test_position_roundtrip_with_legs(db):
    p = make_position()
    pid = db.save_position(p)
    assert p.id == pid
    assert all(l.id is not None and l.position_id == pid for l in p.legs)
    got = db.get_position(pid)
    assert got == p
    assert got.legs[1].open is True and got.legs[1].pending_rate == 0.0001
    got.status = "closed"
    got.legs[0].open = False
    got.legs[0].funding_received = 3.0
    db.update_position(got)
    again = db.get_position(pid)
    assert again.status == "closed" and again.legs[0].open is False
    assert again.legs[0].funding_received == 3.0
    assert [x.status for x in db.positions({"closed"})] == ["closed"]
    assert db.positions({"open"}) == []
    assert len(db.positions()) == 1


def test_wal_enabled(tmp_path):
    d = Db(str(tmp_path / "sub" / "shio.db"))
    d.init()
    assert d.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_idempotent(db):
    db.init()
    db.init()


def test_commands_taken_once(db):
    cid = db.add_command("approve", "5")
    db.add_command("pause")
    first = db.take_new_commands()
    assert [c.type for c in first] == ["approve", "pause"]
    assert first[0] == Command(cid, first[0].created_at, "approve", "5", "taken", None)
    assert db.take_new_commands() == []
    db.finish_command(cid, "done", "ok")
    row = db.conn.execute("SELECT status, result FROM commands WHERE id=?", (cid,)).fetchone()
    assert tuple(row) == ("done", "ok")


def test_funding_unique_per_leg_and_ts(db):
    p = make_position()
    db.save_position(p)
    lid = p.legs[1].id
    db.add_funding(lid, 100, 0.0001, 0.75)
    db.add_funding(lid, 100, 0.0001, 0.75)
    assert db.funding_total() == pytest.approx(0.75)
    db.add_funding(lid, 200, 0.0001, 0.25)
    assert db.funding_total() == pytest.approx(1.0)
    assert db.funding_total(since_ms=150) == pytest.approx(0.25)


def test_pending_proposals_excludes_expired(db):
    live = make_proposal(1000, 5000)
    dead = make_proposal(1000, 2000)
    done = make_proposal(1000, 5000)
    done.status = "approved"
    for x in (live, dead, done):
        x.id = db.save_proposal(x)
    got = db.pending_proposals(3000)
    assert [x.id for x in got] == [live.id]
    assert got[0].legs_plan == [{"exchange": "okx", "note": "тест"}]
    live.status = "executed"
    db.update_proposal(live)
    assert db.pending_proposals(3000) == []


def test_kv_audit_heartbeat(db):
    assert db.get_kv("x", 5) == 5
    db.set_kv("x", {"a": [1, 2]})
    assert db.get_kv("x") == {"a": [1, 2]}
    db.audit("событие", {"msg": "привет"})
    row = db.conn.execute("SELECT event, detail FROM audit_log").fetchone()
    assert row[0] == "событие" and "привет" in row[1]
    db.heartbeat(42)
    assert db.get_kv("heartbeat") == 42


def test_fail_taken_commands():
    from engine.db import Db as _Db
    d = _Db(":memory:")
    d.init()
    a = d.add_command("pause")
    d.take_new_commands()
    b = d.add_command("resume")
    assert d.fail_taken_commands("бот перезапущен") == 1
    rows = {r["id"]: (r["status"], r["result"]) for r in d.conn.execute("SELECT * FROM commands")}
    assert rows[a] == ("error", "бот перезапущен")
    assert rows[b] == ("new", None)
