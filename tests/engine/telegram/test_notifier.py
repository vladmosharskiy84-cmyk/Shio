import asyncio
import json
import logging
import time

import pytest

from engine.db import Db
from engine.models import Proposal
from engine.telegram.api import Button
from engine.telegram.notifier import TelegramNotifier
from tests.engine.telegram.fakes import FakeTelegramApi

CHAT = 42


_MADE: list = []  # нотификаторы теста: run() дожидается их очередей


def run(coro):
    async def go():
        result = await coro
        for n in _MADE:
            assert await n.flush(timeout=5)
        return result
    return asyncio.run(go())


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


def make(tmp_path, api=None, dedupe_s=600, retries=3, **kw):
    db = Db(str(tmp_path / "t.db"))
    db.init()
    api = api or FakeTelegramApi()
    clock = Clock()
    kw.setdefault("breaker_s", 0)
    n = TelegramNotifier(api, CHAT, db, dedupe_s, clock=clock, retries=retries, retry_delay_s=0,
                         backup_dir=tmp_path / "backups", **kw)
    _MADE.clear()
    _MADE.append(n)
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
    assert run(n.send_document("b.bin", b"data", "копия")) is True
    assert api.documents == [{"chat_id": CHAT, "filename": "b.bin", "data": b"data",
                              "caption": "копия"}]


def test_send_document_gives_up_without_raising(tmp_path):
    n, api, db, _ = make(tmp_path, api=FakeTelegramApi(fail_times=9))
    assert run(n.send_document("b.bin", b"data", "копия")) is False
    assert api.attempts == 3 and api.documents == []
    assert len(audits(db, "telegram_error")) == 1
    assert (n.backup_dir / "b.bin").read_bytes() == b"data"  # запасная копия на диске


def test_send_with_buttons_puts_them_on_last_part(tmp_path):
    n, api, _, _ = make(tmp_path)
    kb = [[Button("Закрыть", "close:ask:3")]]
    run(n.send("ставка развернулась", "warn", buttons=kb))
    assert api.messages[-1]["buttons"] == kb
    long = "\n".join("y" * 100 for _ in range(100))
    run(n.send(long, "info", buttons=kb))
    tail = [m["buttons"] for m in api.messages[1:]]
    assert tail[-1] == kb and all(b is None for b in tail[:-1])


def test_log_notifier_accepts_buttons(tmp_path):
    from engine.notifier import LogNotifier
    db = Db(str(tmp_path / "l.db"))
    db.init()
    run(LogNotifier(db).send("x", "warn", buttons=[[Button("Закрыть", "close:ask:1")]]))
    assert len(audits(db, "notify")) == 1


class HangingApi(FakeTelegramApi):
    """Telegram «висит»: каждый вызов ждёт час."""

    async def send_message(self, chat_id, text, buttons=None):
        self.attempts += 1
        await asyncio.sleep(3600)

    async def send_document(self, chat_id, filename, data, caption):
        self.attempts += 1
        await asyncio.sleep(3600)


def test_send_returns_immediately_when_telegram_hangs(tmp_path):
    n, api, db, _ = make(tmp_path, api=HangingApi())
    _MADE.clear()

    async def go():
        t0 = time.monotonic()
        for i in range(5):
            await asyncio.wait_for(n.send(f"тревога {i}", "alert"), 0.5)
        p = Proposal(7, 0, 900_000, "HOT", "perp_perp", "calm", [], 100.0, 50.0, 1.0, "pending")
        await asyncio.wait_for(n.proposal(p, 15, 3.0), 0.5)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0.05)  # разборщик очереди успел начать «висящий» вызов
        await n.aclose(timeout=0.1)  # остановка не зависает
        return elapsed

    assert run(go()) < 0.5
    assert api.attempts >= 1
    assert len(audits(db, "notify")) == 6  # журнал записан сразу


def test_send_document_bounded_when_telegram_hangs(tmp_path):
    n, api, _, _ = make(tmp_path, api=HangingApi(), document_wait_s=0.1)
    _MADE.clear()

    async def go():
        t0 = time.monotonic()
        ok = await n.send_document("b.bin", b"data", "копия")
        elapsed = time.monotonic() - t0
        await n.aclose(timeout=0.1)
        return ok, elapsed

    ok, elapsed = run(go())
    assert ok is False and elapsed < 1.0
    assert (n.backup_dir / "b.bin").read_bytes() == b"data"  # при остановке — копия на диске


def test_runner_step_not_blocked_by_hanging_telegram(tmp_path):
    from engine.config import load_settings
    from engine.runner import Runner
    from tests.engine.test_runner import Clock as RClock, make_cfg, make_exchange
    from engine.broker import PaperBroker
    from engine.collector import Collector
    from tests.engine.fakes import FakeClientFactory

    settings = load_settings(make_cfg(tmp_path))
    db = Db(settings.db_path)
    db.init()
    collector = Collector(settings, FakeClientFactory(make_exchange(
        {"bybit": {"HOT": (0.001, "8h", 100.0)}})))
    n = TelegramNotifier(HangingApi(), CHAT, db, 600, backup_dir=tmp_path / "b")
    runner = Runner(settings, collector, PaperBroker(collector, settings, db), db, n,
                    clock=RClock())
    db.add_command("pause")
    db.add_command("resume")

    async def go():
        t0 = time.monotonic()
        await asyncio.wait_for(runner.step(), 5)  # всё расписание: сбор, команды, охрана...
        elapsed = time.monotonic() - t0
        await n.aclose(timeout=0.1)
        await collector.close()
        return elapsed

    _MADE.clear()
    assert run(go()) < 2.0
    statuses = [r["status"] for r in db.conn.execute("SELECT status FROM commands")]
    assert statuses == ["done", "done"]


def test_queue_overflow_drops_oldest_info_first(tmp_path, caplog):
    n, api, _, _ = make(tmp_path, queue_max=3)

    async def go():
        # без await-пауз между отправками разборщик очереди не успевает ничего взять
        await n.send("a", "info")
        await n.send("b", "warn")
        await n.send("c", "info")
        await n.send("d", "alert")   # переполнение: выброшено «a» (самое старое info)
        await n.send("e", "info")    # выброшено «c»
        await n.send("f", "alert")   # выброшено «e»
        await n.send("g", "alert")   # info нет — выброшено самое старое: «b»

    with caplog.at_level(logging.WARNING):
        run(go())
    assert [m["text"] for m in api.messages] == ["🚨 d", "🚨 f", "🚨 g"]
    assert any("переполнена" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)


def test_breaker_pauses_delivery_after_failure(tmp_path):
    n, api, _, _ = make(tmp_path, api=FakeTelegramApi(fail_times=3), breaker_s=0.3)
    _MADE.clear()

    async def go():
        await n.send("a", "alert")
        await n.send("b", "alert")
        await asyncio.sleep(0.15)
        during = (api.attempts, len(api.messages))
        assert await n.flush(timeout=2)
        return during

    during = run(go())
    assert during == (3, 0)  # «a» исчерпал повторы, «b» ждёт окончания паузы
    assert [m["text"] for m in api.messages] == ["🚨 b"]


class RetryAfter(Exception):
    def __init__(self, retry_after):
        super().__init__(f"Flood control exceeded. Retry in {retry_after} seconds")
        self.retry_after = retry_after


@pytest.mark.parametrize("retry_after,expected", [(100, 30.0), (2, 2.0)])
def test_retry_after_respected_and_capped(tmp_path, retry_after, expected):
    class Flood(FakeTelegramApi):
        async def send_message(self, chat_id, text, buttons=None):
            if self.attempts == 0:
                self.attempts += 1
                raise RetryAfter(retry_after)
            return await super().send_message(chat_id, text, buttons)

    n, api, _, _ = make(tmp_path, api=Flood())
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    n._sleep = fake_sleep
    run(n.send("x", "info"))
    assert sleeps == [expected]
    assert [m["text"] for m in api.messages] == ["x"]


def test_level_warning_is_warn(tmp_path):
    n, api, db, _ = make(tmp_path)
    run(n.send("w", "warning"))
    assert [m["text"] for m in api.messages] == ["⚠️ w"]
    assert json.loads(audits(db, "notify")[0]["detail"])["level"] == "warn"
    run(n.send("w", "warn"))  # то же сообщение — подавлено
    assert len(api.messages) == 1


def test_token_redacted_from_errors(tmp_path, caplog):
    class Leaky(FakeTelegramApi):
        async def send_message(self, chat_id, text, buttons=None):
            self.attempts += 1
            raise ConnectionError(
                "Cannot connect to https://api.telegram.org/bot123456:AAH-x_yZ/sendMessage")

    n, api, db, _ = make(tmp_path, api=Leaky())
    with caplog.at_level(logging.ERROR):
        run(n.send("x", "alert"))
    detail = audits(db, "telegram_error")[0]["detail"]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for text in (detail, logged):
        assert "123456:AAH" not in text and "bot<токен скрыт>" in text


def test_proposal_written_to_python_log(tmp_path, caplog):
    n, api, _, _ = make(tmp_path)
    p = Proposal(7, 0, 900_000, "HOT", "perp_perp", "calm", [], 100.0, 50.0, 1.0, "pending")
    with caplog.at_level(logging.INFO):
        run(n.proposal(p, 15, 3.0))
    assert any("Предложение #7" in r.getMessage() for r in caplog.records)


def test_document_in_flight_not_sent_twice(tmp_path):
    class Slow(FakeTelegramApi):
        async def send_document(self, chat_id, filename, data, caption):
            await asyncio.sleep(0.3)
            await super().send_document(chat_id, filename, data, caption)

    n, api, _, _ = make(tmp_path, api=Slow(), document_wait_s=0.05)

    async def go():
        first = await n.send_document("b.bin", b"data", "копия")
        second = await n.send_document("b.bin", b"data2", "копия")  # отправка ещё идёт
        await asyncio.sleep(0.4)
        third = await n.send_document("b.bin", b"data3", "копия")   # итог уже известен
        return first, second, third

    assert run(go()) == (False, False, True)
    assert [d["data"] for d in api.documents] == [b"data"]


def test_aclose_closes_api_and_logs_undelivered(tmp_path, caplog):
    class Closable(FakeTelegramApi):
        closed = False

        async def close(self):
            self.closed = True

    n, api, _, _ = make(tmp_path, api=Closable(fail_times=99), breaker_s=60)
    _MADE.clear()

    async def go():
        await n.send("a", "alert")
        await n.send("b", "alert")
        await asyncio.sleep(0.05)
        await n.aclose(timeout=0.1)

    with caplog.at_level(logging.WARNING):
        run(go())
    assert api.closed
    assert any("Не доставлено" in r.getMessage() and "b" in r.getMessage()
               for r in caplog.records)
