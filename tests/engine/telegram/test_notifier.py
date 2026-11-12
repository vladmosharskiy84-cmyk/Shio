import asyncio

from engine.db import Db
from engine.models import Proposal
from engine.telegram.api import Button
from engine.telegram.notifier import TelegramNotifier
from tests.engine.telegram.fakes import FakeTelegramApi

CHAT = 42


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


def make(tmp_path, api=None, dedupe_s=600, retries=3):
    db = Db(str(tmp_path / "t.db"))
    db.init()
    api = api or FakeTelegramApi()
    clock = Clock()
    n = TelegramNotifier(api, CHAT, db, dedupe_s, clock=clock, retries=retries, retry_delay_s=0)
    return n, api, db, clock


def audits(db, event):
    return [r for r in db.conn.execute("SELECT * FROM audit_log WHERE event=?", (event,))]


def test_levels_prefix(tmp_path):
    n, api, db, _ = make(tmp_path)
    run(n.send("a", "info"))
    run(n.send("b", "warn"))
    run(n.send("c", "alert"))
    assert [m["text"] for m in api.messages] == ["a", "⚠️ b", "🚨 c"]
    assert all(m["chat_id"] == CHAT for m in api.messages)
    assert len(audits(db, "notify")) == 3


def test_long_text_split(tmp_path):
    n, api, _, _ = make(tmp_path)
    text = "\n".join(f"строка {i} " + "x" * 90 for i in range(200))
    run(n.send(text, "info"))
    assert len(api.messages) > 1
    assert all(len(m["text"]) <= 4096 for m in api.messages)
    assert "\n".join(m["text"] for m in api.messages) == text


def test_dedupe_same_text_within_window(tmp_path):
    n, api, db, clock = make(tmp_path, dedupe_s=600)
    run(n.send("беда", "alert"))
    clock.t += 300
    run(n.send("беда", "alert"))
    assert len(api.messages) == 1
    assert len(audits(db, "notify")) == 2
    clock.t += 660  # 11 минут от первой отправки
    run(n.send("беда", "alert"))
    assert len(api.messages) == 2
    run(n.send("беда", "warn"))  # другой уровень — другое сообщение
    assert len(api.messages) == 3


def test_retries_then_gives_up_without_raising(tmp_path):
    n, api, db, _ = make(tmp_path, api=FakeTelegramApi(fail_times=5))
    run(n.send("x", "alert"))
    assert api.attempts == 3 and api.messages == []
    assert len(audits(db, "telegram_error")) == 1
    assert len(audits(db, "notify")) == 1


def test_recovers_after_one_failure(tmp_path):
    n, api, db, _ = make(tmp_path, api=FakeTelegramApi(fail_times=1))
    run(n.send("x", "info"))
    assert api.attempts == 2 and len(api.messages) == 1
    assert audits(db, "telegram_error") == []


def test_failed_send_is_not_deduped(tmp_path):
    n, api, _, _ = make(tmp_path, api=FakeTelegramApi(fail_times=3))
    run(n.send("x", "alert"))
    run(n.send("x", "alert"))
    assert len(api.messages) == 1


def test_proposal_has_buttons_and_stores_message_id(tmp_path):
    n, api, db, _ = make(tmp_path)
    p = Proposal(7, 0, 900_000, "HOT", "perp_perp", "calm", [], 100.0, 50.0, 1.0, "pending")
    run(n.proposal(p, 15, 3.0))
    m = api.messages[0]
    assert "#7" in m["text"] and "HOT" in m["text"] and "python -m engine" not in m["text"]
    assert m["buttons"] == [[Button("Открыть", "approve:7"), Button("Пропустить", "reject:7")]]
    assert db.get_kv("tg_msg:proposal:7") == m["message_id"]


def test_proposal_failure_does_not_raise(tmp_path):
    n, api, db, _ = make(tmp_path, api=FakeTelegramApi(fail_times=9))
    p = Proposal(7, 0, 900_000, "HOT", "perp_perp", "calm", [], 100.0, 50.0, 1.0, "pending")
    run(n.proposal(p, 15, 3.0))
    assert db.get_kv("tg_msg:proposal:7") is None
    assert len(audits(db, "telegram_error")) == 1


def test_send_document_retries_and_never_raises(tmp_path):
    n, api, db, _ = make(tmp_path, api=FakeTelegramApi(fail_times=1))
    run(n.send_document("b.bin", b"data", "копия"))
    assert api.documents == [{"chat_id": CHAT, "filename": "b.bin", "data": b"data",
                              "caption": "копия"}]


def test_send_document_gives_up_without_raising(tmp_path):
    n, api, db, _ = make(tmp_path, api=FakeTelegramApi(fail_times=9))
    run(n.send_document("b.bin", b"data", "копия"))
    assert api.attempts == 3 and api.documents == []
    assert len(audits(db, "telegram_error")) == 1
