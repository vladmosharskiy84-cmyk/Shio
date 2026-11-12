"""Чистая математика funding-арбитража. Без сети — легко тестировать.

Знак ставки: funding > 0 — лонги платят шортам, funding < 0 — шорты платят лонгам.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_INTERVAL_H = 8.0


@dataclass
class PerpInfo:
    exchange: str
    base: str                  # BTC, ETH, ...
    symbol: str                # BTC/USDT:USDT
    funding_rate: float        # ставка за один период (0.0001 = 0.01%)
    interval_h: float          # период выплаты, часов
    mark_price: Optional[float] = None
    volume_usd: Optional[float] = None
    next_funding_ts: Optional[int] = None
    hist_avg_rate_per_h: Optional[float] = None   # средняя ставка в час по истории
    hist_positive_share: Optional[float] = None   # доля периодов со ставкой > 0

    @property
    def rate_per_hour(self) -> float:
        return self.funding_rate / self.interval_h


@dataclass
class SpotInfo:
    exchange: str
    base: str
    symbol: str
    price: Optional[float]
    volume_usd: Optional[float]


@dataclass
class Fees:
    spot_taker: float
    perp_taker: float


@dataclass
class Opportunity:
    kind: str                  # "spot-perp" | "perp-perp"
    base: str
    description: str           # что купить / что продать
    rate_per_day: float        # чистая ставка funding в день (доля от номинала)
    apr_gross: float           # % годовых без комиссий, на номинал
    apr_net_capital: float     # % годовых на ВАШ капитал после комиссий за hold_days
                               # (по min(текущая ставка, средняя по истории))
    notional_usd: float        # номинал одной ноги
    profit_hold_usd: float     # ожидаемая прибыль за hold_days после комиссий
    fees_usd: float
    price_diff_pct: float      # базис/разница цен между ногами, %
    hist_rate_per_day: Optional[float] = None
    hist_positive_share: Optional[float] = None
    extra: dict = field(default_factory=dict)


def parse_interval_hours(interval) -> float:
    """'8h' -> 8.0, '1h' -> 1.0, None/мусор -> 8.0"""
    if interval is None:
        return DEFAULT_INTERVAL_H
    if isinstance(interval, (int, float)):
        return float(interval) if interval > 0 else DEFAULT_INTERVAL_H
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*h\s*", str(interval).lower())
    if m and float(m.group(1)) > 0:
        return float(m.group(1))
    return DEFAULT_INTERVAL_H


def pct_diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """(a - b) / b в процентах."""
    if not a or not b:
        return None
    return (a - b) / b * 100.0


def spot_perp_notional(capital: float, leverage: float) -> float:
    """Капитал = спот (N) + маржа фьючерса (N / leverage)."""
    return capital / (1.0 + 1.0 / leverage)


def perp_perp_notional(capital: float, leverage: float) -> float:
    """Капитал = маржа на бирже A (N/lev) + маржа на бирже B (N/lev)."""
    return capital * leverage / 2.0


def _finish(kind, base, desc, rate_per_day, notional, capital, fees_frac,
            hold_days, price_diff, hist_per_day=None, hist_pos=None, extra=None):
    fees_usd = notional * fees_frac
    # Консервативно: если история хуже текущей ставки, считаем доход по истории
    eff_rate = rate_per_day if hist_per_day is None else min(rate_per_day, hist_per_day)
    income = notional * eff_rate * hold_days
    profit = income - fees_usd
    apr_net_capital = profit / capital / hold_days * 365 * 100 if capital > 0 else 0.0
    return Opportunity(
        kind=kind, base=base, description=desc,
        rate_per_day=rate_per_day,
        apr_gross=rate_per_day * 365 * 100,
        apr_net_capital=apr_net_capital,
        notional_usd=notional,
        profit_hold_usd=profit,
        fees_usd=fees_usd,
        price_diff_pct=price_diff if price_diff is not None else 0.0,
        hist_rate_per_day=hist_per_day,
        hist_positive_share=hist_pos,
        extra=extra or {},
    )


def spot_perp_opportunity(perp: PerpInfo, spot: SpotInfo, fees: Fees,
                          capital: float, leverage: float, hold_days: float) -> Optional[Opportunity]:
    """Купить спот + зашортить фьючерс на той же бирже. Выгодно при funding > 0."""
    if perp.funding_rate <= 0:
        return None
    notional = spot_perp_notional(capital, leverage)
    # 4 сделки: покупка спота, шорт фьючерса, продажа спота, закрытие шорта
    fees_frac = 2 * fees.spot_taker + 2 * fees.perp_taker
    rate_day = perp.rate_per_hour * 24
    hist_day = perp.hist_avg_rate_per_h * 24 if perp.hist_avg_rate_per_h is not None else None
    return _finish(
        "spot-perp", perp.base,
        f"{perp.exchange}: купить спот {spot.symbol} + шорт {perp.symbol}",
        rate_day, notional, capital, fees_frac, hold_days,
        pct_diff(perp.mark_price, spot.price),  # >0: фьючерс дороже спота — вход выгоднее
        hist_day, perp.hist_positive_share,
        {"exchange": perp.exchange, "interval_h": perp.interval_h},
    )


def perp_perp_opportunity(a: PerpInfo, b: PerpInfo, fees_a: Fees, fees_b: Fees,
                          capital: float, leverage: float, hold_days: float) -> Optional[Opportunity]:
    """Шорт фьючерса там, где ставка выше, лонг там, где ниже (ставки нормированы на час)."""
    if a.exchange == b.exchange:
        return None
    short, long_ = (a, b) if a.rate_per_hour >= b.rate_per_hour else (b, a)
    f_short, f_long = (fees_a, fees_b) if short is a else (fees_b, fees_a)
    diff_h = short.rate_per_hour - long_.rate_per_hour
    if diff_h <= 0:
        return None
    notional = perp_perp_notional(capital, leverage)
    fees_frac = 2 * f_short.perp_taker + 2 * f_long.perp_taker
    hist_day = None
    if short.hist_avg_rate_per_h is not None and long_.hist_avg_rate_per_h is not None:
        hist_day = (short.hist_avg_rate_per_h - long_.hist_avg_rate_per_h) * 24
    return _finish(
        "perp-perp", short.base,
        f"шорт {short.exchange} ({short.interval_h:g}ч) / лонг {long_.exchange} ({long_.interval_h:g}ч)",
        diff_h * 24, notional, capital, fees_frac, hold_days,
        pct_diff(short.mark_price, long_.mark_price),
        hist_day, None,
        {"short": short.exchange, "long": long_.exchange},
    )


def history_stats(rates: list[float], interval_h: float) -> tuple[Optional[float], Optional[float]]:
    """Средняя ставка в час и доля положительных выплат по истории."""
    rates = [r for r in rates if r is not None]
    if not rates:
        return None, None
    avg = sum(rates) / len(rates)
    return avg / interval_h, sum(1 for r in rates if r > 0) / len(rates)


def estimate_fill(levels: list[list[float]], notional_usd: float) -> tuple[float, float, float]:
    """Проход по одной стороне стакана (asks для покупки, bids для продажи), лучшая цена первой.

    Возвращает (avg_price, filled_usd, slippage), где slippage = |avg - best| / best.
    Если книга тоньше суммы — заполняется сколько есть.
    """
    if not levels or notional_usd <= 0:
        return 0.0, 0.0, 0.0
    best = float(levels[0][0])
    remaining = float(notional_usd)
    filled_usd = 0.0
    filled_amount = 0.0
    for level in levels:
        price, amount = float(level[0]), float(level[1])
        if remaining <= 0 or price <= 0 or amount <= 0:
            continue
        take_usd = min(remaining, price * amount)
        filled_usd += take_usd
        filled_amount += take_usd / price
        remaining -= take_usd
    if filled_amount <= 0:
        return 0.0, 0.0, 0.0
    avg = filled_usd / filled_amount
    return avg, filled_usd, abs(avg - best) / best


def liquidation_price(entry: float, side: str, leverage: float, mmr: float = 0.005) -> float:
    """Цена ликвидации изолированной позиции при данном плече и ставке поддерживающей маржи."""
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0, got {leverage}")
    if side == "short":
        return entry * (1.0 + 1.0 / leverage - mmr)
    if side == "long":
        return entry * (1.0 - 1.0 / leverage + mmr)
    raise ValueError(f"side must be 'long' or 'short', got {side!r}")


def margin_buffer(entry: float, mark: float, liq: float) -> float:
    """Доля пути от входа до ликвидации, которая ещё не пройдена, в [0, 1]."""
    if liq == entry:
        return 0.0
    return min(1.0, max(0.0, (liq - mark) / (liq - entry)))
