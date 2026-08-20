from argparse import Namespace
from datetime import date
from unittest import TestCase

from options_scanner.ibkr import IbkrMarketDataProvider
from options_scanner.scan_puts import _ibkr_candidates
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


class ProductiveIbkrScannerTest(TestCase):
    class ScannerTransport:
        def __init__(self, underlying_snapshots):
            self.underlying_snapshots = iter(underlying_snapshots)
            self.calls = []

        def get(self, path, params):
            self.calls.append((path, params.copy()))
            if path.endswith("auth/status"):
                return {"authenticated": True}
            if path.endswith("secdef/search"):
                return [{"symbol": "NVDA", "conid": 4815747, "sections": [{"secType": "OPT", "months": "SEP26"}]}]
            if path.endswith("secdef/strikes"):
                return {"put": [80]}
            if path.endswith("secdef/info"):
                return [{
                    "conid": 9001, "symbol": "NVDA", "secType": "OPT", "right": "P",
                    "strike": 80, "maturityDate": "20260925",
                }]
            if path.endswith("marketdata/snapshot") and params["conids"] == "4815747":
                return next(self.underlying_snapshots)
            if path.endswith("marketdata/snapshot"):
                return [{
                    "conid": 9001, "84": "1.00", "86": "1.20", "7308": "-0.20",
                    "7309": "0.01", "7310": "-0.04", "7311": "0.08",
                    "7633": "0.30", "7638": "100", "6509": "RpB",
                }]
            raise AssertionError(path)

    @staticmethod
    def args():
        return Namespace(
            ticker="NVDA", min_dte=30, max_dte=45, min_safety_margin=.20,
            min_abs_delta=.15, max_abs_delta=.30,
        )

    def test_scanner_continues_after_partial_preflight_then_price_snapshot(self):
        transport = self.ScannerTransport((
            [{"conidEx": "4815747@SMART"}],
            [{"conid": 4815747, "31": "100.00"}],
        ))
        provider = IbkrMarketDataProvider(transport, snapshot_retry_delay=0)

        candidates = _ibkr_candidates(provider, self.args(), date(2026, 8, 20))

        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0].underlying_price, candidates[0].strike), (100.0, 80.0))
        underlying_calls = [
            params for path, params in transport.calls
            if path.endswith("marketdata/snapshot") and params["conids"] == "4815747"
        ]
        self.assertEqual(len(underlying_calls), 2)
        self.assertTrue(all(params["fields"] == "31,84,86" for params in underlying_calls))

    def test_scanner_uses_bid_ask_mid_when_field_31_never_arrives(self):
        transport = self.ScannerTransport((
            [{"conid": 4815747}],
            [{"conid": 4815747, "84": "99.00", "86": "101.00"}],
        ))
        provider = IbkrMarketDataProvider(transport, snapshot_retry_delay=0)

        with self.assertLogs("options_scanner.ibkr", level="WARNING"):
            candidates = _ibkr_candidates(provider, self.args(), date(2026, 8, 20))

        self.assertEqual(candidates[0].underlying_price, 100.0)


class BatchedSnapshotTest(TestCase):
    class Transport:
        def __init__(self, deliveries):
            self.deliveries = iter(deliveries)
            self.calls = []

        def get(self, path, params):
            self.calls.append((path, params.copy()))
            return next(self.deliveries)

    def test_multiple_conids_share_request_and_partial_fields_are_merged(self):
        transport = self.Transport((
            [{"conid": 1, "84": "1.0"}, {"conid": 2, "86": "2.2"}],
            [{"conid": 1, "86": "1.2", "7308": "-.2"},
             {"conid": 2, "84": "2.0", "7308": "-.25"}],
        ))
        provider = IbkrMarketDataProvider(transport, snapshot_retry_delay=0)

        quotes = provider.get_put_quotes_batched(
            (("1", 80), ("2", 75)), date(2026, 9, 24), attempts=1,
        )

        self.assertEqual({call[1]["conids"] for call in transport.calls}, {"1,2"})
        self.assertEqual([(q.bid, q.ask, q.delta) for q in quotes], [(1, 1.2, -.2), (2, 2.2, -.25)])

    def test_optional_iv_and_oi_do_not_delay_or_make_contract_incomplete(self):
        transport = self.Transport((
            [{"conid": 1}],
            [{"conid": 1, "84": "1", "86": "1.2", "7308": "-.2"}],
        ))
        quote = IbkrMarketDataProvider(transport, snapshot_retry_delay=0).get_put_quotes_batched(
            (("1", 80),), date(2026, 9, 24), attempts=4,
        )[0]
        row = candidate(bid=quote.bid, ask=quote.ask, delta=quote.delta,
                        implied_volatility=quote.implied_volatility, open_interest=quote.open_interest)
        self.assertTrue(row.complete)
        self.assertIsNone(row.implied_volatility)
        self.assertEqual(len(transport.calls), 2)

    def test_missing_essential_fields_remains_incomplete(self):
        for payload in (
            {"conid": 1, "7308": "-.2"},
            {"conid": 1, "84": "1", "86": "1.2"},
        ):
            transport = self.Transport(([payload], [payload]))
            quote = IbkrMarketDataProvider(transport, snapshot_retry_delay=0).get_put_quotes_batched(
                (("1", 80),), date(2026, 9, 24), attempts=1,
            )[0]
            self.assertFalse(candidate(bid=quote.bid, ask=quote.ask, delta=quote.delta).complete)

    def test_expired_global_deadline_avoids_market_data(self):
        transport = self.Transport(())
        quotes = IbkrMarketDataProvider(transport).get_put_quotes_batched(
            (("1", 80),), date(2026, 9, 24), deadline=0,
        )
        self.assertEqual(quotes[0].conid, "1")
        self.assertIsNone(quotes[0].bid)
        self.assertEqual(transport.calls, [])

    def test_batch_ranking_matches_sequential_ranking_for_same_data(self):
        payload = [
            {"conid": 1, "84": "1", "86": "1.2", "7308": "-.2"},
            {"conid": 2, "84": "2", "86": "2.2", "7308": "-.25"},
        ]
        batched = IbkrMarketDataProvider(self.Transport((payload, payload)), snapshot_retry_delay=0)
        batch_quotes = batched.get_put_quotes_batched((("1", 80), ("2", 80)), date(2026, 9, 24), attempts=1)
        sequential_quotes = []
        for row, conid in zip(payload, ("1", "2")):
            provider = IbkrMarketDataProvider(self.Transport(([row], [row])), snapshot_retry_delay=0)
            sequential_quotes.extend(provider.get_put_quotes_batched(((conid, 80),), date(2026, 9, 24), attempts=1))
        def ranked(quotes):
            return [item.bid for item in rank_candidates([
                candidate(bid=q.bid, ask=q.ask, delta=q.delta) for q in quotes
            ])]
        self.assertEqual(ranked(batch_quotes), ranked(sequential_quotes))
