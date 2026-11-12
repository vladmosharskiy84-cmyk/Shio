"""Учёт виртуальных выплат funding, equity и дневного PnL."""
from __future__ import annotations

from datetime import datetime, timezone

from .collector import MarketSnapshot
from .models import Leg, Position

_ACCRUING = ("open", "closing")


def accrue(snapshot: MarketSnapshot, positions: list[Position], db, now_ms: int) -> list[tuple[Leg, float]]:
    """Начислить наступившие выплаты по открытым perp-ногам. Возвращает только новые.

    До момента выплаты ставка ноги обновляется из снимка на каждом вызове, пока
    снимок указывает на ту же выплату: начисляется последний прогноз перед выплатой.
    """
    booked: list[tuple[Leg, float]] = []
    for pos in positions:
        if pos.status not in _ACCRUING:
            continue
        changed = False
        for leg in pos.legs:
            if not leg.open or leg.kind != "perp":
                continue
            info = snapshot.perp(leg.exchange, leg.symbol)
            if info is None or info.next_funding_ts is None:
                continue
            if leg.pending_ts is not None and leg.pending_rate is not None and now_ms >= leg.pending_ts:
                mark = info.mark_price if info.mark_price is not None else leg.entry_price
                sign = 1 if leg.side == "long" else -1
                amount = -sign * leg.amount * mark * leg.pending_rate
                if db.add_funding(leg.id, leg.pending_ts, leg.pending_rate, amount):
                    leg.funding_received += amount
                    leg.last_funding_ts = leg.pending_ts
                    booked.append((leg, amount))
                leg.pending_rate, leg.pending_ts = info.funding_rate, info.next_funding_ts
                changed = True
            elif (leg.pending_ts is not None and info.next_funding_ts == leg.pending_ts
                  and now_ms < leg.pending_ts and info.funding_rate is not None):
                # до выплаты биржа уточняет прогноз ставки: начислим по последнему
                if info.funding_rate != leg.pending_rate:
                    leg.pending_rate = info.funding_rate
                    changed = True
            elif leg.pending_ts is None or leg.pending_rate is None:
                leg.pending_rate, leg.pending_ts = info.funding_rate, info.next_funding_ts
                changed = True
        if changed:
            db.update_position(pos)
    return booked


def equity(db, broker_balances: dict[str, float], positions: list[Position],
           snapshot: MarketSnapshot) -> float:
    """Свободные балансы + стоимость открытых ног (спот по цене, perp = маржа + нереализ. PnL)."""
    total = sum(broker_balances.values())
    for pos in positions:
        for leg in pos.legs:
            if not leg.open:
                continue
            if leg.kind == "spot":
                price = next((s.price for s in snapshot.spots
                              if s.exchange == leg.exchange and s.symbol == leg.symbol
                              and s.price is not None), leg.entry_price)
                total += leg.amount * price
            else:
                info = snapshot.perp(leg.exchange, leg.symbol)
                mark = info.mark_price if info and info.mark_price is not None else leg.entry_price
                diff = mark - leg.entry_price if leg.side == "long" else leg.entry_price - mark
                total += leg.margin + diff * leg.amount
    return total


def day_pnl(db, now_ms: int, equity_now: float) -> float:
    """equity_now минус equity на начало текущих суток UTC (первый вызов суток фиксирует базу)."""
    day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    start = db.get_kv("day_start_equity")
    if not start or start.get("day") != day:
        db.set_kv("day_start_equity", {"day": day, "equity": equity_now})
        return 0.0
    return equity_now - start["equity"]
