"""Главный цикл движка: сбор данных, решения, охрана позиций и команды пользователя."""
from __future__ import annotations

import asyncio
import logging
import time

from engine import ledger
from engine.collector import MarketSnapshot
from engine.decider import (MIN_PAIR_CAPITAL, NO_BALANCE_REASON, affordable_capital,
                            capital_for_pair, check_limits, daily_loss_hit, decide,
                            reserve_capital)
from engine.executor import close_pair, open_pair
from engine.finder import find_candidates
from engine.guardian import guard, leg_buffer
from engine.models import Command, Proposal
from engine.reconciler import reconcile

log = logging.getLogger(__name__)

COMMANDS_INTERVAL_S = 2
HEARTBEAT_INTERVAL_S = 60
RECONCILE_INTERVAL_S = 600
_LOG_SKIPS = 5  # сколько причин пропуска лучших кандидатов писать в журнал за тик

_HELD = {"opening", "open", "closing"}
_PROPOSAL_STATUS_RU = {"pending": "ожидает", "approved": "подтверждено", "rejected": "отклонено",
                       "expired": "истекло", "executed": "исполнено", "failed": "не исполнено"}


class CommandError(Exception):
    """Команду выполнить нельзя (текст — на русском, идёт в commands.result)."""


class Runner:
    loop_sleep_s = 1.0

    def __init__(self, settings, collector, broker, db, notifier, clock=time.time):
        self.settings = settings
        self.collector = collector
        self.broker = broker
        self.db = db
        self.notifier = notifier
        self.clock = clock
        self.snapshot: MarketSnapshot | None = None
        self.snapshot_at: int | None = None  # now_ms (по self.clock) последнего удачного сбора
        self.last_marks: dict[tuple[str, str], float] = {}
        self._stopping = False
        self._next_due: dict[str, float] = {}
        self._jobs = [
            ("reconcile", RECONCILE_INTERVAL_S, self.tick_reconcile),
            ("collect", settings.collect_interval_s, self.tick_collect),
            ("commands", COMMANDS_INTERVAL_S, self.tick_commands),
            ("guard", settings.guard_interval_s, self.tick_guard),
            ("heartbeat", HEARTBEAT_INTERVAL_S, self.tick_heartbeat),
        ]

    # --- цикл ---
    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    async def step(self) -> None:
        """Выполнить все задачи, срок которых наступил (по часам self.clock)."""
        for name, interval, tick in self._jobs:
            now = self.clock()
            if now >= self._next_due.get(name, float("-inf")):
                self._next_due[name] = now + interval
                await tick()

    async def run_forever(self) -> None:
        """Сверка при старте (первый шаг), затем задачи по расписанию до stop()."""
        log.info("Движок запущен: режим %s, %s торговля",
                 "автомат" if self.settings.mode == "auto" else "полуавтомат",
                 "виртуальная" if self.settings.virtual else "реальная")
        self._stopping = False
        try:
            lost = self.db.fail_taken_commands("бот перезапущен")
        except Exception:  # noqa: BLE001
            log.exception("Не удалось разобрать команды, оставшиеся с прошлого запуска")
        else:
            if lost:
                log.warning("Команд, прерванных прошлым запуском: %d (помечены ошибкой)", lost)
        while not self._stopping:
            await self.step()
            await asyncio.sleep(self.loop_sleep_s)

    def stop(self) -> None:
        self._stopping = True

    async def _guarded(self, name: str, coro_fn) -> None:
        """Ошибка тика пишется в журнал и audit_log и не останавливает цикл."""
        try:
            await coro_fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка в задаче %s: %s", name, exc)
            try:
                self.db.audit("tick_error", {"tick": name, "error": repr(exc)})
            except Exception:  # noqa: BLE001
                log.exception("Не удалось записать ошибку задачи %s в журнал", name)

    # --- задачи ---
    async def tick_reconcile(self) -> None:
        await self._guarded("reconcile", self._reconcile)

    async def tick_collect(self) -> None:
        await self._guarded("collect", self._collect)

    async def tick_guard(self) -> None:
        await self._guarded("guard", self._guard)

    async def tick_commands(self) -> None:
        await self._guarded("commands", self._commands)

    async def tick_heartbeat(self) -> None:
        await self._guarded("heartbeat", self._heartbeat)

    async def _heartbeat(self) -> None:
        self.db.heartbeat(self.now_ms())

    async def _reconcile(self) -> None:
        issues = await reconcile(self.broker, self.db, self.notifier, self.settings)
        if issues:
            log.warning("Сверка: расхождений %d", len(issues))

    # --- сбор и решения ---
    async def _balances(self) -> dict[str, float]:
        trade = [n for n, e in self.settings.exchanges.items() if e.trade]
        return {ex: await self.broker.free_balance(ex) for ex in trade}

    async def _equity(self, now: int) -> tuple[float, float]:
        balances = await self._balances()
        snap = self.snapshot or MarketSnapshot(ts=now)
        eq = ledger.equity(self.db, balances, self.db.positions(_HELD), snap)
        return eq, ledger.day_pnl(self.db, now, eq)

    def _save_stats(self, **values) -> None:
        stats = self.db.get_kv("stats") or {}
        stats.update(values, ts=self.now_ms())
        self.db.set_kv("stats", stats)

    async def _collect(self) -> None:
        now = self.now_ms()
        await self._expire(now)
        snap = await self.collector.collect()
        self.snapshot = snap
        self.snapshot_at = now
        log.info("Снимок рынка: %s; фьючерсов %d, спотовых пар %d",
                 ", ".join(f"{n} {'есть данные' if ok else 'нет данных'}"
                           for n, ok in snap.ok.items()), len(snap.perps), len(snap.spots))

        for leg, amount in ledger.accrue(snap, self.db.positions(_HELD), self.db, now):
            await self.broker.credit_funding(leg.exchange, amount)
            log.info("Выплата funding: %s %s %+.4f USD", leg.exchange, leg.symbol, amount)

        eq, dpnl = await self._equity(now)
        self._save_stats(equity=eq, day_pnl=dpnl)
        await self._check_daily_loss(eq, dpnl)

        cands = await find_candidates(snap, self.collector, self.settings,
                                      lambda kind: capital_for_pair(eq, self.settings))
        cands = await self._fit_to_balances(cands)
        paused = bool(self.db.get_kv("paused", False))
        decisions = decide(cands, self.db.positions(_HELD), self.db.pending_proposals(now),
                           eq, dpnl, paused, self.settings)
        counts = {"propose": 0, "enter": 0, "skip": 0}
        avail = await self._balances()  # уменьшается по мере принятых связок этого тика
        for i, d in enumerate(decisions):
            c = d.candidate
            if d.action in ("propose", "enter"):
                capital = affordable_capital(c.op.kind, c.legs_plan, c.capital, avail)
                if capital < MIN_PAIR_CAPITAL:
                    self.db.audit("skip", {"base": c.op.base, "kind": c.op.kind,
                                           "reason": NO_BALANCE_REASON})
                    counts["skip"] += 1
                    continue
                c.capital = capital
                reserve_capital(c.op.kind, c.legs_plan, capital, avail)
            counts[d.action] += 1
            if d.action == "propose":
                await self._propose(c, now)
            elif d.action == "enter":
                log.info("Автомат: вхожу в связку %s %s", c.op.kind, c.op.base)
                await self._open_pair(c.legs_plan, c.capital, c.op.base, c.op.kind, c.profile)
            elif i < _LOG_SKIPS:
                log.info("Пропуск %s %s (%.1f%% годовых): %s", c.op.kind, c.op.base,
                         c.op.apr_net_capital, d.reason)
        log.info("Кандидатов %d: предложено %d, вход %d, пропущено %d; капитал %.2f USD, "
                 "P&L за день %+.2f USD", len(cands), counts["propose"], counts["enter"],
                 counts["skip"], eq, dpnl)

    async def _check_daily_loss(self, eq: float, dpnl: float) -> None:
        """Дневной убыток достиг лимита — пауза до ручного возобновления (resume)."""
        if bool(self.db.get_kv("paused", False)) or not daily_loss_hit(eq, dpnl, self.settings):
            return
        self.db.set_kv("paused", True)
        limit = self.settings.limits.daily_loss_pause
        self.db.audit("daily_loss_pause", {"equity": eq, "day_start_equity": eq - dpnl,
                                           "day_pnl": dpnl, "limit": limit})
        await self.notifier.send(
            f"Дневной убыток {dpnl:+.2f} USD достиг лимита {limit:.0%} от капитала на начало "
            f"суток ({eq - dpnl:.2f} USD). Бот на паузе: новые связки не открываются до "
            f"команды resume.", "alert")

    async def _fit_to_balances(self, cands: list) -> list:
        """Урезать капитал кандидатов до свободных балансов их бирж; слишком малые — убрать."""
        balances = await self._balances()
        out = []
        for c in cands:
            capital = affordable_capital(c.op.kind, c.legs_plan, c.capital, balances)
            if capital < MIN_PAIR_CAPITAL:
                self.db.audit("skip", {"base": c.op.base, "kind": c.op.kind,
                                       "reason": NO_BALANCE_REASON})
                continue
            c.capital = capital
            out.append(c)
        return out

    async def _open_pair(self, plan, capital, base, kind, profile):
        """open_pair; если он упал посреди открытия — сразу сверка с брокером.

        Иначе позиция «opening» с исполненной ногой осталась бы без охраны до
        следующей плановой сверки. Исключение пробрасывается дальше.
        """
        try:
            return await open_pair(plan, capital, base, kind, profile,
                                   self.broker, self.db, self.notifier, self.settings)
        except Exception as exc:
            self.db.audit("open_pair_error", {"base": base, "kind": kind, "error": repr(exc)})
            try:
                await self._reconcile()
            except Exception:  # noqa: BLE001 — исходная ошибка важнее
                log.exception("Сверка после сбоя открытия %s не удалась", base)
            raise

    def _expected_profit(self, apr: float, capital: float) -> float:
        """Ожидаемая прибыль за hold_days при данной доходности (% годовых на капитал)."""
        return apr / 100 * capital * self.settings.hold_days / 365

    async def _propose(self, c, now: int) -> None:
        ttl = self.settings.limits.proposal_ttl_s
        p = Proposal(None, now, now + ttl * 1000, c.op.base, c.op.kind, c.profile, c.legs_plan,
                     c.capital, c.op.apr_net_capital,
                     self._expected_profit(c.op.apr_net_capital, c.capital), "pending")
        self.db.save_proposal(p)
        self.db.audit("proposal", {"id": p.id, "base": p.base, "kind": p.kind,
                                   "capital": p.capital, "apr": p.expected_apr})
        legs = ", ".join(f"{l['side']} {l['kind']} на {l['exchange']}" for l in c.legs_plan)
        profile = "агрессивный" if c.profile == "aggressive" else "спокойный"
        await self.notifier.send(
            f"Предложение #{p.id}: связка {p.kind} {p.base} ({legs}), профиль {profile}, "
            f"капитал {p.capital:.2f} USD, ожидаемая доходность {p.expected_apr:.1f}% годовых "
            f"(~{p.expected_profit:.2f} USD за {self.settings.hold_days:g} дн.). "
            f"Подтвердить: python -m engine approve {p.id} — действует {ttl // 60} мин.", "info")

    async def _expire(self, now: int) -> None:
        for pid in self.db.expire_proposals(now):
            self.db.audit("proposal_expired", {"id": pid})
            log.info("Предложение #%d истекло", pid)

    # --- охрана ---
    def _fresh_snapshot(self, now: int) -> MarketSnapshot:
        """Последний снимок, если он не старше 2 интервалов сбора, иначе пустой.

        С пустым снимком Guardian берёт последние известные цены и предупреждает об устаревших данных.
        """
        max_age = 2 * self.settings.collect_interval_s * 1000
        if self.snapshot is None or self.snapshot_at is None or now - self.snapshot_at > max_age:
            return MarketSnapshot(ts=now)
        return self.snapshot

    async def _guard(self) -> None:
        now = self.now_ms()
        positions = self.db.positions({"open", "closing"})
        await guard(positions, self._fresh_snapshot(now), self.broker, self.db, self.notifier, self.settings,
                    self.last_marks, now_ms=now)
        buffers = {}
        for pos in self.db.positions({"open"}):
            values = [leg_buffer(l, self.last_marks[(l.exchange, l.symbol)])
                      for l in pos.legs
                      if l.open and l.kind == "perp" and l.amount > 0 and l.margin > 0
                      and (l.exchange, l.symbol) in self.last_marks]
            if values:
                buffers[str(pos.id)] = min(values)
        self._save_stats(buffers=buffers)

    # --- команды ---
    async def _commands(self) -> None:
        now = self.now_ms()
        await self._expire(now)
        for cmd in self.db.take_new_commands():
            try:
                result = await self._execute(cmd, now)
            except CommandError as exc:
                self.db.finish_command(cmd.id, "error", str(exc))
                log.warning("Команда %s %s отклонена: %s", cmd.type, cmd.arg or "", exc)
            except Exception as exc:  # noqa: BLE001 — сбой одной команды не мешает остальным
                self.db.finish_command(cmd.id, "error", f"ошибка: {exc!r}")
                self.db.audit("command_error", {"id": cmd.id, "type": cmd.type,
                                                "error": repr(exc)})
                log.exception("Команда %s %s завершилась ошибкой", cmd.type, cmd.arg or "")
            else:
                self.db.finish_command(cmd.id, "done", result)
                log.info("Команда %s %s: %s", cmd.type, cmd.arg or "", result)

    async def _execute(self, cmd: Command, now: int) -> str:
        handler = {"approve": self._approve, "reject": self._reject, "close": self._close,
                   "stop_all": self._stop_all, "pause": self._pause,
                   "resume": self._resume}.get(cmd.type)
        if handler is None:
            raise CommandError(f"неизвестная команда «{cmd.type}»")
        return await handler(cmd.arg, now)

    @staticmethod
    def _id(arg: str | None, what: str) -> int:
        try:
            return int(arg or "")
        except ValueError:
            raise CommandError(f"не указан номер {what}: «{arg}»") from None

    def _pending(self, arg: str | None, now: int) -> Proposal:
        pid = self._id(arg, "предложения")
        p = self.db.get_proposal(pid)
        if p is None:
            raise CommandError(f"предложение #{pid} не найдено")
        if p.status == "pending" and p.expires_at <= now:
            p.status = "expired"
            self.db.update_proposal(p)
        if p.status == "expired":
            raise CommandError(f"предложение #{pid} истекло")
        if p.status != "pending":
            raise CommandError(
                f"предложение #{pid} уже в статусе «{_PROPOSAL_STATUS_RU.get(p.status, p.status)}»")
        return p

    async def _approve(self, arg, now: int) -> str:
        p = self._pending(arg, now)
        eq, dpnl = await self._equity(now)
        others = [x for x in self.db.pending_proposals(now) if x.id != p.id]
        capital = min(p.capital, capital_for_pair(eq, self.settings))
        reason = check_limits(p.base, p.profile, capital, self.db.positions(_HELD), others,
                              eq, dpnl, bool(self.db.get_kv("paused", False)), self.settings)
        if reason is None:
            capital = affordable_capital(p.kind, p.legs_plan, capital, await self._balances())
            if capital < MIN_PAIR_CAPITAL:
                reason = NO_BALANCE_REASON
        if reason is not None:
            raise CommandError(f"предложение #{p.id} не исполнено: {reason}")
        if capital < p.capital:
            p.expected_profit = self._expected_profit(p.expected_apr, capital)
            p.capital = capital
        p.status = "approved"
        self.db.update_proposal(p)
        try:
            pos = await self._open_pair(p.legs_plan, p.capital, p.base, p.kind, p.profile)
        except Exception:
            p.status = "failed"  # не оставлять «подтверждено» навсегда
            self.db.update_proposal(p)
            raise
        p.status = "executed" if pos.status == "open" else "failed"
        self.db.update_proposal(p)
        if p.status == "failed":
            return f"предложение #{p.id}: связка {p.base} не открыта (позиция #{pos.id})"
        return f"предложение #{p.id}: связка {p.base} открыта (позиция #{pos.id})"

    async def _reject(self, arg, now: int) -> str:
        p = self._pending(arg, now)
        p.status = "rejected"
        self.db.update_proposal(p)
        return f"предложение #{p.id} отклонено"

    async def _close(self, arg, now: int) -> str:
        pid = self._id(arg, "позиции")
        try:
            pos = self.db.get_position(pid)
        except KeyError:
            raise CommandError(f"позиция #{pid} не найдена") from None
        if pos.status not in ("open", "closing"):
            raise CommandError(f"позиция #{pid} в статусе «{pos.status}», закрывать нечего")
        pos = await close_pair(pos, "закрыто вручную", self.broker, self.db, self.notifier,
                               self.settings)
        return f"позиция #{pid}: статус «{pos.status}»"

    async def _stop_all(self, arg, now: int) -> str:
        self.db.set_kv("paused", True)  # сразу: даже если закрытие упадёт, новых связок не будет
        results = []
        left = 0
        positions = self.db.positions({"open", "closing"})
        for pos in positions:
            pos = await close_pair(pos, "стоп всё", self.broker, self.db, self.notifier,
                                   self.settings)
            results.append(f"#{pos.id} {pos.base}: {pos.status}")
            left += pos.status == "closing"
        summary = f"закрыто {len(positions) - left} из {len(positions)}"
        if left:
            summary += (f", ещё закрывается {left} (Guardian повторяет закрытие каждые "
                        f"{self.settings.guard_interval_s} с)")
        await self.notifier.send(f"Стоп всё: {summary}. Бот на паузе.", "alert")
        return "; ".join(results + [summary, "бот на паузе"])

    async def _pause(self, arg, now: int) -> str:
        self.db.set_kv("paused", True)
        await self.notifier.send("Бот на паузе: новые связки не открываются.", "warn")
        return "бот на паузе"

    async def _resume(self, arg, now: int) -> str:
        self.db.set_kv("paused", False)
        await self.notifier.send("Пауза снята.", "info")
        return "пауза снята"
