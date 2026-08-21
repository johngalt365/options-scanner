from dataclasses import replace
from datetime import date

from options_scanner.historical import HistoricalPeriod
from options_scanner.scanner import PutScanCandidate, rank_candidates
from options_scanner.short_put_ranking import (evaluate, liquidity_score, premium_score,
                                                rank_by_score, risk_score, theta_score)


def c(**changes):
    values = dict(ticker="T", expiration=date(2026, 10, 1), dte=40, strike=80,
                  underlying_price=100, safety_margin=.20, bid=1, ask=1.2, delta=-.19,
                  gamma=.01, theta=-.04, vega=.1, implied_volatility=.4,
                  open_interest=100, market_data_availability="RealTime")
    values.update(changes)
    return PutScanCandidate(**values)


def test_component_bounds_saturation_and_reasonable_monotonicity():
    assert risk_score(-.19, .30)[0] == 30
    assert risk_score(-.19, .80)[0] == 30
    assert risk_score(-.20, .20)[0] > risk_score(-.25, .20)[0] > risk_score(-.30, .20)[0]
    assert risk_score(-.19, .25)[0] > risk_score(-.19, .20)[0]
    assert premium_score(.05, .3)[0] == premium_score(.50, .9)[0] == 20
    assert premium_score(.03, .9)[0] == premium_score(.03, .2)[0]
    assert theta_score(5)[0] == theta_score(50)[0] == 15
    assert theta_score(2)[0] < theta_score(4)[0]
    assert liquidity_score(.05, 500)[0] > liquidity_score(.40, 500)[0]
    assert liquidity_score(.1, 5000)[0] == liquidity_score(.1, 500)[0]


def test_missing_data_is_confessed_and_total_is_bounded():
    row = c(bid=None, theta=None, implied_volatility=None, open_interest=None, delta=None)
    result = evaluate(row)
    assert 0 <= result.total_score <= 100
    assert {"Delta", "contexto técnico", "IV", "theta relativo", "spread relativo", "open interest"} <= set(result.missing_data)


def test_score_ranking_differs_from_legacy_yield_and_is_deterministic():
    expensive_bad = c(strike=80, bid=3, ask=5, delta=-.29, open_interest=5)
    balanced = c(strike=79, bid=1.5, ask=1.7, delta=-.18, open_interest=500)
    assert rank_candidates([balanced, expensive_bad])[0] == expensive_bad
    ranked = rank_by_score([expensive_bad, balanced])
    assert ranked[0].strike == 79
    assert rank_by_score([expensive_bad, balanced]) == ranked
    assert all(row.evaluation is not None for row in ranked)


def test_tie_breakers_delta_then_premium_then_oi_then_identity():
    # Force equal totals by saturated premium/theta and zero technical context.
    a = c(ticker="B", strike=70, safety_margin=.30, bid=4, ask=4, theta=-.2, delta=-.18, open_interest=500)
    b = replace(a, ticker="A")
    assert [x.ticker for x in rank_by_score([a, b])] == ["A", "B"]


def test_labels_are_neutral_and_reasons_reconstruct_metrics():
    result = evaluate(c())
    assert result.label in {"Muy sólida", "Sólida", "Intermedia", "Débil"}
    assert any("|Delta| 0.19" in reason for reason in result.reasons)
    assert any("spread relativo" in reason for reason in result.reasons)
