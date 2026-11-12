import asyncio

import pytest
import yaml

from scanner import exchanges as exmod
from scanner.engine import scan
from scanner.exchanges import normalize_perps, normalize_spots, ticker_volume_usd
from scanner.report import print_report, save_csv


def mkt(base, swap=True):
    if swap:
        return {"base": base, "quote": "USDT", "settle": "USDT", "swap": True, "linear": True, "active": True}
    return {"base": base, "quote": "USDT", "spot": True, "active": True}


def test_ticker_volume():
    assert ticker_volume_usd({"quoteVolume": 5}) == 5
    assert ticker_volume_usd({"baseVolume": 2, "last": 3}) == 6
    assert ticker_volume_usd(None) is None


def test_normalize_filters_non_usdt_and_reads_interval():
    markets = {"A/USDT:USDT": mkt("A"),
               "B/USD:B": {"base": "B", "quote": "USD", "settle": "B", "swap": True, "linear": False}}
    funding = {"A/USDT:USDT": {"fundingRate": 0.0002, "interval": "4h", "markPrice": 10},
               "B/USD:B": {"fundingRate": 0.01}}
    perps = normalize_perps("okx", markets, funding, {"A/USDT:USDT": {"quoteVolume": 1e8}})
    assert len(perps) == 1
    assert perps[0].interval_h == 4 and perps[0].volume_usd == 1e8 and perps[0].mark_price == 10
    # интервал из fetch_funding_intervals (binance)
    perps = normalize_perps("binance", markets, {"A/USDT:USDT": {"fundingRate": 0.0001}}, {},
                            {"A/USDT:USDT": {"interval": "1h"}})
    assert perps[0].interval_h == 1
    spots = normalize_spots("okx", {"A/USDT": mkt("A", swap=False)}, {"A/USDT": {"last": 10, "quoteVolume": 5e6}})
    assert spots[0].base == "A" and spots[0].price == 10


class FakeExchange:
    """Имитирует ccxt.async_support биржу."""
    DATA = {
        # rate за 8ч; HOT — высокая ставка, SPIKE — всплеск (история низкая)
        "binance": {"HOT": (0.0010, "8h", 100.0), "SPIKE": (0.0030, "8h", 5.0), "BTC": (0.0001, "8h", 60000.0)},
        "bybit":   {"HOT": (0.0002, "8h", 100.1), "SPIKE": (0.0000, "8h", 5.0), "BTC": (0.0001, "8h", 60010.0)},
        "okx":     {"HOT": (-0.0003, "8h", 99.9), "BTC": (0.0001, "8h", 60005.0)},
        "whitebit": {"BTC": (0.0001, "8h", 60000.0)},
    }
    HIST = {"SPIKE": 0.00001}
    has = {"fetchFundingIntervals": False}

    def __init__(self, name):
        self.name = name

    async def load_markets(self):
        out = {}
        for b in self.DATA[self.name]:
            out[f"{b}/USDT:USDT"] = mkt(b)
            out[f"{b}/USDT"] = mkt(b, swap=False)
        return out

    async def fetch_funding_rates(self):
        return {f"{b}/USDT:USDT": {"fundingRate": r, "interval": i, "markPrice": p}
                for b, (r, i, p) in self.DATA[self.name].items()}

    async def fetch_tickers(self, symbols, params):
        suffix = ":USDT" if params["type"] == "swap" else ""
        return {f"{b}/USDT{suffix}": {"last": p, "quoteVolume": 5e7}
                for b, (r, i, p) in self.DATA[self.name].items()}

    async def fetch_funding_rate_history(self, symbol, since, limit):
        base = symbol.split("/")[0]
        rate = self.HIST.get(base, self.DATA[self.name][base][0])
        return [{"fundingRate": rate}] * limit

    async def close(self):
        pass


@pytest.fixture
def cfg():
    with open("config.yaml", encoding="utf-8") as f:
        c = yaml.safe_load(f)
    c["min_net_apr"] = 0
    return c


def test_full_scan(monkeypatch, cfg, tmp_path):
    class FakeCcxt:
        def __getattr__(self, name):
            return lambda opts: FakeExchange(name)
    monkeypatch.setattr(exmod, "ccxt", FakeCcxt())

    spot_perp, perp_perp, stats = asyncio.run(scan(cfg))

    assert set(stats) == {"binance", "bybit", "okx", "whitebit"}
    # всплеск SPIKE: по истории ставка ~0, после комиссий убыток -> отфильтрован
    assert [o.base for o in spot_perp][0] == "HOT"
    assert all(o.base != "SPIKE" for o in spot_perp + perp_perp)
    # BTC с одинаковыми ставками не даёт perp-perp связки
    assert all(o.base != "BTC" for o in perp_perp)
    best = next(o for o in perp_perp if o.base == "HOT" and o.extra == {"short": "binance", "long": "okx"})
    assert best.rate_per_day == pytest.approx((0.0010 + 0.0003) * 3)

    print_report(spot_perp, perp_perp, stats, cfg)
    p = tmp_path / "out.csv"
    save_csv(spot_perp, perp_perp, str(p))
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1 + len(spot_perp) + len(perp_perp)


def test_exchange_failure_does_not_break_scan(monkeypatch, cfg):
    class Broken(FakeExchange):
        async def load_markets(self):
            raise RuntimeError("451 restricted location")

    class FakeCcxt:
        def __getattr__(self, name):
            return lambda opts: (Broken if name == "bybit" else FakeExchange)(name)
    monkeypatch.setattr(exmod, "ccxt", FakeCcxt())
    spot_perp, perp_perp, stats = asyncio.run(scan(cfg))
    assert stats["bybit"] == (0, 0)
    assert spot_perp
