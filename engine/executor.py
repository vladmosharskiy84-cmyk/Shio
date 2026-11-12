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
    complete: bool = False  # запрошенный объём исполнен (остаток не торгуется)

    @property
    def avg(self) -> float:
        return self.notional / self.filled if self.filled else 0.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _open_side(leg: Leg) -> str:
    return "buy" if leg.side == "long" else "sell"


def _close_side(leg: Leg) -> str:
    return "sell" if leg.side == "long" else "buy"


def _margin(leg: Leg) -> float:
    notional = leg.amount * leg.entry_price
    return notional / leg.leverage if leg.kind == "perp" else notional


def _price_pnl(leg: Leg, exit_price: float, amount: float) -> float:
    if leg.side == "long":
        return (exit_price - leg.entry_price) * amount
    return (leg.entry_price - exit_price) * amount


def _add_fill(pos: Position, leg: Leg, fill) -> None:
    """Увеличение ноги: средняя цена входа считается по всем исполнениям."""
    total = leg.amount + fill.filled
    leg.entry_price = (leg.entry_price * leg.amount + fill.avg_price * fill.filled) / total
    leg.amount = total
    leg.fees_paid += fill.fee
    leg.margin = _margin(leg)


def _reduce_fill(pos: Position, leg: Leg, fill) -> None:
    """Уменьшение ноги: P&L цены сразу попадает в realized_pnl.

    Маржа уменьшается пропорционально объёму, чтобы довнесённая маржа не терялась.
    """
    pos.realized_pnl += _price_pnl(leg, fill.avg_price, fill.filled)
    before = leg.amount
    leg.amount = max(leg.amount - fill.filled, 0.0)
    leg.fees_paid += fill.fee
    leg.margin = leg.margin * leg.amount / before if before > 0 else 0.0


async def _fill_up(broker, db, pos: Position, leg: Leg, side: str, amount: float, settings,
                   on_fill, attempts: int | None = None) -> _Result:
    """Исполняет amount рыночными IOC: до attempts (по умолчанию leg_retries) попыток на остаток.

    Остаток округляется через broker.round_amount; если он округлился до 0 — стоп.
    Исключение брокера считается неудачной попыткой. После каждого исполнения
    on_fill меняет ногу, и позиция сразу сохраняется в базе.
    """
    res = _Result()

    def tiny() -> bool:
        return amount - res.filled <= amount * _EPS

    for _ in range(attempts or settings.limits.leg_retries):
        if tiny():
            res.complete = True
            return res
        try:
            qty = await broker.round_amount(leg.exchange, leg.symbol, amount - res.filled)
            if qty <= 0:
                res.complete = True
                return res
            fill = await broker.market_ioc(leg.exchange, leg.symbol, leg.kind, side, qty,
                                           settings.limits.max_slippage)
        except Exception as exc:  # noqa: BLE001 — любая ошибка брокера = неудачная попытка
            db.audit("order_error", {"position_id": pos.id, "exchange": leg.exchange,
                                     "symbol": leg.symbol, "side": side, "error": repr(exc)})
            continue
        if fill.filled > 0:
            res.filled += fill.filled
            res.notional += fill.filled * fill.avg_price
            res.fee += fill.fee
            on_fill(fill)
            db.update_position(pos)
    res.complete = res.complete or tiny()
    return res


async def _finish_failed(pos: Position, db, notifier, text: str, level: str, detail: dict) -> Position:
    pos.status = "failed"
    pos.closed_at = _now_ms()
    db.update_position(pos)
    db.audit("open_failed", {"position_id": pos.id, "base": pos.base, **detail})
    await notifier.send(text, level)
    return pos


async def _shrink(pos, leg, extra, broker, db, settings) -> _Result:
    """Встречный ордер, уменьшающий ногу на extra (общее ядро open_pair и reduce_leg)."""
    return await _fill_up(broker, db, pos, leg, _close_side(leg), extra, settings,
                          lambda f: _reduce_fill(pos, leg, f))


async def reduce_leg(position: Position, leg: Leg, amount: float, broker, db, notifier,
                     settings) -> float:
    """Уменьшает открытую ногу на amount встречными IOC (до leg_retries попыток).

    P&L цены идёт в position.realized_pnl, комиссия — в leg.fees_paid, позиция
    сохраняется после каждого исполнения. Возвращает фактически сокращённый объём.
    Если сократить полностью не удалось — запись в журнале и тревога.
    """
    res = await _shrink(position, leg, amount, broker, db, settings)
    db.audit("leg_reduced", {"position_id": position.id, "exchange": leg.exchange,
                             "symbol": leg.symbol, "requested": amount, "reduced": res.filled})
    if not res.complete:
        await notifier.send(
            f"Связка {position.base}: не удалось сократить ногу на {leg.exchange} "
            f"на {amount - res.filled:g} (сокращено {res.filled:g}). Нужна проверка.", "alert")
    return res.filled


async def open_pair(plan: list[dict], capital: float, base: str, kind: str, profile: str,
                    broker, db, notifier, settings) -> Position:
    lim = settings.limits
    legs = [Leg(None, None, p["exchange"], p["symbol"], p["kind"], p["side"], 0.0, 0.0,
                1.0 if p["kind"] == "spot" else lim.max_leverage, 0.0) for p in plan]
    pos = Position(None, kind, base, profile, "opening", capital, _now_ms(), legs=legs)  # type: ignore[arg-type]

    notional = (perp_perp_notional if kind == "perp-perp" else spot_perp_notional)(
        capital, lim.max_leverage)
    amounts = []
    try:
        for leg in legs:
            price = await broker.ref_price(leg.exchange, leg.symbol, _open_side(leg))
            amounts.append(0.0 if price <= 0 else
                           await broker.round_amount(leg.exchange, leg.symbol, notional / price))
    except Exception as exc:  # noqa: BLE001
        db.save_position(pos)
        return await _finish_failed(pos, db, notifier,
                                    f"Связка {base} не открыта: ошибка получения цены ({exc}).",
                                    "warning", {"reason": "sizing_error", "error": repr(exc)})
    amount = min(amounts)

    db.save_position(pos)
    if amount <= 0:
        return await _finish_failed(
            pos, db, notifier,
            f"Связка {base} не открыта: нет цены или недостаточно ликвидности в стакане.",
            "warning", {"reason": "no_price_or_liquidity"})
    db.audit("opening", {"position_id": pos.id, "base": base, "kind": kind, "amount": amount,
                         "plan": plan})

    try:
        for leg in legs:
            if leg.kind == "perp":
                await broker.set_leverage(leg.exchange, leg.symbol, lim.max_leverage)
    except Exception as exc:  # noqa: BLE001
        return await _finish_failed(pos, db, notifier,
                                    f"Связка {base} не открыта: не удалось выставить плечо ({exc}).",
                                    "warning", {"reason": "leverage_error", "error": repr(exc)})

    first, second = legs[0], legs[1]
    r1 = await _fill_up(broker, db, pos, first, _open_side(first), amount, settings,
                        lambda f: _add_fill(pos, first, f), attempts=1)
    if r1.filled <= 0:
        return await _finish_failed(
            pos, db, notifier,
            f"Связка {base} не открыта: недостаточно ликвидности, первая нога ({first.exchange}) не исполнена.",
            "warning", {"reason": "недостаточно ликвидности"})

    await _fill_up(broker, db, pos, second, _open_side(second), first.amount, settings,
                   lambda f: _add_fill(pos, second, f))
    if second.amount <= 0:
        return await _unwind_first(pos, first, second, broker, db, notifier, settings)

    # выравнивание: уменьшаем ту ногу, где объём больше
    if abs(first.amount - second.amount) > max(first.amount, second.amount) * _EPS:
        big, small = (first, second) if first.amount > second.amount else (second, first)
        extra = big.amount - small.amount
        rc = await _shrink(pos, big, extra, broker, db, settings)
        if not rc.complete:
            pos.status = "open"
            db.update_position(pos)
            db.audit("shrink_incomplete", {"position_id": pos.id, "left": extra - rc.filled})
            await notifier.send(
                f"Связка {base}: ноги не выровнены, не удалось сократить ногу на {big.exchange} "
                f"на {extra - rc.filled:g}. Нужна проверка.", "alert")
            return pos
        db.audit("shrunk_leg", {"position_id": pos.id, "exchange": big.exchange,
                                "reduced": rc.filled})

    pos.status = "open"
    db.update_position(pos)
    db.audit("opened", {"position_id": pos.id, "base": base,
                        "amounts": [leg.amount for leg in legs]})
    await notifier.send(f"Связка {base} открыта: объём {first.amount:g} на каждой ноге.", "info")
    return pos


async def _unwind_first(pos, first, second, broker, db, notifier, settings) -> Position:
    second.open = False
    amount = first.amount
    rc = await _shrink(pos, first, amount, broker, db, settings)
    if not rc.complete:
        pos.status = "closing"
        db.update_position(pos)
        db.audit("unwind_incomplete", {"position_id": pos.id, "left": first.amount})
        await notifier.send(
            f"Связка {pos.base}: вторая нога не исполнена, первую ногу закрыть не удалось "
            f"(осталось {first.amount:g} на {first.exchange}). Закройте вручную!", "alert")
        return pos
    first.amount = 0.0
    first.margin = 0.0
    first.open = False
    pos.realized_pnl -= sum(l.fees_paid for l in pos.legs)
    return await _finish_failed(
        pos, db, notifier,
        f"Связка {pos.base}: вторая нога не исполнена, закрыта первая нога ({first.exchange}). "
        f"Связка не открыта.", "alert", {"reason": "second_leg_not_filled", "unwound": amount})


async def close_pair(position: Position, reason: str, broker, db, notifier, settings) -> Position:
    pos = position
    pos.status = "closing"
    db.update_position(pos)
    db.audit("closing", {"position_id": pos.id, "reason": reason})

    for leg in reversed(pos.legs):
        if not leg.open:
            continue
        if leg.amount > 0:
            res = await _fill_up(broker, db, pos, leg, _close_side(leg), leg.amount, settings,
                                 lambda f, leg=leg: _reduce_fill(pos, leg, f))
            if not res.complete:
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
