"""Guardian: каждые 30 с защищает открытые связки.

Порядок проверок для позиции в статусе «open»: цены ног → цена за ликвидацией →
запас ≤ act_buffer (довнести маржу, иначе закрыть) → запас ≤ warn_buffer →
перекос объёмов ног → разворот ставки.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from engine.collector import MarketSnapshot
from engine.executor import close_pair, reduce_leg
from engine.models import Leg, Position
from scanner.calc import liquidation_price, margin_buffer

_HOUR_MS = 3_600_000
_TOPUP_TARGET = 0.5  # запас, к которому возвращает доливка маржи
_MMR = 0.005         # ставка поддерживающей маржи, как в liquidation_price по умолчанию

ActionKind = Literal["ok", "warn", "topup", "close", "propose_close", "rebalance",
                     "liquidated", "stale"]


@dataclass
class GuardAction:
    position_id: int
    kind: ActionKind
    detail: str


def _now_ms() -> int:
    return int(time.time() * 1000)


def _once_per_hour(db, key: str, now_ms: int) -> bool:
    """True не чаще раза в час на ключ; отметка времени хранится в kv."""
    last = db.get_kv(key)
    if last is not None and now_ms - last < _HOUR_MS:
        return False
    db.set_kv(key, now_ms)
    return True


def _snapshot_mark(snapshot: MarketSnapshot, leg: Leg) -> float | None:
    if leg.kind == "perp":
        info = snapshot.perp(leg.exchange, leg.symbol)
        price = info.mark_price if info is not None else None
    else:
        price = next((s.price for s in snapshot.spots
                      if s.exchange == leg.exchange and s.symbol == leg.symbol), None)
    return price if price is not None and price > 0 else None


def leg_buffer(leg: Leg, mark: float) -> float:
    """Запас до ликвидации perp-ноги с учётом фактической (довнесённой) маржи."""
    lev_eff = leg.entry_price * leg.amount / leg.margin
    liq = liquidation_price(leg.entry_price, leg.side, lev_eff)
    return margin_buffer(leg.entry_price, mark, liq)


def _topup_amount(leg: Leg, mark: float) -> float:
    """Сколько USD маржи довнести, чтобы запас при текущей цене стал 50%."""
    target_liq = 2 * mark - leg.entry_price  # (liq - mark) / (liq - entry) = 0.5
    if leg.side == "short":
        inv_lev = target_liq / leg.entry_price - 1 + _MMR
    else:
        inv_lev = 1 + _MMR - target_liq / leg.entry_price
    needed = leg.entry_price * leg.amount * inv_lev
    return needed - leg.margin


def _perp_legs(pos: Position) -> list[Leg]:
    return [l for l in pos.legs if l.open and l.kind == "perp" and l.amount > 0 and l.margin > 0]


async def guard(positions: list[Position], snapshot: MarketSnapshot, broker, db, notifier,
                settings, last_marks: dict[tuple[str, str], float],
                now_ms: int | None = None) -> list[GuardAction]:
    now = _now_ms() if now_ms is None else now_ms
    actions: list[GuardAction] = []
    for pos in positions:
        if pos.status != "open":
            continue
        try:
            actions += await _guard_position(pos, snapshot, broker, db, notifier, settings,
                                             last_marks, now)
        except Exception as exc:  # noqa: BLE001 — сбой одной связки не должен оставить без защиты остальные
            actions.append(GuardAction(pos.id, "stale", f"ошибка проверки: {exc!r}"))
            try:
                db.audit("guard_error", {"position_id": pos.id, "error": repr(exc)})
            except Exception:  # noqa: BLE001
                pass
            try:
                await notifier.send(
                    f"Связка {pos.base}: ошибка проверки Guardian ({exc!r}). Проверьте позицию.",
                    "alert")
            except Exception:  # noqa: BLE001
                pass
    return actions


class _ThrottledNotifier:
    """Пропускает уведомления не чаще раза в час на ключ kv."""

    def __init__(self, notifier, db, key: str, now_ms: int):
        self.notifier, self.db, self.key, self.now_ms = notifier, db, key, now_ms

    async def send(self, text: str, level: str = "info") -> None:
        if _once_per_hour(self.db, self.key, self.now_ms):
            await self.notifier.send(text, level)


async def _guard_position(pos, snapshot, broker, db, notifier, settings, last_marks,
                          now) -> list[GuardAction]:
    lim = settings.limits
    out: list[GuardAction] = []

    def act(kind: ActionKind, detail: str) -> None:
        out.append(GuardAction(pos.id, kind, detail))

    # 1. цены ног: снимок, иначе последняя известная
    marks: dict[int, float] = {}  # id(leg) -> цена
    stale: list[str] = []
    missing: list[str] = []
    no_margin: list[str] = []
    for leg in pos.legs:
        if not leg.open:
            continue
        key = (leg.exchange, leg.symbol)
        name = f"{leg.symbol} на {leg.exchange}"
        mark = _snapshot_mark(snapshot, leg)
        if mark is not None:
            last_marks[key] = mark
        else:
            mark = last_marks.get(key)
            (stale if mark is not None else missing).append(name)
        if mark is not None:
            marks[id(leg)] = mark
        if leg.kind == "perp" and leg.amount > 0 and leg.margin <= 0:
            no_margin.append(name)
    problems = []
    if missing:
        problems.append("нет цены: " + ", ".join(missing))
    if stale:
        problems.append("данные устарели, используется последняя известная цена: " + ", ".join(stale))
    if no_margin:
        problems.append("не указана маржа, запас до ликвидации не считается: " + ", ".join(no_margin))
    if problems:
        act("stale", "; ".join(problems))
        if _once_per_hour(db, f"guard:stale:{pos.id}", now):
            await notifier.send(f"Связка {pos.base}: " + "; ".join(problems) + ".", "warning")

    def buffers() -> list[tuple[Leg, float, float]]:
        return [(l, marks[id(l)], leg_buffer(l, marks[id(l)]))
                for l in _perp_legs(pos) if id(l) in marks]

    # 2. цена уже за ликвидацией
    gone = [(l, m) for l, m, b in buffers() if b <= 0]
    if gone:
        await _liquidated(pos, gone, broker, db, notifier, settings)
        act("liquidated", "ликвидация: " + ", ".join(f"{l.symbol} на {l.exchange}" for l, _ in gone))
        return out

    # 3. запас ≤ act_buffer: довнести маржу, иначе закрыть
    for leg, mark, buf in buffers():
        if buf > lim.act_buffer:
            continue
        usd = _topup_amount(leg, mark)
        try:
            ok = await broker.add_margin(leg.exchange, leg.symbol, usd)
        except Exception as exc:  # noqa: BLE001 — ошибка брокера = доливка не удалась
            db.audit("margin_topup_error", {"position_id": pos.id, "exchange": leg.exchange,
                                            "error": repr(exc)})
            ok = False
        if not ok:
            db.audit("guard_close", {"position_id": pos.id, "reason": "угроза ликвидации",
                                     "buffer": buf, "topup_failed": usd})
            await close_pair(pos, "угроза ликвидации", broker, db, notifier, settings)
            act("close", f"угроза ликвидации: запас {buf:.0%}, довнести {usd:.2f} USD не удалось")
            return out
        leg.margin += usd
        db.update_position(pos)
        db.audit("margin_topup", {"position_id": pos.id, "exchange": leg.exchange,
                                  "symbol": leg.symbol, "usd": usd, "buffer": buf,
                                  "margin": leg.margin})
        await notifier.send(
            f"Связка {pos.base}: запас до ликвидации {buf:.0%} на {leg.exchange}, "
            f"довнесена маржа {usd:.2f} USD.", "warning")
        act("topup", f"довнесено {usd:.2f} USD на {leg.exchange}, запас был {buf:.0%}")

    # 4. запас ≤ warn_buffer: предупреждение (уведомление не чаще раза в час)
    low = [(l, b) for l, _, b in buffers() if b <= lim.warn_buffer]
    if low:
        worst_leg, worst = min(low, key=lambda x: x[1])
        act("warn", f"запас до ликвидации {worst:.0%} на {worst_leg.exchange}")
        if _once_per_hour(db, f"guard:warn:{pos.id}", now):
            await notifier.send(
                f"Связка {pos.base}: запас до ликвидации {worst:.0%} на {worst_leg.exchange}.",
                "warning")

    # 5. перекос объёмов ног (только когда у обеих ног есть цена)
    open_legs = [l for l in pos.legs if l.open]
    if len(open_legs) == 2 and all(id(l) in marks for l in open_legs):
        a, b = open_legs
        top = max(a.amount, b.amount)
        if top > 0 and abs(a.amount - b.amount) / top > lim.max_drift:
            big = a if a.amount > b.amount else b
            diff = abs(a.amount - b.amount)
            quiet = _ThrottledNotifier(notifier, db, f"guard:rebalance:{pos.id}", now)
            got = await reduce_leg(pos, big, diff, broker, db, quiet, settings)
            act("rebalance", f"нога на {big.exchange} сокращена на {got:g} из {diff:g}")

    # 6. разворот ставки: ожидаемый доход за 24 ч
    income = 0.0
    for leg in pos.legs:
        if not leg.open or leg.kind != "perp":
            continue
        info = snapshot.perp(leg.exchange, leg.symbol)
        if (info is None or info.funding_rate is None or not info.interval_h
                or id(leg) not in marks):
            return out or [GuardAction(pos.id, "ok", "разворот не проверен: нет ставки")]
        sign = 1 if leg.side == "long" else -1
        income += -sign * (info.funding_rate / info.interval_h) * 24 * leg.amount * marks[id(leg)]
    if income < 0:
        reason = "ставка развернулась"
        if settings.mode == "auto":
            db.audit("guard_close", {"position_id": pos.id, "reason": reason, "income_24h": income})
            await close_pair(pos, reason, broker, db, notifier, settings)
            act("close", f"{reason}: ожидаемый доход за 24 ч {income:+.2f} USD")
        else:
            act("propose_close", f"{reason}: ожидаемый доход за 24 ч {income:+.2f} USD")
            if _once_per_hour(db, f"guard:propose:{pos.id}", now):
                await notifier.send(
                    f"Связка {pos.base}: ставка развернулась, ожидаемый доход за 24 ч "
                    f"{income:+.2f} USD. Предлагаю закрыть связку.", "warning")

    return out or [GuardAction(pos.id, "ok", "всё в порядке")]


async def _liquidated(pos, gone, broker, db, notifier, settings) -> None:
    """Цена перепрыгнула ликвидацию: нога потеряна, остальное закрываем, бот на паузу.

    Если остальные ноги закрыть не удалось, позиция остаётся «закрывается» (R18).
    """
    lost = 0.0
    for leg, mark in gone:
        leg_lost = await broker.on_liquidated(leg.exchange, leg.symbol)
        lost += leg_lost
        db.audit("liquidated", {"position_id": pos.id, "exchange": leg.exchange,
                                "symbol": leg.symbol, "mark": mark, "lost": leg_lost})
        leg.amount = 0.0
        leg.margin = 0.0
        leg.open = False
    pos.realized_pnl -= lost  # до close_pair: его итог в уведомлении и журнале уже с потерей
    db.update_position(pos)
    await close_pair(pos, "ликвидация", broker, db, notifier, settings)
    if pos.status == "closed":
        pos.status = "liquidated"
    db.update_position(pos)
    db.set_kv("paused", True)
    await notifier.send(
        f"Связка {pos.base}: позиция ликвидирована, потеря маржи {lost:.2f} USD. "
        f"Бот поставлен на паузу.", "alert")
