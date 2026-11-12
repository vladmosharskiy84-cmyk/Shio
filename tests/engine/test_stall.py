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


def test_limit_is_five_minutes():
    assert STALL_LIMIT_S == 300


def test_fires_after_limit(caplog):
    clock, exits = Clock(), []
    wd = StallWatchdog(clock=clock, exit_fn=exits.append, check_every_s=0.005)
    with caplog.at_level(logging.CRITICAL):
        wd.start()
        try:
            clock.t += 299
            time.sleep(0.05)
            assert exits == []
            clock.t += 2
            assert _wait_for(lambda: exits)
        finally:
            wd.stop()
    assert exits == [1]
    assert any("Движок завис 301 с — перезапуск" in r.getMessage() for r in caplog.records)


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
    assert len(feeds) == 3


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
