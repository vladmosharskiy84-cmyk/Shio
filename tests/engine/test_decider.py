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


def run(cands, open_positions=(), pending=(), day_pnl=0.0, paused=False, mode="semi"):
    return decide(cands, list(open_positions), list(pending), EQUITY, day_pnl, paused,
                  settings(mode))


def test_capital_for_pair():
    assert capital_for_pair(500.0, settings()) == 250.0


def test_semi_mode_proposes():
    d = run([cand(apr=90.0)])
    assert [x.action for x in d] == ["propose"]


def test_auto_mode_enters_above_min_apr():
    assert run([cand(apr=25.0)], mode="auto")[0].action == "enter"


def test_auto_mode_proposes_below_min_apr():
    assert run([cand(apr=10.0)], mode="auto")[0].action == "propose"


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
    d = run([cand("A"), cand("B")], day_pnl=-26.0)
    assert [x.action for x in d] == ["skip", "skip"]
    assert all("дневной" in x.reason for x in d)
    assert run([cand()], day_pnl=-24.0)[0].action == "propose"


def test_paused_skips_all():
    d = run([cand("A"), cand("B")], paused=True)
    assert [x.action for x in d] == ["skip", "skip"]
    assert all(x.reason for x in d)


def test_one_decision_per_candidate_in_order():
    cands = [cand("A"), cand("B", liquidity_ok=False), cand("C")]
    d = run(cands)
    assert [x.candidate for x in d] == cands
