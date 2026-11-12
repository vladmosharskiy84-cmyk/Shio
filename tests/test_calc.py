import pytest

from scanner.calc import (Fees, PerpInfo, SpotInfo, history_stats, parse_interval_hours,
                          perp_perp_notional, perp_perp_opportunity, spot_perp_notional,
                          spot_perp_opportunity)

FEES = Fees(spot_taker=0.001, perp_taker=0.0005)


def perp(ex, rate, interval=8.0, price=100.0, base="XYZ"):
    return PerpInfo(ex, base, f"{base}/USDT:USDT", rate, interval, mark_price=price, volume_usd=1e7)


@pytest.mark.parametrize("raw,exp", [("8h", 8), ("1h", 1), ("4H", 4), (None, 8), ("abc", 8), (0, 8), (2, 2)])
def test_parse_interval(raw, exp):
    assert parse_interval_hours(raw) == exp


def test_notional_split():
    assert spot_perp_notional(500, 1) == pytest.approx(250)
    assert spot_perp_notional(500, 2) == pytest.approx(333.333, rel=1e-4)
    assert perp_perp_notional(500, 2) == pytest.approx(500)


def test_spot_perp_math():
    # 0.03% за 8ч -> 0.09% в день; плечо 1 -> номинал 250
    op = spot_perp_opportunity(perp("binance", 0.0003, price=100.5),
                               SpotInfo("binance", "XYZ", "XYZ/USDT", 100.0, 1e7),
                               FEES, capital=500, leverage=1, hold_days=10)
    assert op.rate_per_day == pytest.approx(0.0009)
    assert op.apr_gross == pytest.approx(32.85)
    assert op.fees_usd == pytest.approx(250 * 0.003)          # 0.75$
    assert op.profit_hold_usd == pytest.approx(250 * 0.009 - 0.75)  # 2.25 - 0.75
    assert op.apr_net_capital == pytest.approx(1.5 / 500 / 10 * 365 * 100)
    assert op.price_diff_pct == pytest.approx(0.5)


def test_spot_perp_negative_funding_ignored():
    assert spot_perp_opportunity(perp("okx", -0.0001), SpotInfo("okx", "XYZ", "XYZ/USDT", 100, 1e7),
                                 FEES, 500, 1, 7) is None


def test_perp_perp_normalizes_intervals():
    # 0.01% за 1ч = 0.24%/день против 0.01% за 8ч = 0.03%/день
    a = perp("bybit", 0.0001, interval=1)
    b = perp("binance", 0.0001, interval=8)
    op = perp_perp_opportunity(a, b, FEES, FEES, capital=500, leverage=2, hold_days=7)
    assert op.extra == {"short": "bybit", "long": "binance"}
    assert op.rate_per_day == pytest.approx(0.0024 - 0.0003)
    assert op.notional_usd == pytest.approx(500)
    assert op.fees_usd == pytest.approx(500 * 0.002)


def test_perp_perp_negative_rate_long_side():
    # лонг там, где ставка отрицательная, — нам платят на обеих ногах
    op = perp_perp_opportunity(perp("okx", -0.0002), perp("bybit", 0.0002),
                               FEES, FEES, 500, 2, 7)
    assert op.extra == {"short": "bybit", "long": "okx"}
    assert op.rate_per_day == pytest.approx(0.0004 / 8 * 24)


def test_perp_perp_same_exchange_or_equal_rates():
    assert perp_perp_opportunity(perp("okx", 0.001), perp("okx", 0.0), FEES, FEES, 500, 2, 7) is None
    assert perp_perp_opportunity(perp("okx", 0.0001), perp("bybit", 0.0001), FEES, FEES, 500, 2, 7) is None


def test_history_stats():
    avg_h, pos = history_stats([0.0008, 0.0, -0.0008, 0.0016], 8)
    assert avg_h == pytest.approx(0.0004 / 8)
    assert pos == 0.5
    assert history_stats([], 8) == (None, None)


def test_profit_uses_conservative_rate():
    p = perp("binance", 0.003)            # 0.9%/день сейчас
    p.hist_avg_rate_per_h = 0.0001 / 8    # 0.03%/день в среднем
    op = spot_perp_opportunity(p, SpotInfo("binance", "XYZ", "XYZ/USDT", 100.0, 1e7),
                               FEES, capital=500, leverage=1, hold_days=10)
    assert op.profit_hold_usd == pytest.approx(250 * 0.0003 * 10 - 0.75)
    assert op.apr_gross == pytest.approx(0.009 * 365 * 100)   # брутто — по текущей
