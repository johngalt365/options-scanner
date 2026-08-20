from datetime import date
from unittest import TestCase
from options_scanner.filters import filter_put_candidates, safety_margin
from options_scanner.market_data import FakeMarketDataProvider
from options_scanner.models import MarketData

class FilterPutCandidatesTest(TestCase):
    def setUp(self):
        provider = FakeMarketDataProvider()
        self.as_of = date(2026, 8, 20)
        self.nvda = provider.get_underlying("NVDA")
        self.quotes = provider.get_option_market_data("NVDA")

    def test_keeps_contracts_that_match_all_rules(self):
        self.assertEqual(filter_put_candidates(self.nvda, self.quotes, self.as_of), list(self.quotes[:2]))
    def test_rejects_delta_outside_range(self):
        self.assertEqual(filter_put_candidates(self.nvda, [self.quotes[2]], self.as_of), [])
    def test_returns_market_data_models(self):
        self.assertIsInstance(filter_put_candidates(self.nvda, self.quotes, self.as_of)[0], MarketData)
    def test_safety_margin_formula(self):
        self.assertAlmostEqual(safety_margin(100.0, 80.0), 0.20)
    def test_rejects_inverted_ranges(self):
        with self.assertRaises(ValueError):
            filter_put_candidates(self.nvda, [], self.as_of, min_dte=46, max_dte=30)
