import pytest

from engine.config import load_settings
from engine.decider import capital_for_pair, decide
from engine.finder import Candidate
from engine.models import Position, Proposal
from scanner.calc import Opportunity

EQUITY = 500.0


def settings(mode="semi"):
    s = load_settings("config.yaml")
    s.mode = mode
    return s


def cand(base="BTC", apr=50.0, profile="calm", capital=100.0, liquidity_ok=True):
    op = Opportunity(kind="spot-perp", base=base, description="", rate_per_day=0.001,
                     apr_gross=60.0, apr_net_capital=apr, notional_usd=200.0,
                     profit_hold_usd=1.0, fees_usd=0.5, price_diff_pct=0.0)
    return Candidate(op, profile, [], capital, liquidity_ok)


def pos(base="ETH", profile="calm", status="open", capital=100.0):
    return Position(None, "spot-perp", base, profile, status, capital, 0)


def prop(base="SOL", status="pending"):
    return Proposal(None, 0, 1, base, "spot-perp", "calm", [], 100.0, 50.0, 1.0, status)


def run(cands, open_positions=(), pending=(), day_pnl=0.0, paused=False, mode="semi",
        equity=EQUITY):
    return decide(cands, list(open_positions), list(pending), equity, day_pnl, paused,
                  settings(mode))


def test_capital_for_pair():
    assert capital_for_pair(500.0, settings()) == 250.0


def test_semi_mode_proposes():
    d = run([cand(apr=90.0)])
    assert [x.action for x in d] == ["propose"]


def test_auto_mode_enters_above_min_apr():
    assert run([cand(apr=25.0)], mode="auto")[0].action == "enter"


def test_auto_mode_proposes_below_min_apr():
    # между min_net_apr (15) и min_auto_apr (20): предложить
    assert run([cand(apr=17.0)], mode="auto")[0].action == "propose"


def test_below_min_net_apr_skipped():
    assert settings().min_net_apr == 15
    for mode in ("semi", "auto"):
        d = run([cand(apr=10.0)], mode=mode)[0]
        assert d.action == "skip" and "15" in d.reason
    assert run([cand(apr=15.0)])[0].action == "propose"


def test_respects_max_positions():
    cands = [cand(b) for b in ("A", "B", "C", "D")]
    d = run(cands, open_positions=[pos("X")])
    assert [x.action for x in d] == ["propose", "propose", "skip", "skip"]
    d = run([cand("A")], open_positions=[pos("X"), pos("Y"), pos("Z", status="closing")])
    assert d[0].action == "skip"


def test_closed_positions_do_not_count():
    d = run([cand("A")], open_positions=[pos("X", status="closed"), pos("Y", status="failed"),
                                         pos("Z", status="liquidated"), pos("W", status="closed")])
    assert d[0].action == "propose"


def test_no_duplicate_base():
    assert run([cand("ETH")], open_positions=[pos("ETH")])[0].action == "skip"
    assert run([cand("SOL")], pending=[prop("SOL")])[0].action == "skip"
    assert run([cand("SOL")], pending=[prop("SOL", status="approved")])[0].action == "skip"
    assert run([cand("SOL")], pending=[prop("SOL", status="expired")])[0].action == "propose"
    d = run([cand("BTC"), cand("BTC")])
    assert [x.action for x in d] == ["propose", "skip"]


def test_illiquid_skipped():
    assert run([cand(liquidity_ok=False)])[0].action == "skip"


def test_aggressive_share_cap():
    d = run([cand(profile="aggressive")], open_positions=[pos("ETH", "aggressive", capital=150.0)])
    assert d[0].action == "skip"
    # учитываются и решения этого же вызова
    d = run([cand("A", profile="aggressive", capital=100.0),
             cand("B", profile="aggressive", capital=100.0)])
    assert [x.action for x in d] == ["propose", "skip"]
    # спокойные связки лимит не трогает
    d = run([cand(profile="calm")], open_positions=[pos("ETH", "aggressive", capital=150.0)])
    assert d[0].action == "propose"


def test_non_positive_apr_skipped():
    assert run([cand(apr=0.0)])[0].action == "skip"
    assert run([cand("B", apr=-5.0)])[0].action == "skip"


def test_daily_loss_pauses():
    # капитал на начало суток 500 (= текущий + дневной убыток), лимит 5% = 25
    d = run([cand("A"), cand("B")], day_pnl=-26.0, equity=EQUITY - 26.0)
    assert [x.action for x in d] == ["skip", "skip"]
    assert all("дневной" in x.reason for x in d)
    assert run([cand()], day_pnl=-24.0, equity=EQUITY - 24.0)[0].action == "propose"


def test_daily_loss_threshold_uses_day_start_equity():
    # начало суток 504.5 -> лимит 25.225; от текущих 480 было бы 24 и пауза
    assert run([cand()], day_pnl=-24.5, equity=480.0)[0].action == "propose"
    assert run([cand()], day_pnl=-25.3, equity=479.2)[0].action == "skip"


def test_paused_skips_all():
    d = run([cand("A"), cand("B")], paused=True)
    assert [x.action for x in d] == ["skip", "skip"]
    assert all(x.reason for x in d)


def test_one_decision_per_candidate_in_order():
    cands = [cand("A"), cand("B", liquidity_ok=False), cand("C")]
    d = run(cands)
    assert [x.candidate for x in d] == cands


def test_pending_proposal_consumes_slot():
    pend = [prop("P1"), prop("P2", status="approved"), prop("P3")]
    d = run([cand("A"), cand("B")], pending=pend)
    assert [x.action for x in d] == ["skip", "skip"]
    d = run([cand("A"), cand("B")], pending=pend[:2])
    assert [x.action for x in d] == ["propose", "skip"]


def test_pending_aggressive_proposal_counts_toward_share():
    p = prop("P1")
    p.profile = "aggressive"
    p.capital = 150.0
    assert run([cand(profile="aggressive")], pending=[p])[0].action == "skip"
    assert run([cand(profile="calm")], pending=[p])[0].action == "propose"


def test_daily_loss_boundary_pauses():
    assert run([cand()], day_pnl=-25.0, equity=EQUITY - 25.0)[0].action == "skip"


def test_aggressive_sum_at_cap_allowed():
    d = run([cand(profile="aggressive", capital=50.0)],
            open_positions=[pos("ETH", "aggressive", capital=100.0)])
    assert d[0].action == "propose"


def test_check_limits_reasons():
    from engine.decider import check_limits
    s = settings()
    assert check_limits("A", "calm", 100.0, [], [], EQUITY, 0.0, False, s) is None
    assert "паузе" in check_limits("A", "calm", 100.0, [], [], EQUITY, 0.0, True, s)
    full = [pos("X"), pos("Y")]
    assert "максимум" in check_limits("A", "calm", 100.0, full, [prop("Z")], EQUITY, 0.0, False, s)
    assert check_limits("A", "calm", 100.0, full, [prop("Z", status="expired")], EQUITY, 0.0,
                        False, s) is None
    assert "ETH" in check_limits("ETH", "calm", 100.0, [pos("ETH")], [], EQUITY, 0.0, False, s)


SP = [{"exchange": "bybit", "kind": "spot"}, {"exchange": "bybit", "kind": "perp"}]
PP = [{"exchange": "bybit", "kind": "perp"}, {"exchange": "okx", "kind": "perp"}]


def test_affordable_capital_spot_perp():
    from engine.decider import affordable_capital
    assert affordable_capital("spot-perp", SP, 250.0, {"bybit": 170.0}) == pytest.approx(166.6)
    assert affordable_capital("spot-perp", SP, 100.0, {"bybit": 170.0}) == 100.0


def test_affordable_capital_perp_perp():
    from engine.decider import affordable_capital
    assert affordable_capital("perp-perp", PP, 250.0, {"bybit": 170.0, "okx": 160.0}) == 250.0
    assert affordable_capital("perp-perp", PP, 250.0, {"bybit": 100.0, "okx": 90.0}) == pytest.approx(176.4)


def test_affordable_capital_below_min_or_missing_balance():
    from engine.decider import MIN_PAIR_CAPITAL, affordable_capital
    assert affordable_capital("spot-perp", SP, 250.0, {"bybit": 15.0}) < MIN_PAIR_CAPITAL
    assert affordable_capital("perp-perp", PP, 250.0, {"bybit": 170.0}) == 0.0


def test_reserve_capital():
    from engine.decider import reserve_capital
    bal = {"bybit": 170.0, "okx": 160.0}
    reserve_capital("spot-perp", SP, 100.0, bal)
    assert bal == {"bybit": 70.0, "okx": 160.0}
    reserve_capital("perp-perp", PP, 100.0, bal)
    assert bal == {"bybit": 20.0, "okx": 110.0}
