import asyncio

from engine.db import Db
from engine.launchpool import (Announcement, BybitAnnouncements, OkxAnnouncements,
                               check_announcements)
from tests.engine.fakes import FakeNotifier


def run(coro):
    return asyncio.run(coro)


class FakeSource:
    def __init__(self, items=None, error=None, exchange="fake"):
        self.exchange = exchange
        self.items = items or []
        self.error = error

    async def fetch(self):
        if self.error:
            raise self.error
        return list(self.items)


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        return self.payload


def ann(i, title="Bybit Launchpool: FOO", ex="bybit"):
    return Announcement(ex, f"id{i}", title, f"https://x/{i}", 1000 + i)


def make_db(tmp_path):
    db = Db(str(tmp_path / "t.db"))
    db.init()
    return db


def audits(db, event):
    return db.conn.execute("SELECT detail FROM audit_log WHERE event=?", (event,)).fetchall()


def test_filters_keywords(tmp_path):
    db, n = make_db(tmp_path), FakeNotifier()
    run(check_announcements([FakeSource([])], db, n, 0))  # первый запуск
    items = [ann(1, "New LAUNCHPOOL: A"), ann(2, "Bybit Launchpad BAR"), ann(3, "Jumpstart X"),
             ann(4, "OKX Megadrop Y"), ann(5, "Listing of ZZZ"), ann(6, "Maintenance")]
    got = run(check_announcements([FakeSource(items)], db, n, 1))
    assert [a.id for a in got] == ["id1", "id2", "id3", "id4"]
    assert len(n.sent) == 4


def test_first_run_marks_seen_without_notify(tmp_path):
    db, n = make_db(tmp_path), FakeNotifier()
    src = FakeSource([ann(1), ann(2, "other")])
    assert run(check_announcements([src], db, n, 0)) == []
    assert n.sent == []
    assert db.get_kv("lp_seen") == {"ids": ["id1"], "sources": ["fake"]}
    assert run(check_announcements([src], db, n, 1)) == []
    assert n.sent == []


def test_new_item_notified_once(tmp_path):
    db, n = make_db(tmp_path), FakeNotifier()
    src = FakeSource([ann(1)])
    run(check_announcements([src], db, n, 0))
    src.items.append(ann(2, "Launchpool: NEW", ex="okx"))
    got = run(check_announcements([src], db, n, 1))
    assert [a.id for a in got] == ["id2"]
    assert n.sent == [("Новый пул на okx: Launchpool: NEW\nhttps://x/2", "info")]
    assert run(check_announcements([src], db, n, 2)) == []
    assert len(n.sent) == 1


def test_source_error_isolated(tmp_path):
    db, n = make_db(tmp_path), FakeNotifier()
    bad, good = FakeSource(error=RuntimeError("boom"), exchange="bad"), FakeSource([])
    run(check_announcements([bad, good], db, n, 0))
    good.items.append(ann(1))
    bad.exchange = "bad"
    got = run(check_announcements([bad, good], db, n, 1))
    assert [a.id for a in got] == ["id1"]
    assert len(audits(db, "launchpool_error")) == 1  # не чаще раза в час
    run(check_announcements([bad, good], db, n, 3_600_001))
    assert len(audits(db, "launchpool_error")) == 2
    assert len(n.sent) == 1


def test_seen_list_capped_at_500(tmp_path):
    db, n = make_db(tmp_path), FakeNotifier()
    items = [ann(i) for i in range(600)]
    run(check_announcements([FakeSource(items)], db, n, 0))
    ids = db.get_kv("lp_seen")["ids"]
    assert len(ids) == 500
    assert ids[-1] == "id599"
    assert ids[0] == "id100"


def test_bybit_and_okx_parsing_defensive():
    http = FakeHttp({"result": {"list": [
        {"title": "Launchpool A", "url": "https://b/1", "publishTime": 123},
        {"title": "no url"}, "junk", {"title": "T", "url": "https://b/2", "publishTime": "45"}]}})
    got = run(BybitAnnouncements(http).fetch())
    assert [(a.id, a.published_ms) for a in got] == [("https://b/1", 123), ("https://b/2", 45)]
    assert got[0].exchange == "bybit"
    assert "api.bybit.com/v5/announcements/index" in http.urls[0]
    http = FakeHttp({"data": [{"details": [
        {"title": "Jumpstart", "url": "https://o/1", "pTime": "99"}, {"url": "x"}]}, "junk"]})
    got = run(OkxAnnouncements(http).fetch())
    assert [(a.exchange, a.id, a.published_ms) for a in got] == [("okx", "https://o/1", 99)]
    assert run(OkxAnnouncements(FakeHttp({})).fetch()) == []
    assert run(BybitAnnouncements(FakeHttp([])).fetch()) == []


def test_first_fetch_failure_keeps_first_run_protection(tmp_path):
    db, n = make_db(tmp_path), FakeNotifier()
    bad = FakeSource(error=RuntimeError("x"))
    run(check_announcements([bad], db, n, 0))
    bad.error = None
    bad.items = [ann(1), ann(2)]
    assert run(check_announcements([bad], db, n, 1)) == []
    assert n.sent == []
    bad.items.append(ann(3))
    assert [a.id for a in run(check_announcements([bad], db, n, 2))] == ["id3"]


def test_late_source_is_baselined_separately(tmp_path):
    db, n = make_db(tmp_path), FakeNotifier()
    a = FakeSource([ann(1)], exchange="bybit")
    b = FakeSource(error=RuntimeError("x"), exchange="okx")
    run(check_announcements([a, b], db, n, 0))
    b.error = None
    b.items = [ann(10, ex="okx")]
    a.items.append(ann(2))
    got = run(check_announcements([a, b], db, n, 1))
    assert [x.id for x in got] == ["id2"]
    b.items.append(ann(11, ex="okx"))
    assert [x.id for x in run(check_announcements([a, b], db, n, 2))] == ["id11"]


def test_non_dict_seen_state_treated_as_first_run(tmp_path):
    db, n = make_db(tmp_path), FakeNotifier()
    db.set_kv("lp_seen", ["junk"])
    assert run(check_announcements([FakeSource([ann(1)])], db, n, 0)) == []
    assert n.sent == []


def test_build_announcement_sources(caplog):
    import logging

    from engine.__main__ import _build_announcement_sources
    from engine.config import LaunchpoolCfg

    class S:
        launchpool = LaunchpoolCfg(exchanges=["bybit", "okx", "nope"])

    with caplog.at_level(logging.WARNING, logger="engine"):
        srcs = _build_announcement_sources(S, object())
    assert [type(x) for x in srcs] == [BybitAnnouncements, OkxAnnouncements]
    assert len([r for r in caplog.records if "nope" in r.getMessage()]) == 1
