"""Сверка позиций в базе с тем, что реально держит брокер."""
from __future__ import annotations

import time

from engine.executor import close_pair
from engine.models import Position

_EPS = 1e-9
R_INCOMPLETE = "незавершённое открытие"
R_NOT_FOUND = "не найдена на бирже"
R_LEG_NOT_FOUND = "нога не найдена на бирже"
R_LIQUIDATED = "ликвидация"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(a), abs(b)) * _EPS + _EPS


def _broker_amount(held: dict[str, dict[str, float]], leg) -> float:
    return abs(held.get(leg.exchange, {}).get(leg.symbol, 0.0))


def _sync_legs(pos: Position, held) -> bool:
    """Брокер — источник истины по объёму ног. Возвращает True, если что-то изменилось."""
    changed = False
    for leg in pos.legs:
        amount = _broker_amount(held, leg)
        if _same(leg.amount, amount) and leg.open == (amount > _EPS):
            continue
        if leg.amount > 0 and amount > 0:
            leg.margin = leg.margin * amount / leg.amount
        leg.amount = amount
        leg.open = amount > _EPS
        if not leg.open:
            leg.amount = 0.0
            leg.margin = 0.0
        changed = True
    return changed


def _zero_legs(pos: Position) -> None:
    for leg in pos.legs:
        leg.amount = 0.0
        leg.margin = 0.0
        leg.open = False


def _legs_desc(pos: Position) -> str:
    return ", ".join(f"{l.exchange} {l.symbol}" for l in pos.legs)


async def reconcile(broker, db, notifier, settings) -> list[str]:
    """Сверяет позиции opening/open/closing с брокером; возвращает расхождения (по-русски)."""
    issues: list[str] = []
    held: dict[str, dict[str, float]] = {}
    failed: set[str] = set()
    for name, cfg in settings.exchanges.items():
        if not cfg.trade:
            continue
        try:
            held[name] = await broker.open_amounts(name)
        except Exception as exc:  # noqa: BLE001 — сбой одной биржи не должен останавливать сверку
            failed.add(name)
            text = f"не удалось сверить {name}: {exc}"
            issues.append(text)
            db.audit("reconcile_error", {"exchange": name, "error": repr(exc)})
            await notifier.send(text, "warn")

    positions = db.positions({"opening", "open", "closing"})
    known = {(l.exchange, l.symbol) for p in positions for l in p.legs}

    for ex, amounts in held.items():
        for symbol, amount in amounts.items():
            if abs(amount) > _EPS and (ex, symbol) not in known:
                text = f"неизвестная позиция {symbol} на {ex}: объём {amount:g}"
                issues.append(text)
                db.audit("reconcile_unknown", {"exchange": ex, "symbol": symbol, "amount": amount})
                await notifier.send(text, "warn")

    for pos in positions:
        if any(l.exchange not in held for l in pos.legs):  # биржа не опрошена или недоступна
            continue
        handler = {"opening": _opening, "closing": _closing, "open": _open}[pos.status]
        await handler(pos, held, broker, db, notifier, settings, issues)
    return issues


async def _repair(pos, db, notifier, issues, event, text, level, **detail) -> None:
    issues.append(text)
    db.audit(event, {"position_id": pos.id, "base": pos.base, "status": pos.status, **detail})
    await notifier.send(text, level)


async def _opening(pos, held, broker, db, notifier, settings, issues) -> None:
    _sync_legs(pos, held)
    amounts = [l.amount for l in pos.legs]
    if all(a > 0 for a in amounts) and all(_same(a, amounts[0]) for a in amounts):
        pos.status = "open"
        db.update_position(pos)
        await _repair(pos, db, notifier, issues, "reconcile_repair",
                      f"Связка {pos.base}: открытие было прервано, но обе ноги на месте — "
                      f"статус «открыта».", "info", action="promoted_to_open")
        return
    text = (f"Связка {pos.base}: найдено незавершённое открытие ({_legs_desc(pos)}), "
            f"закрываю оставшееся.")
    if any(a > 0 for a in amounts):
        pos = await close_pair(pos, R_INCOMPLETE, broker, db, notifier, settings)
        if pos.status == "closed":
            pos.status = "failed"
            pos.close_reason = R_INCOMPLETE
    else:
        _zero_legs(pos)
        pos.status = "failed"
        pos.close_reason = R_INCOMPLETE
        pos.closed_at = _now_ms()
    db.update_position(pos)
    await _repair(pos, db, notifier, issues, "reconcile_repair", text, "alert",
                  action="closed_orphan", result=pos.status)


async def _closing(pos, held, broker, db, notifier, settings, issues) -> None:
    _sync_legs(pos, held)
    if any(l.amount > 0 for l in pos.legs):
        reason = pos.close_reason or "закрытие после перезапуска"
        text = f"Связка {pos.base}: закрытие не было завершено, закрываю остаток."
        pos = await close_pair(pos, reason, broker, db, notifier, settings)
        if pos.status == "closed" and reason == R_LIQUIDATED:
            pos.status = "liquidated"
        db.update_position(pos)
        await _repair(pos, db, notifier, issues, "reconcile_repair", text, "alert",
                      action="retry_close", result=pos.status)
        return
    _zero_legs(pos)
    pos.status = "liquidated" if pos.close_reason == R_LIQUIDATED else "closed"
    pos.closed_at = pos.closed_at or _now_ms()
    db.update_position(pos)
    await _repair(pos, db, notifier, issues, "reconcile_repair",
                  f"Связка {pos.base}: у брокера ничего не осталось, закрытие завершено.",
                  "info", action="close_finalized", result=pos.status)


async def _open(pos, held, broker, db, notifier, settings, issues) -> None:
    missing = [l for l in pos.legs if l.open and _broker_amount(held, l) <= _EPS]
    if not missing:
        return
    if len(missing) == len([l for l in pos.legs if l.open]):
        _zero_legs(pos)
        pos.status = "closed"
        pos.close_reason = R_NOT_FOUND
        pos.closed_at = _now_ms()
        db.update_position(pos)
        await _repair(pos, db, notifier, issues, "reconcile_repair",
                      f"Связка {pos.base}: не найдена на бирже ({_legs_desc(pos)}), помечена закрытой.",
                      "alert", action="marked_closed")
        return
    for leg in missing:
        leg.amount, leg.margin, leg.open = 0.0, 0.0, False
    for leg in pos.legs:  # оставшиеся ноги — по объёму брокера
        if leg.open:
            leg.amount = _broker_amount(held, leg)
    pos = await close_pair(pos, R_LEG_NOT_FOUND, broker, db, notifier, settings)
    await _repair(pos, db, notifier, issues, "reconcile_repair",
                  f"Связка {pos.base}: на бирже нет ноги "
                  f"{', '.join(f'{l.exchange} {l.symbol}' for l in missing)}, закрываю остальное.",
                  "alert", action="closed_rest", result=pos.status)
