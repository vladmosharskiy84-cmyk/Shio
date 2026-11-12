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
        assert [(q.id, ttl, hold) for q, ttl, hold in env.notifier.proposals] == [
            (prop.id, 15, env.settings.hold_days)]

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


def _crashing_open_pair(env):
    """open_pair, который успел открыть первую ногу и упал (позиция «opening»)."""
    async def crash(plan, capital, base, kind, profile, broker, db, notifier, settings):
        legs = [Leg(None, None, p["exchange"], p["symbol"], p["kind"], p["side"], 0.0, 0.0, 1.0, 0.0)
                for p in plan]
        db.save_position(Position(None, kind, base, profile, "opening", capital, 0, legs=legs))
        await broker.market_ioc(plan[0]["exchange"], plan[0]["symbol"], plan[0]["kind"], "buy",
                                1.0, 0.01)
        raise RuntimeError("сбой исполнителя")
    return crash


@pytest.mark.parametrize("mode", ["auto", "semi"])
def test_open_pair_crash_triggers_reconcile(cfg_path, monkeypatch, mode):
    env = Env(cfg_path, mode=mode)
    try:
        if mode == "semi":
            run(env.runner.tick_collect())
            pid = env.db.pending_proposals(now_ms(env))[0].id
        monkeypatch.setattr("engine.runner.open_pair", _crashing_open_pair(env))
        if mode == "auto":
            run(env.runner.tick_collect())
        else:
            env.db.add_command("approve", str(pid))
            run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    # сверка сразу закрыла осиротевшую ногу, не дожидаясь 10-минутного тика
    [pos] = env.db.positions()
    assert pos.status == "failed" and pos.close_reason == "незавершённое открытие"
    assert run(env.broker.open_amounts("bybit")) == {}
    if mode == "semi":
        assert env.proposal(pid).status == "failed"


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


def test_daily_loss_pauses_until_resume(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.tick_collect())            # база суток: 10000
        assert env.db.get_kv("day_start_equity")["equity"] == pytest.approx(10000)
        env.db.set_kv("paper_balances", {"bybit": 9400.0})  # убыток 6% > 5%
        env.clock.t += 60
        run(env.runner.tick_collect())
        assert env.db.get_kv("paused") is True
        assert len(env.audits("daily_loss_pause")) == 1
        alerts = [t for t, lvl in env.notifier.sent if lvl == "alert"]
        assert len(alerts) == 1 and "Дневной убыток" in alerts[0]
        env.clock.t += 60
        run(env.runner.tick_collect())            # уже на паузе — без повторной тревоги
        assert len(env.audits("daily_loss_pause")) == 1
        assert len([1 for _, lvl in env.notifier.sent if lvl == "alert"]) == 1
        # новые сутки UTC: дневной P&L обнулился, но пауза остаётся до resume
        env.clock.t += 86_400
        run(env.runner.tick_collect())
        assert env.db.get_kv("paused") is True
        assert env.db.pending_proposals(now_ms(env)) == []
        env.db.add_command("resume")
        run(env.runner.tick_commands())
        env.clock.t += 60
        run(env.runner.tick_collect())
    finally:
        run(env.collector.close())
    assert env.db.get_kv("paused") is False
    assert [p.base for p in env.db.pending_proposals(now_ms(env))] == ["HOT"]


def test_stop_all_reports_real_outcome(cfg_path, monkeypatch):
    data = {"bybit": {"HOT": (0.001, "8h", 100.0), "ALT": (0.0012, "8h", 50.0)}}
    env = Env(cfg_path, data=data, mode="auto")
    env.settings.limits.max_pair_share = 0.3

    async def half_close(pos, reason, broker, db, notifier, settings):
        pos.status = "closed" if pos.base == "HOT" else "closing"
        db.update_position(pos)
        return pos

    try:
        run(env.runner.tick_collect())
        monkeypatch.setattr("engine.runner.close_pair", half_close)
        cid = env.db.add_command("stop_all")
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    text, level = [s for s in env.notifier.sent if s[1] == "alert"][-1]
    assert "закрыто 1 из 2" in text and "ещё закрывается 1" in text
    assert "закрыто 1 из 2" in env.command(cid)["result"]


def test_runner_proposal_goes_through_notifier_proposal(cfg_path):
    env = Env(cfg_path)
    try:
        run(env.runner.tick_collect())
    finally:
        run(env.collector.close())
    (prop,) = env.db.pending_proposals(now_ms(env))
    assert len(env.notifier.proposals) == 1
    p, ttl_min, hold_days = env.notifier.proposals[0]
    assert (p.id, ttl_min, hold_days) == (prop.id, 15, env.settings.hold_days)
    assert "python -m engine approve" not in env.notifier.texts()


def test_runner_reports_command_result(cfg_path):
    env = Env(cfg_path)
    try:
        env.db.add_command("pause")
        run(env.runner.tick_commands())
        env.db.add_command("approve", "999")
        run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    assert ("Команда «pause»: бот на паузе", "info") in env.notifier.sent
    texts = [(t, lvl) for t, lvl in env.notifier.sent if t.startswith("Команда «approve»")]
    assert texts == [("Команда «approve»: предложение #999 не найдено", "warn")]


# --- ежедневная сводка и резервные копии ---
DAY0 = 1_800_000_000.0 - (1_800_000_000 % 86400)  # полночь UTC дня T0


def at(env, hour, minute=0, day=0):
    env.clock.t = DAY0 + day * 86400 + hour * 3600 + minute * 60


def test_daily_summary_once_per_day(cfg_path, monkeypatch):
    monkeypatch.delenv("SHIO_BACKUP_KEY", raising=False)
    env = Env(cfg_path)
    env.settings.backup.enabled = False
    hour = env.settings.telegram.daily_summary_hour_utc
    try:
        pid = env.db.save_position(Position(None, "perp-perp", "HOT", "calm", "open", 100.0, 1,
                                            legs=[Leg(None, None, "bybit", HOT_PERP, "perp", "short",
                                                      1.0, 100.0, 1.0, 100.0)]))
        leg_id = env.db.get_position(pid).legs[0].id
        day_start_ms = int(DAY0 * 1000)
        env.db.add_funding(leg_id, day_start_ms - 3_600_000, 0.001, 99.0)  # вчера: не считается
        env.db.add_funding(leg_id, day_start_ms + 3_600_000, 0.001, 7.5)
        at(env, hour - 1, 59)
        run(env.runner.tick_daily())
        assert env.notifier.sent == []
        at(env, hour)
        run(env.runner.tick_daily())
        at(env, hour, 1)
        run(env.runner.tick_daily())
        summaries = [t for t, _ in env.notifier.sent if "Сводка" in t]
        assert len(summaries) == 1
        text = summaries[0]
        for word in ("Капитал", "P&L", "Funding", "Открытых связок", "Ожидают"):
            assert word in text
        assert "Funding за день: +7.50 USD" in text      # только выплаты текущих суток
        assert "Открытых связок: 1" in text
        assert "Ожидают решения предложений: 0" in text
        at(env, hour, day=1)
        run(env.runner.tick_daily())
        assert len([t for t, _ in env.notifier.sent if "Сводка" in t]) == 2
    finally:
        run(env.collector.close())


def test_daily_summary_late_start_sends_same_day(cfg_path, monkeypatch):
    monkeypatch.delenv("SHIO_BACKUP_KEY", raising=False)
    env = Env(cfg_path)
    env.settings.backup.enabled = False
    try:
        at(env, env.settings.telegram.daily_summary_hour_utc + 2)
        run(env.runner.tick_daily())
        assert len([t for t, _ in env.notifier.sent if "Сводка" in t]) == 1
    finally:
        run(env.collector.close())


def test_backup_sent_once_per_day_with_key(cfg_path, monkeypatch):
    from engine import backup
    key = bytes(range(32))
    monkeypatch.setenv("SHIO_BACKUP_KEY", __import__("base64").b64encode(key).decode())
    env = Env(cfg_path)
    hour = env.settings.backup.hour_utc
    try:
        at(env, hour)
        run(env.runner.tick_daily())
        run(env.runner.tick_daily())
        assert len(env.notifier.documents) == 1
        name, data, caption = env.notifier.documents[0]
        assert name.startswith("shio-") and name.endswith(".db.enc")
        assert "Резервная копия" in caption
        assert backup.decrypt(data, key).startswith(b"SQLite format 3")
        at(env, hour, day=1)
        run(env.runner.tick_daily())
        assert len(env.notifier.documents) == 2
    finally:
        run(env.collector.close())


def test_backup_skipped_without_key_warns_once(cfg_path, monkeypatch):
    monkeypatch.delenv("SHIO_BACKUP_KEY", raising=False)
    env = Env(cfg_path)
    hour = env.settings.backup.hour_utc
    try:
        at(env, hour)
        run(env.runner.tick_daily())
        at(env, hour, 5)
        run(env.runner.tick_daily())
        assert env.notifier.documents == []
        warns = [(t, l) for t, l in env.notifier.sent if "SHIO_BACKUP_KEY" in t]
        assert len(warns) == 1 and warns[0][1] == "warn"
        at(env, hour, day=1)
        run(env.runner.tick_daily())
        assert len([1 for t, _ in env.notifier.sent if "SHIO_BACKUP_KEY" in t]) == 2
    finally:
        run(env.collector.close())


def test_backup_disabled_does_nothing(cfg_path, monkeypatch):
    monkeypatch.delenv("SHIO_BACKUP_KEY", raising=False)
    env = Env(cfg_path)
    env.settings.backup.enabled = False
    try:
        at(env, env.settings.backup.hour_utc)
        run(env.runner.tick_daily())
        assert env.notifier.documents == [] and "SHIO_BACKUP_KEY" not in env.notifier.texts()
    finally:
        run(env.collector.close())


def test_summary_retried_after_equity_failure(cfg_path, monkeypatch):
    monkeypatch.delenv("SHIO_BACKUP_KEY", raising=False)
    env = Env(cfg_path)
    env.settings.backup.enabled = False
    hour = env.settings.telegram.daily_summary_hour_utc
    real = env.runner._equity
    calls = []

    async def flaky(now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("биржа недоступна")
        return await real(now)

    env.runner._equity = flaky
    try:
        at(env, hour)
        run(env.runner.tick_daily())
        assert env.notifier.sent == [] and env.db.get_kv("daily:summary") is None
        assert len(env.audits("tick_error")) == 1
        at(env, hour, 10)
        run(env.runner.tick_daily())
        assert len(calls) == 1                       # раньше 20 минут не повторяем
        at(env, hour, 21)
        run(env.runner.tick_daily())
        assert len([t for t, _ in env.notifier.sent if "Сводка" in t]) == 1
        at(env, hour, 45)
        run(env.runner.tick_daily())
        assert len(calls) == 2                       # после успеха больше не пытаемся
    finally:
        run(env.collector.close())


def test_backup_retried_when_delivery_fails(cfg_path, monkeypatch):
    monkeypatch.setenv("SHIO_BACKUP_KEY", __import__("base64").b64encode(bytes(32)).decode())
    env = Env(cfg_path)
    env.notifier.document_failures = 1
    hour = env.settings.backup.hour_utc
    try:
        at(env, hour)
        run(env.runner.tick_daily())
        assert env.notifier.documents == [] and env.db.get_kv("daily:backup") is None
        at(env, hour, 10)
        run(env.runner.tick_daily())
        assert env.notifier.documents == []
        at(env, hour, 21)
        run(env.runner.tick_daily())
        assert len(env.notifier.documents) == 1
        assert env.db.get_kv("daily:backup") is not None
        at(env, hour, 50)
        run(env.runner.tick_daily())
        assert len(env.notifier.documents) == 1
    finally:
        run(env.collector.close())


def test_backup_snapshot_failure_warns_once_and_retries(cfg_path, monkeypatch):
    from engine import backup
    monkeypatch.setenv("SHIO_BACKUP_KEY", __import__("base64").b64encode(bytes(32)).decode())
    env = Env(cfg_path)
    hour = env.settings.backup.hour_utc
    real = backup.snapshot_bytes
    state = {"fail": 2}

    def flaky(db):
        if state["fail"]:
            state["fail"] -= 1
            raise OSError("диск полон")
        return real(db)

    monkeypatch.setattr(backup, "snapshot_bytes", flaky)
    try:
        at(env, hour)
        run(env.runner.tick_daily())
        at(env, hour, 21)
        run(env.runner.tick_daily())
        warns = [(t, l) for t, l in env.notifier.sent if "снимка" in t]
        assert len(warns) == 1 and warns[0][1] == "warn"   # предупреждение раз в день
        assert env.notifier.documents == []
        at(env, hour, 42)
        run(env.runner.tick_daily())
        assert len(env.notifier.documents) == 1
    finally:
        run(env.collector.close())


def test_runner_launchpool_tick_respects_interval_and_disabled(cfg_path):
    from tests.engine.test_launchpool import FakeSource, ann

    env = Env(cfg_path)
    src = FakeSource([])
    env.runner.announcement_sources = [src]
    env.settings.launchpool.interval_s = 1800
    try:
        run(env.runner.step())  # первый запуск: ничего не шлём
        src.items.append(ann(1))
        env.clock.t += 60
        run(env.runner.step())
        assert not [t for t, _ in env.notifier.sent if "Новый пул" in t]
        env.clock.t += 1800
        run(env.runner.step())
        assert len([t for t, _ in env.notifier.sent if "Новый пул" in t]) == 1
        env.settings.launchpool.enabled = False
        src.items.append(ann(2))
        env.clock.t += 1800
        run(env.runner.step())
        assert len([t for t, _ in env.notifier.sent if "Новый пул" in t]) == 1
    finally:
        run(env.collector.close())


def test_report_failure_does_not_skip_remaining_commands(cfg_path, caplog):
    class ReportFails(FakeNotifier):
        async def send(self, text, level="info", buttons=None):
            if text.startswith("Команда «"):
                raise ConnectionError("отчёт не ушёл")
            await super().send(text, level)

    env = Env(cfg_path)
    env.runner.notifier = ReportFails()
    try:
        first = env.db.add_command("pause")
        second = env.db.add_command("resume")
        with caplog.at_level(logging.ERROR):
            run(env.runner.tick_commands())
    finally:
        run(env.collector.close())
    assert env.command(first)["status"] == "done"
    assert env.command(second)["status"] == "done"
    assert env.db.get_kv("paused") is False
    assert any("отчёт" in r.getMessage() or "Команда" in r.getMessage() for r in caplog.records)


class RecordingPinger:
    def __init__(self, env, exc=None):
        self.env, self.exc, self.seen = env, exc, []

    async def ping(self, fail=False):
        self.seen.append(self.env.db.get_kv("heartbeat"))
        if self.exc:
            raise self.exc
        return True


def test_heartbeat_pings_after_db_write(cfg_path):
    env = Env(cfg_path)
    env.runner.pinger = RecordingPinger(env)
    run(env.runner.tick_heartbeat())
    assert env.runner.pinger.seen == [int(T0 * 1000)]


def test_heartbeat_pinger_exception_isolated(cfg_path, caplog):
    env = Env(cfg_path)
    env.runner.pinger = RecordingPinger(env, exc=RuntimeError("boom-secret"))
    with caplog.at_level(logging.WARNING):
        run(env.runner.step())
    assert env.db.get_kv("heartbeat") == int(T0 * 1000)
    assert env.audits("tick_error") == []
    assert "Сторож" in caplog.text
    assert "RuntimeError" in caplog.text and "boom-secret" not in caplog.text


def test_stop_waits_for_current_step(cfg_path):
    env = Env(cfg_path)
    env.runner.loop_sleep_s = 0
    done = []
    orig = env.runner.tick_commands

    async def tick():
        env.runner.stop()
        await asyncio.sleep(0)
        await orig()
        done.append(1)

    env.runner.tick_commands = tick
    env.runner._jobs = [(n, i, tick if n == "commands" else t) for n, i, t in env.runner._jobs]

    async def scenario():
        await asyncio.wait_for(env.runner.run_forever(), 5)

    steps = []
    orig_step = env.runner.step

    async def counting_step():
        steps.append(1)
        await orig_step()

    env.runner.step = counting_step
    seen = []
    orig_delete = env.db.delete_kv

    def delete_kv(key):
        seen.append(env.db.get_kv(key))  # что было перед снятием сигнала при остановке
        orig_delete(key)

    env.db.delete_kv = delete_kv
    run(scenario())
    assert done == [1]
    assert steps == [1]  # второй шаг не запускался
    assert seen == [int(T0 * 1000)]  # остальные задачи шага выполнены


def test_setup_logging_idempotent(cfg_path):
    import logging.handlers
    from engine.__main__ import _setup_logging

    settings = load_settings(cfg_path)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        _setup_logging(settings)
        _setup_logging(settings)
        added = [h for h in root.handlers if h not in before]
        files = [h for h in added if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(files) == 1
        assert files[0].maxBytes == 10 * 1024 * 1024 and files[0].backupCount == 5
        assert len(added) == 2
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_heartbeat_hanging_pinger_bounded(cfg_path, monkeypatch):
    import time
    import engine.runner as runner_mod

    class Hanging:
        async def ping(self, fail=False):
            await asyncio.sleep(60)

    monkeypatch.setattr(runner_mod, "PING_TIMEOUT_S", 0.1)
    env = Env(cfg_path)
    env.runner.pinger = Hanging()
    start = time.monotonic()
    run(env.runner.tick_heartbeat())
    assert time.monotonic() - start < 2
    assert env.db.get_kv("heartbeat") == int(T0 * 1000)


def test_install_signal_handlers_stop_once(caplog):
    from engine.__main__ import _install_signal_handlers

    class FakeRunner:
        stops = 0

        def stop(self):
            self.stops += 1

    async def scenario():
        loop = asyncio.get_running_loop()
        captured = {}
        loop.add_signal_handler = lambda sig, cb, *a: captured.__setitem__(sig, (cb, a))
        r = FakeRunner()
        _install_signal_handlers(r)
        import signal
        assert set(captured) == {signal.SIGTERM, signal.SIGINT}
        cb, a = captured[signal.SIGTERM]
        cb(*a)
        cb2, a2 = captured[signal.SIGINT]
        with caplog.at_level(logging.WARNING):
            cb2(*a2)
        return r

    r = run(scenario())
    assert r.stops == 1
    assert "Повторный сигнал" in caplog.text


def test_log_pinger(caplog):
    from engine.__main__ import _log_pinger
    from engine.watchdog import HealthPinger

    with caplog.at_level(logging.INFO, logger="engine"):
        _log_pinger(HealthPinger(None))
        _log_pinger(HealthPinger("https://hc-ping.com/secret-uuid"))
    assert "Сторож не настроен" in caplog.text
    assert "hc-ping.com" in caplog.text and "secret-uuid" not in caplog.text


def test_clean_stop_clears_heartbeat(cfg_path):
    # после чистой остановки restore не ждёт 3 минуты: сигнала «жив» больше нет
    env = Env(cfg_path)
    env.runner.loop_sleep_s = 0
    orig_step = env.runner.step

    async def step_then_stop():
        await orig_step()
        assert env.db.get_kv("heartbeat") is not None
        env.runner.stop()

    env.runner.step = step_then_stop
    run(asyncio.wait_for(env.runner.run_forever(), 5))
    assert env.db.get_kv("heartbeat") is None
