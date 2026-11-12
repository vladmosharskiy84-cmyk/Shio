"""Один цикл сканирования: собрать данные -> найти связки -> проверить историю -> отсортировать."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from itertools import combinations

from .calc import Fees, Opportunity, PerpInfo, SpotInfo, perp_perp_opportunity, spot_perp_opportunity
from .exchanges import ExchangeClient


def enabled_exchanges(cfg: dict) -> dict[str, Fees]:
    out = {}
    for name, ec in (cfg.get("exchanges") or {}).items():
        if ec and ec.get("enabled"):
            out[name] = Fees(spot_taker=float(ec["spot_taker"]), perp_taker=float(ec["perp_taker"]))
    return out


def find_opportunities(perps: list[PerpInfo], spots: list[SpotInfo], fees: dict[str, Fees],
                       cfg: dict) -> tuple[list[Opportunity], list[Opportunity]]:
    cap, lev, hold = cfg["capital_usd"], cfg["perp_leverage"], cfg["hold_days"]
    min_pv, min_sv = cfg["min_perp_volume_usd"], cfg["min_spot_volume_usd"]
    max_diff = cfg["max_price_diff_pct"]

    def liquid(v, minimum):  # неизвестный объём не отсекаем, но помечаем в отчёте
        return v is None or v >= minimum

    perps = [p for p in perps if liquid(p.volume_usd, min_pv)]
    spot_idx = {(s.exchange, s.base): s for s in spots if liquid(s.volume_usd, min_sv)}

    spot_perp = []
    for p in perps:
        s = spot_idx.get((p.exchange, p.base))
        if not s:
            continue
        op = spot_perp_opportunity(p, s, fees[p.exchange], cap, lev, hold)
        if op and abs(op.price_diff_pct) <= max_diff:
            spot_perp.append(op)

    by_base = defaultdict(list)
    for p in perps:
        by_base[p.base].append(p)
    perp_perp = []
    for base, group in by_base.items():
        for a, b in combinations(group, 2):
            op = perp_perp_opportunity(a, b, fees[a.exchange], fees[b.exchange], cap, lev, hold)
            if op and abs(op.price_diff_pct) <= max_diff:
                perp_perp.append(op)

    key = lambda o: o.apr_net_capital
    return sorted(spot_perp, key=key, reverse=True), sorted(perp_perp, key=key, reverse=True)


async def scan(cfg: dict) -> tuple[list[Opportunity], list[Opportunity], dict]:
    fees = enabled_exchanges(cfg)
    clients = {n: ExchangeClient(n) for n in fees}
    try:
        snaps = await asyncio.gather(*(c.snapshot() for c in clients.values()))
        perps, spots, stats = [], [], {}
        for name, (p, s) in zip(clients, snaps):
            perps += p
            spots += s
            stats[name] = (len(p), len(s))

        spot_perp, perp_perp = find_opportunities(perps, spots, fees, cfg)

        # История ставок для лучших кандидатов: отсекаем разовые всплески
        top = cfg["history_check_top"]
        perp_by_key = {(p.exchange, p.symbol): p for p in perps}
        need = set()
        for op in spot_perp[:top]:
            need.add((op.extra["exchange"], f"{op.base}/USDT:USDT"))
        for op in perp_perp[:top]:
            need.add((op.extra["short"], f"{op.base}/USDT:USDT"))
            need.add((op.extra["long"], f"{op.base}/USDT:USDT"))
        await asyncio.gather(*(
            clients[ex].enrich_history(perp_by_key[(ex, sym)], cfg["history_periods"])
            for ex, sym in need if (ex, sym) in perp_by_key))

        # пересчитываем уже с историей
        spot_perp, perp_perp = find_opportunities(perps, spots, fees, cfg)
        min_apr = cfg["min_net_apr"]
        spot_perp = [o for o in spot_perp if o.apr_net_capital >= min_apr]
        perp_perp = [o for o in perp_perp if o.apr_net_capital >= min_apr]
        return spot_perp, perp_perp, stats
    finally:
        await asyncio.gather(*(c.close() for c in clients.values()), return_exceptions=True)
