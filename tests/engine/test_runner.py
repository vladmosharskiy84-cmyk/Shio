import asyncio
import copy
import logging

import pytest
import yaml

from engine.__main__ import build_status, format_status, main
from engine.broker import PaperBroker
from engine.collector import Collector
from engine.config import load_settings
from engine.db import Db
from engine.models import Leg, Position
from engine.notifier import LogNotifier
from engine.runner import Runner
from tests.engine.fakes import FakeClientFactory, FakeExchange, FakeNotifier

T0 = 1_800_000_000.0  # секунды
H_MS = 3_600_000
HOT_PERP = "HOT/USDT:USDT"


def run(coro):
    return asyncio.run(coro)


def make_exchange(data):
    class Ex(FakeExchange):
        DATA = copy.deepcopy(data)
        BOOKS: dict = {}
        NEXT_TS = int(T0 * 1000) + H_MS

        async def fetch_funding_rates(self):
            out = await super().fetch_funding_rates()
            for v in out.values():
                v["fundingTimestamp"] = type(self).NEXT_TS
            return out

    return Ex


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_cfg(tmp_path, default_balances=False):
    """default_balances: капитал 500 и балансы 170/170/160 из config.yaml.

    Данные есть только у bybit (у okx и hyperliquid подделка отдаёт пустой снимок).
    """
    with open("config.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    for name, ex in raw["exchanges"].items():
        ex["enabled"] = name == "bybit" or (default_balances and ex.get("trade", False))
        if not default_balances:
            ex["trade"] = name == "bybit"
    eng = raw["engine"]
    eng["db_path"] = str(tmp_path / "shio.db")
    eng["aggressive_apr"] = 1000
    if not default_balances:
        raw["capital_usd"] = 10000
        eng["paper_balances"] = {"bybit": 10000}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return str(path)


@pytest.fixture
def cfg_path(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def cfg_default(tmp_path):
    return make_cfg(tmp_path, default_balances=True)


class Env:
    def __init__(self, cfg_path, data=None, mode="semi"):
        self.cfg_path = cfg_path
        self.settings = load_settings(cfg_path)
        self.settings.mode = mode
        self.ex = make_exchange(data or {"bybit": {"HOT": (0.001, "8h", 100.0)}})
        self.db = Db(self.settings.db_path)
        self.db.init()
        self.collector = Collector(self.settings, FakeClientFactory(self.ex))
        self.broker = PaperBroker(self.collector, self.settings, self.db)
        self.notifier = FakeNotifier()
        self.clock = Clock()
        self.runner = Runner(self.settings, self.collector, self.broker, self.db, self.notifier,
                             clock=self.clock)

    def proposal(self, pid):
        return self.db.get_proposal(pid)

    def audits(self, event):
        return self.db.conn.execute(
            "SELECT detail FROM audit_log WHERE event=?", (event,)).fetchall()

    def command(self, cid):
        return self.db.conn.execute("SELECT * FROM commands WHERE id=?", (cid,)).fetchone()


def now_ms(env):
    return int(env.clock() * 1000)


@pytest.mark.parametrize("balances", ["large", "default"])
def test_full_cycle_semi(balances, request, capsys):
    cfg_path = request.getfixturevalue("cfg_path" if balances == "large" else "cfg_default")
    env = Env(cfg_path)

    async def scenario():
        await env.runner.tick_collect()
        pending = env.db.pending_proposals(now_ms(env))
        assert len(pending) == 1
        prop = pending[0]
        assert prop.base == "HOT" and prop.status == "pending"
        assert prop.expires_at == now_ms(env) + 900_000
        if balances == "default":
            # 250 USD на связку не помещаются в 170 USD на bybit — капитал урезан
            assert prop.capital == pytest.approx(170.0 * 0.98)
        assert f"python -m engine approve {prop.id}" in env.notifier.texts()

        assert main(["--config", cfg_path, "approve", str(prop.id)]) == 0
        assert env.db.positions() == []  # CLI только пишет команду

        await env.runner.tick_commands()
        positions = env.db.positions()
        assert [p.status for p in positions] == ["open"]
        assert env.proposal(prop.id).status == "executed"

        # следующий снимок: ставка стала большой, она запоминается к выплате
        env.ex.DATA["bybit"]["HOT"] = (0.01, "8h", 100.0)
        env.clock.t += 60
        await env.runner.tick_collect()
        before = await env.broker.free_balance("bybit")

        # выплата прошла
        env.clock.t = env.ex.NEXT_TS / 1000 + 1
        env.ex.NEXT_TS += 8 * H_MS
        await env.runner.tick_collect()
        pos = env.db.positions()[0]
        received = sum(l.funding_received for l in pos.legs)
        assert received > 0
        after = await env.broker.free_balance("bybit")
        assert after - before == pytest.approx(received)

    try:
        run(scenario())
    finally:
        run(env.collector.close())

    st = build_status(env.db, env.settings, now_ms(env))
    assert st["funding_total"] > 0
    assert st["pnl_total"] > 0
    assert st["positions"][0]["funding"] > 0

    capsys.readouterr()
    assert main(["--config", cfg_path, "status"]) == 0
    out = capsys.readouterr().out
    assert "HOT" in out and "Полуавтомат" in out and "виртуальн" in out


def test_auto_mode_enters_without_approval(cfg_path):
    env = Env(cfg_path, mode="auto")
    try:
        run(env.runner.tick_collect())
    finally:
        run(env.collector.close())
    assert env.db.pending_proposals(now_ms(env)) == []
    assert [(p.base, p.status) for p in env.db.positions()] == [("HOT", "open")]


def test_proposal_expires_after_15_min(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.tick_collect())
        pid = env.db.pending_proposals(now_ms(env))[0].id
        env.clock.t += 899
        run(env.runner.tick_commands())
        assert env.proposal(pid).status == "pending"
        env.clock.t += 2  # 901 с от создания
        run(env.runner.tick_commands())
        assert env.proposal(pid).status == "expired"
        # просроченное предложение подтвердить нельзя
        cid = env.db.add_command("approve", str(pid))
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    cmd = env.command(cid)
    assert cmd["status"] == "error" and "истекло" in cmd["result"]
    assert env.db.positions() == []


def test_proposal_expiry_checked_on_collect(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.tick_collect())
        pid = env.db.pending_proposals(now_ms(env))[0].id
        env.clock.t += 901
        run(env.runner.tick_collect())
    finally:
        run(env.collector.close())
    assert env.proposal(pid).status == "expired"
    # монета снова свободна — появилось новое предложение
    assert [p.base for p in env.db.pending_proposals(now_ms(env))] == ["HOT"]


def test_stop_all_closes_everything(cfg_path):
    data = {"bybit": {"HOT": (0.001, "8h", 100.0), "ALT": (0.0012, "8h", 50.0)}}
    env = Env(cfg_path, data=data, mode="auto")
    env.settings.limits.max_pair_share = 0.3
    try:
        run(env.runner.tick_collect())
        assert sorted(p.base for p in env.db.positions({"open"})) == ["ALT", "HOT"]
        cid = env.db.add_command("stop_all")
        run(env.runner.tick_commands())
        assert env.command(cid)["status"] == "done"
        positions = env.db.positions()
        assert [p.status for p in positions] == ["closed", "closed"]
        assert all(p.close_reason == "стоп всё" for p in positions)
        assert env.db.get_kv("paused") is True
        assert run(env.broker.open_amounts("bybit")) == {}
        # на паузе новых связок нет
        env.clock.t += 60
        run(env.runner.tick_collect())
    finally:
        run(env.collector.close())
    assert len(env.db.positions()) == 2
    assert env.db.pending_proposals(now_ms(env)) == []


def test_pause_and_resume_commands(cfg_path):
    env = Env(cfg_path)
    try:
        env.db.add_command("pause")
        run(env.runner.tick_commands())
        assert env.db.get_kv("paused") is True
        run(env.runner.tick_collect())
        assert env.db.pending_proposals(now_ms(env)) == []
        env.db.add_command("resume")
        run(env.runner.tick_commands())
        assert env.db.get_kv("paused") is False
        env.clock.t += 60
        run(env.runner.tick_collect())
    finally:
        run(env.collector.close())
    assert len(env.db.pending_proposals(now_ms(env))) == 1


def test_reject_and_close_commands(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.tick_collect())
        pid = env.db.pending_proposals(now_ms(env))[0].id
        cid = env.db.add_command("reject", str(pid))
        run(env.runner.tick_commands())
        assert env.command(cid)["status"] == "done"
        assert env.proposal(pid).status == "rejected"

        env.settings.mode = "auto"
        env.clock.t += 60
        run(env.runner.tick_collect())
        pos = env.db.positions({"open"})[0]
        cid = env.db.add_command("close", str(pos.id))
        run(env.runner.tick_commands())
        bad = env.db.add_command("close", "999")
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    assert env.command(cid)["status"] == "done"
    got = env.db.get_position(pos.id)
    assert got.status == "closed" and got.close_reason == "закрыто вручную"
    assert env.command(bad)["status"] == "error" and "999" in env.command(bad)["result"]


def test_approve_rechecks_limits(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.tick_collect())
        pid = env.db.pending_proposals(now_ms(env))[0].id
        # пока предложение ждало, лимит связок заполнился
        env.settings.limits.max_positions = 1
        held = Position(None, "spot-perp", "ETH", "calm", "open", 100.0, 0, legs=[
            Leg(None, None, "bybit", "ETH/USDT", "spot", "long", 0.0, 1.0, 1.0, 0.0, open=False)])
        env.db.save_position(held)
        cid = env.db.add_command("approve", str(pid))
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    cmd = env.command(cid)
    assert cmd["status"] == "error" and "максимум" in cmd["result"]
    assert env.proposal(pid).status == "pending"
    assert [p.base for p in env.db.positions()] == ["ETH"]


def test_candidate_dropped_when_balance_too_small(cfg_path):
    env = Env(cfg_path)
    env.db.set_kv("paper_balances", {"bybit": 15.0})
    try:
        run(env.runner.tick_collect())
    finally:
        run(env.collector.close())
    assert env.db.pending_proposals(now_ms(env)) == []
    skips = env.audits("skip")
    assert skips and "недостаточно свободного баланса" in skips[0]["detail"]


def test_approve_caps_capital_by_current_balance(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.tick_collect())
        pid = env.db.pending_proposals(now_ms(env))[0].id
        assert env.proposal(pid).capital == pytest.approx(5000.0)
        # баланс ушёл почти в ноль — подтверждение отклоняется, предложение ждёт
        env.db.set_kv("paper_balances", {"bybit": 10.0})
        env.db.set_kv("day_start_equity", None)  # иначе сработает лимит дневного убытка
        cid = env.db.add_command("approve", str(pid))
        run(env.runner.tick_commands())
        assert env.command(cid)["status"] == "error"
        assert "недостаточно свободного баланса" in env.command(cid)["result"]
        assert env.proposal(pid).status == "pending"
        # баланса хватает на часть — открываем на урезанный капитал
        env.db.set_kv("paper_balances", {"bybit": 3000.0})
        env.db.set_kv("day_start_equity", None)
        env.settings.limits.max_pair_share = 1.0  # чтобы ограничивал именно баланс, а не доля
        cid = env.db.add_command("approve", str(pid))
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    assert env.command(cid)["status"] == "done"
    p = env.proposal(pid)
    assert p.status == "executed" and p.capital == pytest.approx(2940.0)
    assert [(x.status, x.capital) for x in env.db.positions()] == [("open", pytest.approx(2940.0))]


def test_approve_unknown_proposal(cfg_path):
    env = Env(cfg_path)
    cid = env.db.add_command("approve", "42")
    try:
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    assert env.command(cid)["status"] == "error"
    assert "42" in env.command(cid)["result"]


def test_funding_payment_credited_to_paper_balance(cfg_path):
    env = Env(cfg_path, mode="auto")
    try:
        run(env.runner.tick_collect())             # вход
        env.clock.t += 60
        run(env.runner.tick_collect())             # запомнили ставку к выплате
        before = run(env.broker.free_balance("bybit"))
        env.clock.t = env.ex.NEXT_TS / 1000 + 1
        env.ex.NEXT_TS += 8 * H_MS
        run(env.runner.tick_collect())             # выплата
        after = run(env.broker.free_balance("bybit"))
    finally:
        run(env.collector.close())
    paid = env.db.funding_total()
    assert paid > 0
    assert after - before == pytest.approx(paid)


class FlakyCollector:
    """Первый collect бросает исключение, дальше — настоящий Collector."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    async def collect(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("биржа упала")
        return await self.inner.collect()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_tick_exception_does_not_stop_loop(cfg_path):
    env = Env(cfg_path)
    flaky = FlakyCollector(env.collector)
    runner = Runner(env.settings, flaky, env.broker, env.db, env.notifier, clock=env.clock)
    try:
        run(runner.step())
        errors = env.audits("tick_error")
        assert len(errors) == 1 and "биржа упала" in errors[0]["detail"]
        assert env.db.get_kv("heartbeat") == now_ms(env)  # остальные задачи шага выполнены
        env.clock.t += 61
        run(runner.step())
    finally:
        run(env.collector.close())
    assert flaky.calls == 2
    assert len(env.db.pending_proposals(now_ms(env))) == 1
    assert len(env.audits("tick_error")) == 1


def test_heartbeat_written(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.step())
        assert env.db.get_kv("heartbeat") == int(T0 * 1000)
        env.clock.t += 30
        run(env.runner.step())
        assert env.db.get_kv("heartbeat") == int(T0 * 1000)
        env.clock.t += 31
        run(env.runner.step())
    finally:
        run(env.collector.close())
    assert env.db.get_kv("heartbeat") == int((T0 + 61) * 1000)


def test_run_forever_reconciles_at_start_and_stops(cfg_path):
    env = Env(cfg_path)
    # незавершённое открытие, у брокера ничего нет → сверка помечает «failed»
    orphan = Position(None, "spot-perp", "X", "calm", "opening", 100.0, 0, legs=[
        Leg(None, None, "bybit", "X/USDT", "spot", "long", 0.0, 0.0, 1.0, 0.0)])
    env.db.save_position(orphan)
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
        run(asyncio.wait_for(env.runner.run_forever(), 5))
    finally:
        run(env.collector.close())
    assert steps["n"] == 3
    assert env.db.get_position(orphan.id).status == "failed"


def test_log_notifier_writes_log_and_audit(caplog):
    db = Db(":memory:")
    db.init()
    with caplog.at_level(logging.INFO, logger="engine.notifier"):
        run(LogNotifier(db).send("Проверка связи", "alert"))
    assert "Проверка связи" in caplog.text
    row = db.conn.execute("SELECT detail FROM audit_log WHERE event='notify'").fetchone()
    assert "Проверка связи" in row["detail"] and "alert" in row["detail"]


def test_cli_commands_only_write_commands(cfg_path, monkeypatch, capsys):
    for argv, expected in ((["reject", "5"], ("reject", "5")), (["close", "7"], ("close", "7")),
                           (["pause"], ("pause", None)), (["resume"], ("resume", None))):
        assert main(["--config", cfg_path, *argv]) == 0
        db = Db(load_settings(cfg_path).db_path)
        row = db.conn.execute("SELECT type, arg FROM commands ORDER BY id DESC").fetchone()
        assert (row["type"], row["arg"]) == expected
        assert db.positions() == []

    monkeypatch.setattr("builtins.input", lambda prompt="": "нет")
    assert main(["--config", cfg_path, "stop"]) == 0
    db = Db(load_settings(cfg_path).db_path)
    assert db.conn.execute("SELECT COUNT(*) FROM commands WHERE type='stop_all'").fetchone()[0] == 0
    monkeypatch.setattr("builtins.input", lambda prompt="": "да")
    assert main(["--config", cfg_path, "stop"]) == 0
    assert db.conn.execute("SELECT COUNT(*) FROM commands WHERE type='stop_all'").fetchone()[0] == 1


def test_cli_status_and_proposals_on_empty_db(cfg_path, capsys):
    assert main(["--config", cfg_path, "status"]) == 0
    out = capsys.readouterr().out
    assert "Капитал" in out and "Открытых связок нет" in out
    assert main(["--config", cfg_path, "proposals"]) == 0
    assert "нет" in capsys.readouterr().out.lower()


def test_cli_run_refuses_real_trading(cfg_path, capsys):
    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["engine"]["virtual"] = False
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)
    assert main(["--config", cfg_path, "run"]) != 0
    assert "этап" in capsys.readouterr().out.lower()


def test_format_status_lists_positions(cfg_path):
    settings = load_settings(cfg_path)
    db = Db(settings.db_path)
    db.init()
    p = Position(None, "spot-perp", "HOT", "calm", "open", 100.0, 0, legs=[
        Leg(None, None, "bybit", "HOT/USDT", "spot", "long", 1.0, 100.0, 1.0, 100.0),
        Leg(None, None, "bybit", HOT_PERP, "perp", "short", 1.0, 100.0, 3.0, 33.3,
            funding_received=0.5)])
    db.save_position(p)
    db.set_kv("stats", {"ts": int(T0 * 1000), "equity": 10010.0, "day_pnl": 3.0,
                        "buffers": {str(p.id): 0.82}})
    text = format_status(build_status(db, settings, int(T0 * 1000)))
    assert "HOT" in text and "bybit" in text and "82%" in text and "+0.50" in text


def test_approve_caps_capital_by_pair_share(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.tick_collect())
        p = env.db.pending_proposals(now_ms(env))[0]
        p.capital = 9000.0  # больше, чем доля на связку от текущего капитала (5000)
        env.db.update_proposal(p)
        env.db.add_command("approve", str(p.id))
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    assert env.proposal(p.id).capital == pytest.approx(5000.0)
    assert env.db.positions()[0].capital == pytest.approx(5000.0)


def test_balances_reserved_within_one_tick(cfg_path):
    data = {"bybit": {"HOT": (0.001, "8h", 100.0), "ALT": (0.0012, "8h", 50.0)}}
    env = Env(cfg_path, data=data)
    try:
        run(env.runner.tick_collect())
    finally:
        run(env.collector.close())
    props = env.db.pending_proposals(now_ms(env))
    # первая связка берёт 5000 из 10000, вторая — не больше 0.98 × оставшихся 5000
    assert [(x.base, x.capital) for x in props] == [("ALT", pytest.approx(5000.0)),
                                                   ("HOT", pytest.approx(4900.0))]


class SwitchCollector:
    """Collector, у которого можно «сломать» сбор данных."""

    def __init__(self, inner):
        self.inner = inner
        self.broken = False

    async def collect(self):
        if self.broken:
            raise RuntimeError("нет связи")
        return await self.inner.collect()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_guard_warns_when_market_data_stale(cfg_path):
    env = Env(cfg_path, mode="auto")
    col = SwitchCollector(env.collector)
    runner = Runner(env.settings, col, env.broker, env.db, env.notifier, clock=env.clock)
    try:
        run(runner.tick_collect())                 # вход по свежим данным
        assert len(env.db.positions({"open"})) == 1
        run(runner.tick_guard())                   # запомнили последние цены
        assert "устарели" not in env.notifier.texts()
        col.broken = True
        env.clock.t += 60
        run(runner.tick_collect())                 # сбор не удался, снимок старый
        run(runner.tick_guard())
        assert "устарели" not in env.notifier.texts()  # ещё в пределах 2 интервалов
        env.clock.t += 2 * env.settings.collect_interval_s
        run(runner.tick_collect())
        run(runner.tick_guard())
    finally:
        run(env.collector.close())
    assert "данные устарели" in env.notifier.texts()


def test_stop_all_pauses_even_if_close_fails(cfg_path, monkeypatch):
    env = Env(cfg_path, mode="auto")
    seen = {}

    async def broken_close(pos, *args, **kwargs):
        seen["paused"] = env.db.get_kv("paused")
        raise RuntimeError("биржа недоступна")

    try:
        run(env.runner.tick_collect())
        monkeypatch.setattr("engine.runner.close_pair", broken_close)
        cid = env.db.add_command("stop_all")
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    assert seen["paused"] is True
    assert env.db.get_kv("paused") is True
    assert env.command(cid)["status"] == "error"


def test_approve_open_pair_crash_marks_proposal_failed(cfg_path, monkeypatch):
    env = Env(cfg_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("сбой исполнителя")

    try:
        run(env.runner.tick_collect())
        pid = env.db.pending_proposals(now_ms(env))[0].id
        monkeypatch.setattr("engine.runner.open_pair", boom)
        cid = env.db.add_command("approve", str(pid))
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    assert env.proposal(pid).status == "failed"
    assert env.command(cid)["status"] == "error" and "сбой исполнителя" in env.command(cid)["result"]


def test_taken_commands_failed_on_start(cfg_path):
    env = Env(cfg_path)
    cid = env.db.add_command("pause")
    env.db.take_new_commands()  # взята прошлым запуском, который упал
    env.runner.loop_sleep_s = 0
    real_step = env.runner.step

    async def one_step():
        await real_step()
        env.runner.stop()

    env.runner.step = one_step
    try:
        run(asyncio.wait_for(env.runner.run_forever(), 5))
    finally:
        run(env.collector.close())
    cmd = env.command(cid)
    assert cmd["status"] == "error" and cmd["result"] == "бот перезапущен"
    assert env.db.get_kv("paused") is None  # команда не выполнялась повторно
