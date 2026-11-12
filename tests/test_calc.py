import pytest

from scanner.calc import (Fees, PerpInfo, SpotInfo, estimate_fill, history_stats, liquidation_price,
                          margin_buffer, parse_interval_hours,
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
    assert (op.extra["perp_symbol"], op.extra["spot_symbol"]) == ("XYZ/USDT:USDT", "XYZ/USDT")


def test_spot_perp_negative_funding_ignored():
    assert spot_perp_opportunity(perp("okx", -0.0001), SpotInfo("okx", "XYZ", "XYZ/USDT", 100, 1e7),
                                 FEES, 500, 1, 7) is None


def test_perp_perp_normalizes_intervals():
    # 0.01% за 1ч = 0.24%/день против 0.01% за 8ч = 0.03%/день
    a = perp("bybit", 0.0001, interval=1)
    b = perp("binance", 0.0001, interval=8)
    op = perp_perp_opportunity(a, b, FEES, FEES, capital=500, leverage=2, hold_days=7)
    assert op.extra == {"short": "bybit", "long": "binance",
                        "short_symbol": "XYZ/USDT:USDT", "long_symbol": "XYZ/USDT:USDT"}
    assert op.rate_per_day == pytest.approx(0.0024 - 0.0003)
    assert op.notional_usd == pytest.approx(500)
    assert op.fees_usd == pytest.approx(500 * 0.002)


def test_perp_perp_negative_rate_long_side():
    # лонг там, где ставка отрицательная, — нам платят на обеих ногах
    op = perp_perp_opportunity(perp("okx", -0.0002), perp("bybit", 0.0002),
                               FEES, FEES, 500, 2, 7)
    assert op.extra == {"short": "bybit", "long": "okx",
                        "short_symbol": "XYZ/USDT:USDT", "long_symbol": "XYZ/USDT:USDT"}
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


def test_estimate_fill_spans_levels():
    avg, filled_usd, slip = estimate_fill([[100, 1], [101, 1]], 150)
    assert avg == pytest.approx(100.33, abs=0.01)
    assert filled_usd == pytest.approx(150, abs=0.01)
    assert slip == pytest.approx(0.0033, abs=0.0001)


def test_estimate_fill_book_too_thin():
    avg, filled_usd, slip = estimate_fill([[100, 0.5]], 150)
    assert filled_usd == pytest.approx(50, abs=0.01)
    assert avg == pytest.approx(100, abs=0.01)
    assert slip == pytest.approx(0.0, abs=1e-9)


def test_estimate_fill_empty_or_zero_notional():
    assert estimate_fill([], 150) == (0.0, 0.0, 0.0)
    assert estimate_fill([[100, 1]], 0) == (0.0, 0.0, 0.0)


def test_estimate_fill_ignores_extra_elements():
    avg, filled_usd, _ = estimate_fill([[100, 1, 7]], 50)
    assert avg == pytest.approx(100)
    assert filled_usd == pytest.approx(50)


def test_liquidation_price_short_and_long():
    assert liquidation_price(100, "short", 3) == pytest.approx(132.83, abs=0.01)
    assert liquidation_price(100, "long", 3) == pytest.approx(67.17, abs=0.01)


@pytest.mark.parametrize("lev,side", [(0, "long"), (-1, "short"), (3, "buy")])
def test_liquidation_price_rejects_bad_input(lev, side):
    with pytest.raises(ValueError):
        liquidation_price(100, side, lev)


def test_margin_buffer():
    assert margin_buffer(100, 100, 132.83) == pytest.approx(1.0)
    assert margin_buffer(100, 126.26, 132.83) == pytest.approx(0.2, abs=0.01)
    assert margin_buffer(100, 140, 132.83) == 0.0
    assert margin_buffer(100, 100, 100) == 0.0


def test_margin_buffer_long_side():
    liq = liquidation_price(100, "long", 3)
    assert margin_buffer(100, 100, liq) == pytest.approx(1.0)
    assert margin_buffer(100, 80.0, liq) == pytest.approx((liq - 80.0) / (liq - 100), abs=1e-9)
    assert margin_buffer(100, 60.0, liq) == 0.0


def test_estimate_fill_full_fill_is_exact_despite_float_error():
    # float-сумма уровней даёт 1.2999999999999998; полное исполнение должно вернуть notional ровно
    levels = [[0.1, 1], [0.1, 1], [1.1, 1]]
    avg, filled_usd, _ = estimate_fill(levels, 1.3)
    assert filled_usd == 1.3
    assert avg == pytest.approx(1.3 / 3)


def test_estimate_fill_thin_book_across_two_levels():
    # уровень 1: $100 (1 BTC), уровень 2: $50.5 (0.5 BTC), остаток $49.5 не заполнен
    avg, filled_usd, slip = estimate_fill([[100, 1], [101, 0.5]], 200)
    assert filled_usd == pytest.approx(150.5, abs=1e-9)
    assert avg == pytest.approx(100.3333, abs=1e-4)
    assert slip == pytest.approx(0.003333, abs=1e-6)


def test_estimate_fill_zero_price_first_level_skipped():
    avg, filled_usd, slip = estimate_fill([[0, 1], [1, 1]], 0.5)
    assert (avg, filled_usd, slip) == pytest.approx((1.0, 0.5, 0.0))
    assert estimate_fill([[0, 1]], 0.5) == (0.0, 0.0, 0.0)
