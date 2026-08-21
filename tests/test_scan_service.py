from datetime import date
from unittest import TestCase
from unittest.mock import patch

from options_scanner.scan_service import PutScanService, ScanRequest
from options_scanner.models import Underlying


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
        self.assertIsNotNone(result.technical_context)

    def test_history_failure_does_not_invalidate_live_option_scan_or_leak_details(self):
        class Provider:
            last_underlying=Underlying("NVDA",217.77)
            last_underlying_conid="4815747"
            def get_historical_bars(self, symbol, period):
                raise RuntimeError("secret-token=https://private.invalid/session/123")
        provider=Provider()
        with patch("options_scanner.scan_puts._ibkr_candidates",return_value=[]), self.assertLogs("options_scanner.scan_service",level="WARNING") as logs:
            result=PutScanService().run(ScanRequest(),provider=provider,verbose=True)
        self.assertEqual(result.candidates,())
        self.assertEqual(result.underlying_price,217.77)
        self.assertEqual(result.summary.historical_status,"error")
        self.assertEqual(result.technical_context.bars,())
        self.assertNotIn("secret-token"," ".join(logs.output))

    def test_history_metrics_are_populated_without_candidates(self):
        class Provider:
            last_underlying=Underlying("NVDA",217.77)
            last_underlying_conid="4815747"
            last_historical_bars_received=2
            def get_historical_bars(self, symbol, period):
                from options_scanner.historical import HistoricalBar
                return (HistoricalBar(date(2026,8,19),216,218,215,217),HistoricalBar(date(2026,8,20),217,219,216,218))
        with patch("options_scanner.scan_puts._ibkr_candidates",return_value=[]):
            result=PutScanService().run(ScanRequest(),provider=Provider())
        self.assertEqual((result.summary.historical_request,result.summary.historical_bars_received,
                          result.summary.historical_bars_valid,result.summary.historical_period,
                          result.summary.historical_status),(1,2,2,"6m","ok"))
        self.assertIn("historical_data",result.summary.phase_seconds)
        self.assertIn("technical_analysis",result.summary.phase_seconds)
