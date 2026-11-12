"""Исполнитель двух ног: открытие и закрытие связки без «одноногих» остатков."""
from __future__ import annotations

import time
from dataclasses import dataclass

from engine.models import Leg, Position
from scanner.calc import perp_perp_notional, spot_perp_notional

_EPS = 1e-9


@dataclass
class _Result:
    filled: float = 0.0
    notional: float = 0.0
    fee: float = 0.0

    @property
    def avg(self) -> float:
        return self.notional / self.filled if self.filled else 0.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _open_side(leg: Leg) -> str:
    return "buy" if leg.side == "long" else "sell"


def _close_side(leg: Leg) -> str:
    return "sell" if leg.side == "long" else "buy"


async def _fill_up(broker, leg: Leg, side: str, amount: float, settings,
                   attempts: int | None = None) -> _Result:
    """Исполняет amount рыночными IOC-ордерами: до attempts (по умолчанию leg_retries) попыток на остаток."""
    res = _Result()
    for _ in range(attempts or settings.limits.leg_retries):
        remaining = amount - res.filled
        if remaining <= amount * _EPS:
            break
        fill = await broker.market_ioc(leg.exchange, leg.symbol, leg.kind, side, remaining,
                                       settings.limits.max_slippage)
        if fill.filled > 0:
            res.filled += fill.filled
            res.notional += fill.filled * fill.avg_price
            res.fee += fill.fee
    return res


def _apply_open(leg: Leg, res: _Result) -> None:
    leg.amount = res.filled
    leg.entry_price = res.avg
    leg.fees_paid += res.fee
    notional = res.filled * res.avg
    leg.margin = notional / leg.leverage if leg.kind == "perp" else notional


def _price_pnl(leg: Leg, exit_price: float, amount: float) -> float:
    if leg.side == "long":
        return (exit_price - leg.entry_price) * amount
    return (leg.entry_price - exit_price) * amount


async def _finish_failed(pos: Position, db, notifier, text: str, level: str, detail: dict) -> Position:
    pos.status = "failed"
    pos.closed_at = _now_ms()
    db.update_position(pos)
    db.audit("open_failed", {"position_id": pos.id, "base": pos.base, **detail})
    await notifier.send(text, level)
    return pos


async def open_pair(plan: list[dict], capital: float, base: str, kind: str, profile: str,
                    broker, db, notifier, settings) -> Position:
    lim = settings.limits
    now = _now_ms()
    legs = [Leg(None, None, p["exchange"], p["symbol"], p["kind"], p["side"], 0.0, 0.0,
                1.0 if p["kind"] == "spot" else lim.max_leverage, 0.0) for p in plan]
    pos = Position(None, kind, base, profile, "opening", capital, now, legs=legs)  # type: ignore[arg-type]

    notional = (perp_perp_notional if kind == "perp-perp" else spot_perp_notional)(
        capital, lim.max_leverage)
    amounts = []
    for leg in legs:
        price = await broker.ref_price(leg.exchange, leg.symbol, _open_side(leg))
        if price <= 0:
            amounts.append(0.0)
        else:
            amounts.append(await broker.round_amount(leg.exchange, leg.symbol, notional / price))
    amount = min(amounts)

    db.save_position(pos)
    if amount <= 0:
        return await _finish_failed(
            pos, db, notifier,
            f"Связка {base} не открыта: нет цены или недостаточно ликвидности в стакане.",
            "warning", {"reason": "no_price_or_liquidity"})
    db.audit("opening", {"position_id": pos.id, "base": base, "kind": kind, "amount": amount,
                         "plan": plan})

    for leg in legs:
        if leg.kind == "perp":
            await broker.set_leverage(leg.exchange, leg.symbol, lim.max_leverage)

    first, second = legs[0], legs[1]
    r1 = await _fill_up(broker, first, _open_side(first), amount, settings, attempts=1)
    if r1.filled <= 0:
        return await _finish_failed(
            pos, db, notifier,
            f"Связка {base} не открыта: первая нога ({first.exchange}) не исполнена.",
            "warning", {"reason": "first_leg_not_filled"})
    _apply_open(first, r1)
    db.update_position(pos)

    r2 = await _fill_up(broker, second, _open_side(second), r1.filled, settings)
    if r2.filled > 0:
        _apply_open(second, r2)
    if r2.filled <= 0:
        return await _unwind_first(pos, first, second, broker, db, notifier, settings)

    if r2.filled < r1.filled - r1.filled * _EPS:
        extra = r1.filled - r2.filled
        rc = await _fill_up(broker, first, _close_side(first), extra, settings)
        first.amount -= rc.filled
        first.fees_paid += rc.fee
        first.margin = first.amount * first.entry_price / (first.leverage if first.kind == "perp" else 1.0)
        if rc.filled < extra - extra * _EPS:
            pos.status = "open"
            db.update_position(pos)
            db.audit("shrink_incomplete", {"position_id": pos.id, "left": extra - rc.filled})
            await notifier.send(
                f"Связка {base}: ноги не выровнены, не удалось сократить первую ногу "
                f"на {extra - rc.filled:g}. Нужна проверка.", "alert")
            return pos
        db.audit("shrunk_first_leg", {"position_id": pos.id, "reduced": rc.filled})

    pos.status = "open"
    db.update_position(pos)
    db.audit("opened", {"position_id": pos.id, "base": base,
                        "amounts": [leg.amount for leg in legs]})
    await notifier.send(f"Связка {base} открыта: объём {first.amount:g} на каждой ноге.", "info")
    return pos


async def _unwind_first(pos, first, second, broker, db, notifier, settings) -> Position:
    amount = first.amount
    rc = await _fill_up(broker, first, _close_side(first), amount, settings)
    first.fees_paid += rc.fee
    left = amount - rc.filled
    pos.realized_pnl += (_price_pnl(first, rc.avg, rc.filled) if rc.filled else 0.0) - first.fees_paid
    if left > amount * _EPS:
        first.amount = left
        first.margin = left * first.entry_price / (first.leverage if first.kind == "perp" else 1.0)
        text = (f"Связка {pos.base}: вторая нога не исполнена, первую ногу закрыть не удалось "
                f"(осталось {left:g} на {first.exchange}). Закройте вручную!")
    else:
        first.open = False
        first.amount = 0.0
        first.margin = 0.0
        text = (f"Связка {pos.base}: вторая нога не исполнена, закрыта первая нога "
                f"({first.exchange}). Связка не открыта.")
    second.open = False
    return await _finish_failed(pos, db, notifier, text, "alert",
                                {"reason": "second_leg_not_filled", "unwound": amount - left,
                                 "left": left})


async def close_pair(position: Position, reason: str, broker, db, notifier, settings) -> Position:
    pos = position
    pos.status = "closing"
    db.update_position(pos)
    db.audit("closing", {"position_id": pos.id, "reason": reason})

    for leg in reversed(pos.legs):
        if not leg.open or leg.amount <= 0:
            continue
        orig = leg.amount
        res = await _fill_up(broker, leg, _close_side(leg), orig, settings)
        leg.fees_paid += res.fee
        if res.filled > 0:
            pos.realized_pnl += _price_pnl(leg, res.avg, res.filled)
            leg.amount -= res.filled
            leg.margin = leg.amount * leg.entry_price / (leg.leverage if leg.kind == "perp" else 1.0)
        if leg.amount > orig * _EPS:
            db.update_position(pos)
            db.audit("close_incomplete", {"position_id": pos.id, "exchange": leg.exchange,
                                          "left": leg.amount})
            await notifier.send(
                f"Связка {pos.base}: не удалось полностью закрыть ногу на {leg.exchange} "
                f"(осталось {leg.amount:g}). Позиция остаётся в статусе «закрывается».", "alert")
            return pos
        leg.amount = 0.0
        leg.margin = 0.0
        leg.open = False
        db.update_position(pos)

    pos.realized_pnl += (sum(l.funding_received for l in pos.legs)
                         - sum(l.fees_paid for l in pos.legs))
    pos.status = "closed"
    pos.closed_at = _now_ms()
    pos.close_reason = reason
    db.update_position(pos)
    db.audit("closed", {"position_id": pos.id, "reason": reason, "realized_pnl": pos.realized_pnl})
    await notifier.send(f"Связка {pos.base} закрыта ({reason}). Итог: {pos.realized_pnl:+.2f} USD.", "info")
    return pos
