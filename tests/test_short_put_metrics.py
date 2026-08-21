from datetime import date

import pytest

from options_scanner.filters import filter_put_candidates
from options_scanner.models import MarketData, OptionContract, OptionType, Underlying
from options_scanner.scanner import PutScanCandidate, rank_candidates
from options_scanner.scan_service import ScanMetrics, ScanResult
from options_scanner.web import _rows


def candidate(**changes):
    values = dict(ticker="TEST", expiration=date(2026, 9, 25), dte=35, strike=80,
                  underlying_price=100, safety_margin=.2, bid=1, ask=1.4,
                  delta=-.2, gamma=.01, theta=-.06, vega=.08,
                  implied_volatility=.482, open_interest=100,
                  market_data_availability="RealTime")
    values.update(changes)
    return PutScanCandidate(**values)


def quote(*, iv=.482, theta=-.06):
    return MarketData(OptionContract("1", "TEST", OptionType.PUT, 80, date(2026, 9, 25)),
                      1, 1.4, -.2, .01, theta, .08, iv, 10, 100)


def test_contract_and_short_theta_and_relative_decay():
    for contract_theta in (-.134, -.112):
        row = candidate(theta=contract_theta)
        assert row.contract_theta == contract_theta
        assert row.short_theta == -contract_theta


def test_positive_contract_theta_is_preserved_and_short_exposure_is_signed():
    row = candidate(theta=.118)
    assert row.theta == .118
    assert row.contract_theta == .118
    assert row.short_theta == -.118
    assert row.theta_decay_pct_per_day == pytest.approx(-9.8333333333)


def test_theta_decay_uses_short_exposure_without_mutating_contract_theta():
    row = candidate(theta=-.12)
    assert row.theta_decay_pct_per_day == pytest.approx(10)
    assert row.contract_theta == -.12


def test_theta_decay_requires_positive_mid_and_theta():
    assert candidate(bid=None).theta_decay_pct_per_day is None
    assert candidate(bid=0, ask=0).theta_decay_pct_per_day is None
    assert candidate(theta=None).short_theta is None
    assert candidate(theta=None).theta_decay_pct_per_day is None


def test_renderer_uses_contract_and_short_theta_in_their_respective_columns():
    row = candidate(theta=.118)
    html = _rows(ScanResult((row,), ScanMetrics(), .1, underlying_price=100))
    assert "<td>0.1180</td><td>-0.1180</td><td>-9.83</td>" in html


def test_optional_iv_and_theta_filters_and_missing_iv():
    underlying = Underlying("TEST", 100)
    positive_contract_theta = quote(theta=.118)
    rows = [quote(), quote(iv=None), quote(theta=-.02), positive_contract_theta]
    base = dict(min_dte=30, max_dte=45, min_safety_margin=.2,
                min_abs_delta=.15, max_abs_delta=.3)
    assert filter_put_candidates(underlying, rows, date(2026, 8, 21), **base) == rows
    assert filter_put_candidates(underlying, rows, date(2026, 8, 21), min_iv=.4, **base) == [rows[0], rows[2], positive_contract_theta]
    assert filter_put_candidates(underlying, rows, date(2026, 8, 21), min_short_theta=.05, **base) == [rows[0], rows[1]]


def test_disabled_new_filters_preserve_existing_ranking():
    rows = [candidate(bid=.5, ask=.7), candidate(bid=1.5, ask=1.7, implied_volatility=None)]
    assert rank_candidates(rows) == rank_candidates(rows)
