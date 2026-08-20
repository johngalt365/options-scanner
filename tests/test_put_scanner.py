from datetime import date
from unittest import TestCase

from options_scanner.scanner import PutScanCandidate, rank_candidates


def candidate(**changes):
    values = dict(
        ticker="NVDA", expiration=date(2026, 9, 24), dte=35, strike=80.0,
        underlying_price=100.0, safety_margin=.20, bid=1.0, ask=1.2,
        delta=-.20, gamma=.01, theta=-.04, vega=.08,
        implied_volatility=.30, open_interest=100, market_data_availability="RpB",
    )
    values.update(changes)
    return PutScanCandidate(**values)


class PutScanCandidateTest(TestCase):
    def test_dte_and_safety_are_exposed(self):
        row = candidate(dte=30, safety_margin=.25)
        self.assertEqual((row.dte, row.safety_margin), (30, .25))

    def test_mid_yield_and_annualization(self):
        row = candidate(bid=1.0, ask=1.4, strike=80, dte=40)
        self.assertAlmostEqual(row.mid, 1.2)
        self.assertAlmostEqual(row.premium_yield, (1.2 * 100) / (80 * 100))
        self.assertAlmostEqual(row.annualized_premium_yield, .015 * 365 / 40)

    def test_missing_side_does_not_invent_mid_and_is_incomplete(self):
        row = candidate(ask=None)
        self.assertIsNone(row.mid)
        self.assertIsNone(row.premium_yield)
        self.assertFalse(row.complete)

    def test_missing_delta_is_incomplete(self):
        self.assertFalse(candidate(delta=None).complete)

    def test_ranking_excludes_incomplete_and_orders_by_annualized_yield(self):
        low = candidate(bid=.8, ask=1.0)
        high = candidate(bid=1.8, ask=2.0)
        self.assertEqual(rank_candidates([low, candidate(bid=None), high]), [high, low])

