import asyncio

import pytest
import yaml

from scanner import exchanges as exmod
from scanner.engine import scan
from scanner.exchanges import normalize_perps, normalize_spots, ticker_volume_usd
from scanner.report import print_report, save_csv
from tests.engine.fakes import FakeExchange, mkt


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


@pytest.fixture
def cfg():
    with open("config.yaml", encoding="utf-8") as f:
        c = yaml.safe_load(f)
    c["min_net_apr"] = 0
    # тест сканера не зависит от списка наблюдаемых бирж движка
    legacy = {"binance", "bybit", "okx", "whitebit"}
    for name, ec in c["exchanges"].items():
        ec["enabled"] = name in legacy
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
