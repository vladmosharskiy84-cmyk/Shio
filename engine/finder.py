"""Поиск кандидатов на открытие связки: расчёт, профиль риска, проверка глубины стакана."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Literal

from scanner.calc import (Fees, Opportunity, estimate_fill, perp_perp_notional,
                          spot_perp_notional)
from scanner.engine import find_opportunities

from .collector import Collector, MarketSnapshot
from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    op: Opportunity
    profile: Literal["calm", "aggressive"]
    legs_plan: list[dict]
    capital: float
    liquidity_ok: bool


def classify_profile(op: Opportunity, aggressive_apr: float) -> str:
    if op.apr_net_capital >= aggressive_apr:
        return "aggressive"
    if op.hist_rate_per_day is None or op.hist_rate_per_day < 0.4 * op.rate_per_day:
        return "aggressive"
    return "calm"


def _legs_plan(op: Opportunity) -> list[dict]:
    perp_sym = f"{op.base}/USDT:USDT"
    if op.kind == "spot-perp":
        ex = op.extra["exchange"]
        return [{"exchange": ex, "symbol": f"{op.base}/USDT", "kind": "spot", "side": "long"},
                {"exchange": ex, "symbol": perp_sym, "kind": "perp", "side": "short"}]
    return [{"exchange": op.extra["short"], "symbol": perp_sym, "kind": "perp", "side": "short"},
            {"exchange": op.extra["long"], "symbol": perp_sym, "kind": "perp", "side": "long"}]


async def _leg_ok(collector: Collector, leg: dict, notional: float, max_slippage: float) -> bool:
    try:
        book = await collector.order_book(leg["exchange"], leg["symbol"])
    except Exception as e:
        log.warning("%s %s: стакан недоступен: %s", leg["exchange"], leg["symbol"], str(e)[:200])
        return False
    levels = (book or {}).get("asks" if leg["side"] == "long" else "bids") or []
    _, filled, slippage = estimate_fill(levels, notional)
    return filled == notional and slippage <= max_slippage


async def find_candidates(snapshot: MarketSnapshot, collector: Collector, settings: Settings,
                          capital_for: Callable[[str], float]) -> list[Candidate]:
    trade = {n for n, e in settings.exchanges.items() if e.trade}
    perps = [p for p in snapshot.perps if p.exchange in trade]
    spots = [s for s in snapshot.spots if s.exchange in trade]
    fees = {n: Fees(e.spot_taker, e.perp_taker) for n, e in settings.exchanges.items()}
    lev = settings.limits.max_leverage

    def cfg(capital: float) -> dict:
        return {"capital_usd": capital, "perp_leverage": lev, "hold_days": settings.hold_days,
                "min_perp_volume_usd": settings.min_perp_volume_usd,
                "min_spot_volume_usd": settings.min_spot_volume_usd,
                "max_price_diff_pct": settings.max_price_diff_pct}

    cap_sp, cap_pp = capital_for("spot-perp"), capital_for("perp-perp")
    spot_perp, _ = find_opportunities(perps, spots, fees, cfg(cap_sp))
    _, perp_perp = find_opportunities(perps, spots, fees, cfg(cap_pp))

    cands = []
    for ops, cap in ((spot_perp, cap_sp), (perp_perp, cap_pp)):
        for op in ops:
            cands.append(Candidate(op, classify_profile(op, settings.aggressive_apr),
                                   _legs_plan(op), cap, False))
    cands.sort(key=lambda c: c.op.apr_net_capital, reverse=True)

    async def check(c: Candidate) -> None:
        notional = (spot_perp_notional(c.capital, lev) if c.op.kind == "spot-perp"
                    else perp_perp_notional(c.capital, lev))
        oks = await asyncio.gather(*(_leg_ok(collector, leg, notional, settings.limits.max_slippage)
                                     for leg in c.legs_plan))
        c.liquidity_ok = all(oks)

    await asyncio.gather(*(check(c) for c in cands[:settings.history_check_top]))
    return cands
