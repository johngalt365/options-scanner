from datetime import date
from unittest import TestCase
from unittest.mock import patch

from options_scanner.scan_service import PutScanService, ScanRequest
from options_scanner.models import Underlying
from options_scanner.historical import HistoricalBar, HistoricalPeriod


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
        scores = [candidate.evaluation.total_score for candidate in result.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # The legacy annualized-yield sorter remains available separately.
        from options_scanner.scanner import rank_candidates
        yields = [candidate.annualized_premium_yield for candidate in rank_candidates(result.candidates)]
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

    def test_fake_scan_preserves_all_filter_reasons_and_counts(self):
        result = PutScanService(today=lambda: date(2026, 8, 20)).run(ScanRequest(
            fake=True, min_abs_delta=.25, max_abs_delta=.30,
            min_iv=.50, min_short_theta=.10,
        ))
        self.assertGreater(result.summary.rejected_delta, 0)
        self.assertGreater(result.summary.rejected_iv, 0)
        self.assertGreater(result.summary.rejected_theta, 0)
        reasons = [reason for item in result.summary.discarded_contracts for reason in item.reasons]
        self.assertTrue(any(reason.startswith("|Delta|") for reason in reasons))
        self.assertTrue(any(reason.startswith("IV ") for reason in reasons))
        self.assertTrue(any(reason.startswith("Theta short ") for reason in reasons))
        self.assertTrue(any("mínimo" in reason and "." in reason for reason in reasons))

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

    def test_multi_history_requests_and_preserves_each_available_horizon(self):
        periods = (HistoricalPeriod.THREE_MONTHS, HistoricalPeriod.SIX_MONTHS,
                   HistoricalPeriod.ONE_YEAR)
        for available_count in (3, 2, 1, 0):
            with self.subTest(available_count=available_count):
                class Provider:
                    last_underlying = Underlying("AEHR", 20.0)
                    last_underlying_conid = "265598"

                    def __init__(self):
                        self.calls = []
                        self.last_historical_bars_received = 0

                    def get_historical_bars(self, symbol, period):
                        self.calls.append((symbol, period, self.last_underlying_conid))
                        if periods.index(period) >= available_count:
                            self.last_historical_bars_received = 0
                            return ()
                        self.last_historical_bars_received = 1
                        return (HistoricalBar(date(2026, 8, 20), 19, 21, 18, 20),)

                provider = Provider()
                with patch("options_scanner.scan_puts._ibkr_candidates", return_value=[]):
                    result = PutScanService().run(
                        ScanRequest(ticker="AEHR", historical_period=HistoricalPeriod.MULTI),
                        provider=provider,
                    )

                self.assertEqual(provider.calls, [("AEHR", period, "265598") for period in periods])
                contexts = result.technical_context.horizon_contexts
                self.assertEqual([bool(item.bars) for item in contexts],
                                 [index < available_count for index in range(3)])
                self.assertEqual(result.summary.historical_request, 3)
                self.assertEqual(result.summary.historical_bars_valid, available_count)
                self.assertEqual(result.summary.historical_status,
                                 "ok" if available_count else "empty")
                self.assertEqual(bool(result.technical_context.bars), bool(available_count))

    def test_multi_history_exception_does_not_discard_other_horizons(self):
        class Provider:
            last_underlying = Underlying("AEHR", 20.0)
            last_underlying_conid = "265598"
            last_historical_bars_received = 1

            def get_historical_bars(self, symbol, period):
                if period == HistoricalPeriod.SIX_MONTHS:
                    raise RuntimeError("unavailable")
                return (HistoricalBar(date(2026, 8, 20), 19, 21, 18, 20),)

        with patch("options_scanner.scan_puts._ibkr_candidates", return_value=[]):
            result = PutScanService().run(
                ScanRequest(ticker="AEHR", historical_period=HistoricalPeriod.MULTI),
                provider=Provider(),
            )
        self.assertEqual([bool(item.bars) for item in result.technical_context.horizon_contexts],
                         [True, False, True])
        self.assertEqual(result.summary.historical_status, "ok")

    def test_http_accounting_contains_only_endpoint_names_and_counts_history(self):
        from collections import Counter
        class Provider:
            last_underlying=Underlying("NVDA",217.77)
            last_underlying_conid="4815747"
            http_call_counts=Counter({"secdef/search":1, "marketdata/snapshot":2})
            def get_historical_bars(self, symbol, period):
                self.http_call_counts["marketdata/history"] += 1
                return ()
        with patch("options_scanner.scan_puts._ibkr_candidates",return_value=[]):
            result=PutScanService().run(ScanRequest(),provider=Provider())
        self.assertEqual(result.summary.http_calls,
                         {"secdef/search":1, "marketdata/snapshot":2, "marketdata/history":1})
        self.assertNotIn("headers", str(result.summary.http_calls).lower())
