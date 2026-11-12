"""Сторож зависания: движок сам перезапускается, если цикл встал."""
import asyncio
import logging
import time

from engine import stall
from engine.stall import STALL_LIMIT_S, StallWatchdog


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_limit_is_ten_minutes():
    assert STALL_LIMIT_S == 600


def test_runner_feeds_after_each_job(tmp_path):
    """Кормим после каждой задачи шага: долгий шаг из нескольких медленных задач не считается зависанием."""
    from tests.engine.test_runner import Env, make_cfg

    env = Env(make_cfg(tmp_path))
    feeds = []

    class FakeStall:
        def feed(self):
            feeds.append(1)

    env.runner.stall = FakeStall()
    asyncio.run(env.runner.step())
    assert len(feeds) >= len(env.runner._jobs)


def test_fires_after_limit(caplog):
    clock, exits = Clock(), []
    wd = StallWatchdog(clock=clock, exit_fn=exits.append, check_every_s=0.005)
    with caplog.at_level(logging.CRITICAL):
        wd.start()
        try:
            clock.t += 599
            time.sleep(0.05)
            assert exits == []
            clock.t += 2
            assert _wait_for(lambda: exits)
        finally:
            wd.stop()
    assert exits == [1]
    assert any("Движок завис 601 с — перезапуск" in r.getMessage() for r in caplog.records)


def test_does_not_fire_when_fed():
    clock, exits = Clock(), []
    wd = StallWatchdog(clock=clock, exit_fn=exits.append, check_every_s=0.005)
    wd.start()
    try:
        for _ in range(5):
            clock.t += 200
            wd.feed()
            time.sleep(0.02)
    finally:
        wd.stop()
    assert exits == []


def test_check_is_deterministic():
    clock, exits = Clock(), []
    wd = StallWatchdog(limit_s=10, clock=clock, exit_fn=exits.append)
    wd.feed()
    clock.t += 9
    assert wd.check() is False
    wd.feed()
    clock.t += 9
    assert wd.check() is False and exits == []
    clock.t += 1
    assert wd.check() is True and exits == [1]


def test_stops_cleanly():
    clock, exits = Clock(), []
    wd = StallWatchdog(clock=clock, exit_fn=exits.append, check_every_s=0.005)
    wd.start()
    wd.stop()
    assert not wd.running
    clock.t += 10_000
    time.sleep(0.05)
    assert exits == []
    wd.stop()  # повторная остановка безопасна


def test_default_exit_is_hard_exit():
    import os
    assert StallWatchdog().exit_fn is os._exit


def test_runner_feeds_after_each_step(tmp_path):
    from tests.engine.test_runner import Env, make_cfg

    env = Env(make_cfg(tmp_path))
    feeds = []

    class FakeStall:
        def feed(self):
            feeds.append(env.clock.t)

    env.runner.stall = FakeStall()
    env.runner.loop_sleep_s = 0
    steps = {"n": 0}
    real_step = env.runner.step

    async def counted_step():
        await real_step()
        steps["n"] += 1
        env.clock.t += 1
        if steps["n"] == 3:
            env.runner.stop()

    env.runner.step = counted_step
    try:
        asyncio.run(asyncio.wait_for(env.runner.run_forever(), 5))
    finally:
        asyncio.run(env.collector.close())
    assert len(feeds) >= 3  # после каждой задачи и после каждого шага


def test_run_starts_and_stops_watchdog(tmp_path, monkeypatch):
    from engine.__main__ import _run
    from engine.config import load_settings
    from engine.notifier import LogNotifier
    from engine.telegram import bot as tgbot
    from tests.engine.test_runner import make_cfg

    settings = load_settings(make_cfg(tmp_path))
    events = []

    class FakeStall:
        def __init__(self, *a, **k):
            events.append("init")

        def start(self):
            events.append("start")

        def feed(self):
            pass

        def stop(self):
            events.append("stop")

    class FakeRunner:
        def __init__(self, *a, **k):
            events.append(("runner", k.get("stall")))

        async def run_forever(self):
            events.append("run")

        def stop(self):
            pass

    monkeypatch.setattr(stall, "StallWatchdog", FakeStall)
    monkeypatch.setattr("engine.runner.Runner", FakeRunner)
    monkeypatch.setattr(tgbot, "build_notifier", lambda s, db: (LogNotifier(db), None))
    asyncio.run(_run(settings))
    assert events[0] == "init" and "start" in events and events[-1] == "stop"
    runner_evt = next(e for e in events if isinstance(e, tuple))
    assert isinstance(runner_evt[1], FakeStall)
    assert events.index("start") < events.index("run") < events.index("stop")


def _run_steps(env, n=1):
    env.runner.loop_sleep_s = 0
    real_step = env.runner.step
    steps = {"n": 0}

    async def counted_step():
        await real_step()
        steps["n"] += 1
        env.clock.t += 1
        if steps["n"] == n:
            env.runner.stop()

    env.runner.step = counted_step
    try:
        asyncio.run(asyncio.wait_for(env.runner.run_forever(), 5))
    finally:
        asyncio.run(env.collector.close())


def _crash_warnings(env):
    return [(t, lvl) for t, lvl in env.notifier.sent if "перезапущен после сбоя" in t]


def test_restart_after_crash_warns(tmp_path):
    from tests.engine.test_runner import T0, Env, make_cfg

    env = Env(make_cfg(tmp_path))
    env.db.set_kv("heartbeat", int((T0 - 3600 + 5 * 60 + 7) * 1000))
    _run_steps(env)
    from datetime import datetime, timezone
    hhmm = datetime.fromtimestamp(T0 - 3600 + 5 * 60 + 7, tz=timezone.utc).strftime("%H:%M")
    warns = _crash_warnings(env)
    assert warns == [(f"⚠️ Движок перезапущен после сбоя (последний сигнал «жив» был в "
                      f"{hhmm} UTC). Позиции проверяются.", "warn")]


def test_clean_start_no_crash_warning(tmp_path):
    from tests.engine.test_runner import Env, make_cfg

    env = Env(make_cfg(tmp_path))
    _run_steps(env)
    assert _crash_warnings(env) == []


def test_crash_warning_failure_does_not_stop_engine(tmp_path):
    from tests.engine.test_runner import T0, Env, make_cfg

    env = Env(make_cfg(tmp_path))
    env.db.set_kv("heartbeat", int((T0 - 600) * 1000))
    real_send = env.notifier.send

    async def failing_send(text, level="info", buttons=None):
        if "перезапущен после сбоя" in text:
            raise ConnectionError("telegram недоступен")
        await real_send(text, level, buttons)

    env.notifier.send = failing_send
    _run_steps(env, 2)  # дошёл до шагов и остановился штатно
    assert env.db.get_kv("heartbeat") is None


def test_corrupt_heartbeat_value_does_not_stop_engine(tmp_path):
    from tests.engine.test_runner import Env, make_cfg

    env = Env(make_cfg(tmp_path))
    env.db.set_kv("heartbeat", "мусор")
    _run_steps(env)
    assert len(_crash_warnings(env)) <= 1
