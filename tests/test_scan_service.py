from datetime import date
from unittest import TestCase

from options_scanner.scan_service import PutScanService, ScanRequest


class ScanRequestTest(TestCase):
    def test_financial_defaults(self):
        request = ScanRequest()
        self.assertEqual(
            (request.ticker, request.min_dte, request.max_dte, request.min_safety_margin,
             request.min_abs_delta, request.max_abs_delta),
            ("NVDA", 30, 45, .20, .15, .30),
        )

    def test_invalid_ranges_are_rejected(self):
        invalid = (
            {"min_dte": 46, "max_dte": 45}, {"min_safety_margin": 1.1},
            {"min_abs_delta": .4, "max_abs_delta": .3}, {"ticker": ""},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ScanRequest(**values)

    def test_fake_scan_returns_ranked_complete_candidates_and_summary(self):
        service = PutScanService(today=lambda: date(2026, 8, 20))
        result = service.run(ScanRequest(fake=True))
        self.assertEqual(len(result.candidates), 2)
        self.assertTrue(all(candidate.complete for candidate in result.candidates))
        yields = [candidate.annualized_premium_yield for candidate in result.candidates]
        self.assertEqual(yields, sorted(yields, reverse=True))
        self.assertEqual((result.summary.considered, result.summary.complete), (3, 2))

    def test_fake_scan_can_have_no_results(self):
        service = PutScanService(today=lambda: date(2026, 8, 20))
        result = service.run(ScanRequest(fake=True, min_abs_delta=.9, max_abs_delta=1))
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.underlying_price, 100.0)
        self.assertEqual(result.market_data_status, "Simulado")
        self.assertTrue(result.simulated)
        self.assertIsNotNone(result.updated_at)
