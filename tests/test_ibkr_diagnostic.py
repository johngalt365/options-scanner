from io import StringIO
from unittest import TestCase
from unittest.mock import patch
from urllib.error import URLError

from options_scanner.ibkr import (
    ClientPortalTransport,
    GatewayUnavailableError,
    IbkrMarketDataProvider,
    IncompleteDataError,
    MarketDataUnauthorizedError,
    MarketDataFieldStatus,
    NotAuthenticatedError,
    TickerNotFoundError,
)
from options_scanner.ibkr_diagnostic import _display_deep_attempt, main, run


class FakeTransport:
    def __init__(self, authenticated=True):
        self.authenticated = authenticated
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, params))
        if path.endswith("auth/status"):
            return {"authenticated": self.authenticated}
        if path.endswith("secdef/search"):
            return [{"symbol": "NVDA", "conid": 4815747, "sections": [{"secType": "OPT", "months": "SEP26;OCT26"}]}]
        if path.endswith("secdef/strikes"):
            return {"put": [95, 100, 105]}
        if path.endswith("secdef/info"):
            return [{"conid": f"9{params['strike'].replace('.', '')}", "right": "P"}]
        if path.endswith("marketdata/snapshot"):
            if params["conids"] == "4815747":
                return [{"conid": 4815747, "31": "101.25"}]
            return [
                {"conid": conid, "84": "1.1", "86": "1.2", "7308": "-0.25", "7310": "-0.04", "7633": "0.32", "7638": "1200", "6509": "RpB"}
                for conid in params["conids"].split(",")
            ]
        raise AssertionError(path)


class DiagnosticTest(TestCase):
    def test_deep_snapshot_preserves_preflight_partial_and_later_fields(self):
        class TemporalTransport:
            def __init__(self):
                self.responses = [
                    [{"conidEx": "101@SMART", "server_id": "must-not-leak"}],
                    [{"conid": 101, "31": "1.15", "6509": "RpBd", "84": "N/A"}],
                    [{"conid": 101, "84": "1.10", "86": "1.20", "7633": "0.32", "7638": "11500"}],
                ]
                self.calls = []

            def get(self, path, params):
                self.calls.append((path, params.copy()))
                return self.responses.pop(0)

        transport = TemporalTransport()
        provider = IbkrMarketDataProvider(transport)
        provider._searched_underlyings.add("4815747")
        observations = provider.diagnose_put_contract("4815747", "101", retry_delays=(0, 0))

        self.assertEqual([item.phase for item in observations], ["pre-flight", "snapshot", "snapshot"])
        self.assertEqual(observations[0].fields, {})
        self.assertEqual(observations[1].fields, {"31": "1.15", "6509": "RpBd", "84": "N/A"})
        self.assertEqual(observations[2].fields["7633"], "0.32")
        self.assertNotIn("server_id", observations[0].fields)
        self.assertTrue(all(call[1]["fields"] == provider.DEEP_OPTION_SNAPSHOT_FIELDS for call in transport.calls))
        partial = _display_deep_attempt(observations[1])
        self.assertIn("84=field recibido con valor N/A", partial)
        self.assertIn("86=field no recibido", partial)
        self.assertIn("6509=RpBd (RealTime, book disponible)", partial)

    def test_deep_snapshot_requires_prior_secdef_search(self):
        provider = IbkrMarketDataProvider(FakeTransport())
        with self.assertRaisesRegex(IncompleteDataError, "secdef/search"):
            provider.diagnose_put_contract("4815747", "101", retry_delays=())

    def test_option_snapshot_repeats_preflight_and_merges_delayed_partial_data(self):
        class DelayedTransport:
            def __init__(self):
                self.calls = []

            def get(self, path, params):
                self.calls.append((path, params.copy()))
                responses = [
                    [{"conidEx": "101@SMART"}, {"conidEx": "102@SMART"}],
                    [{"conid": 101, "84": "C1.10", "86": "1.20"}, {"conid": 102, "84": "2.10"}],
                    [{"conid": 101, "7308": "-0.25", "7310": "-0.04", "7633": "32%", "7638": "1.2K", "6509": "RpB"},
                     {"conid": 102, "86": "2.20", "7308": "-0.30", "7310": "-0.05", "7633": "0.35", "7638": 900, "6509": "D"}],
                ]
                return responses[len(self.calls) - 1]

        transport = DelayedTransport()
        quotes = IbkrMarketDataProvider(transport, snapshot_attempts=2, snapshot_retry_delay=0).get_put_quotes(
            (("101", 100.0), ("102", 95.0)), __import__("datetime").date(2026, 9, 1)
        )

        self.assertEqual(len(transport.calls), 3)  # pre-flight + dos entregas diferidas
        self.assertTrue(all(call[1]["fields"] == "84,86,7308,7310,7633,7638,6509" for call in transport.calls))
        self.assertEqual((quotes[0].bid, quotes[0].implied_volatility, quotes[0].open_interest), (1.1, 32.0, 1200))
        self.assertEqual(quotes[1].ask, 2.2)
        self.assertTrue(all(status is MarketDataFieldStatus.AVAILABLE for quote in quotes for status in quote.field_statuses.values()))
        self.assertEqual(quotes[0].market_data_availability.display, "RpB (RealTime, book disponible)")
        self.assertEqual(quotes[1].market_data_availability.feed, "Delayed")

    def test_market_data_availability_interprets_status_incomplete_and_book(self):
        class AvailabilityTransport:
            def __init__(self): self.calls = 0
            def get(self, path, params):
                self.calls += 1
                return [{"conid": 101}] if self.calls == 1 else [
                    {"conid": 101, "31": "1.15", "7308": "-.25", "7310": "-.04", "6509": "NiB"}
                ]

        quote = IbkrMarketDataProvider(
            AvailabilityTransport(), snapshot_attempts=1, snapshot_retry_delay=0
        ).get_put_quotes((("101", 100.0),), __import__("datetime").date(2026, 9, 1))[0]

        self.assertEqual(quote.market_data_availability.raw, "NiB")
        self.assertEqual(quote.market_data_availability.feed, "Not Subscribed")
        self.assertTrue(quote.market_data_availability.incomplete)
        self.assertTrue(quote.market_data_availability.book)
        self.assertIs(quote.field_statuses["bid"], MarketDataFieldStatus.PARTIAL_RESPONSE)

    def test_option_snapshot_classifies_not_ready_unavailable_and_partial(self):
        class IncompleteTransport:
            def __init__(self): self.calls = 0
            def get(self, path, params):
                self.calls += 1
                if self.calls == 1:
                    return [{"conid": 101}, {"conid": 102}, {"conid": 103}]
                return [{"conid": 101}, {"conid": 102, "84": "N/A"}, {"conid": 103, "84": "1.0"}]

        with self.assertLogs("options_scanner.ibkr", level="WARNING") as logs:
            quotes = IbkrMarketDataProvider(IncompleteTransport(), snapshot_attempts=1, snapshot_retry_delay=0).get_put_quotes(
                (("101", 100.0), ("102", 99.0), ("103", 98.0)), __import__("datetime").date(2026, 9, 1)
            )

        self.assertIs(quotes[0].field_statuses["bid"], MarketDataFieldStatus.NOT_READY)
        self.assertIs(quotes[1].field_statuses["bid"], MarketDataFieldStatus.UNAVAILABLE)
        self.assertIs(quotes[2].field_statuses["ask"], MarketDataFieldStatus.PARTIAL_RESPONSE)
        self.assertIn("campos recibidos", " ".join(logs.output))

    def test_underlying_snapshot_repeats_after_conid_only_preflight(self):
        class PreflightTransport:
            def __init__(self):
                self.calls = []

            def get(self, path, params):
                self.calls.append((path, params))
                return [{"conid": 4815747}] if len(self.calls) == 1 else [{"conid": 4815747, "31": "101.25"}]

        transport = PreflightTransport()
        underlying = IbkrMarketDataProvider(transport, snapshot_retry_delay=0).get_underlying_by_conid("NVDA", "4815747")

        self.assertEqual(underlying.current_price, 101.25)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][1]["fields"], IbkrMarketDataProvider.SNAPSHOT_FIELDS)
        self.assertEqual(transport.calls[1][1], transport.calls[0][1])

    def test_underlying_uses_bid_ask_mid_when_last_is_absent(self):
        class BidAskTransport:
            def __init__(self):
                self.calls = 0

            def get(self, path, params):
                self.calls += 1
                return [{"conid": 4815747}] if self.calls == 1 else [
                    {"conid": 4815747, "84": "100.00", "86": "102.00"}
                ]

        provider = IbkrMarketDataProvider(BidAskTransport(), snapshot_retry_delay=0)
        with self.assertLogs("options_scanner.ibkr", level="WARNING") as logs:
            underlying = provider.get_underlying_by_conid("NVDA", "4815747")

        self.assertEqual(underlying.current_price, 101.0)
        self.assertIn("se usa el mid", logs.output[0])

    def test_underlying_fails_after_retries_when_no_price_is_available(self):
        class NoPriceTransport:
            def __init__(self):
                self.calls = 0

            def get(self, path, params):
                self.calls += 1
                return [{"conid": 4815747}]

        transport = NoPriceTransport()
        provider = IbkrMarketDataProvider(transport, snapshot_attempts=3, snapshot_retry_delay=0)

        with self.assertRaisesRegex(IncompleteDataError, "precio del subyacente"):
            provider.get_underlying_by_conid("NVDA", "4815747")
        self.assertEqual(transport.calls, 4)  # pre-flight + tres intentos de datos

    def test_complete_workflow_uses_provider_and_prints_quotes(self):
        transport = FakeTransport()
        lines = []
        run(IbkrMarketDataProvider(transport), "nvda", "2026-09", 2, output=lines.append)
        text = "\n".join(lines)
        self.assertIn("Ticker: NVDA (conid 4815747)", text)
        self.assertIn("Precio subyacente: 101.25", text)
        self.assertIn("Strikes PUT seleccionados: 100, 105", text)
        self.assertIn("-0.25 | -0.04 | 0.32 | 1200", text)
        self.assertIn("RpB (RealTime, book disponible)", text)
        self.assertEqual([call[0] for call in transport.calls].count("/iserver/secdef/info"), 2)

    def test_derivative_workflow_requires_search_and_orders_it_before_market_data(self):
        transport = FakeTransport()
        provider = IbkrMarketDataProvider(transport, snapshot_retry_delay=0)
        with self.assertRaisesRegex(IncompleteDataError, "secdef/search"):
            provider.get_put_strikes("4815747", __import__("datetime").date(2026, 9, 1))

        run(provider, "NVDA", "2026-09", 1, output=lambda _: None)
        paths = [path for path, _ in transport.calls]
        search_index = paths.index("/iserver/secdef/search")
        first_derivative_snapshot = next(
            index for index, (path, params) in enumerate(transport.calls)
            if path == "/iserver/marketdata/snapshot" and params.get("conids") != "4815747"
        )
        self.assertLess(search_index, paths.index("/iserver/secdef/strikes"))
        self.assertLess(search_index, paths.index("/iserver/secdef/info"))
        self.assertLess(search_index, first_derivative_snapshot)

    def test_unauthenticated_session_has_specific_error(self):
        provider = IbkrMarketDataProvider(FakeTransport(authenticated=False))
        with self.assertRaises(NotAuthenticatedError):
            provider.require_authenticated_session()

    def test_unknown_ticker_has_specific_error(self):
        class EmptyTransport:
            def get(self, path, params): return []
        with self.assertRaises(TickerNotFoundError):
            IbkrMarketDataProvider(EmptyTransport()).locate_stock("NOPE")

    def test_partial_expiration_data_has_specific_error(self):
        class PartialTransport:
            def get(self, path, params): return [{"symbol": "NVDA", "conid": 1, "sections": []}]
        with self.assertRaises(IncompleteDataError):
            IbkrMarketDataProvider(PartialTransport()).locate_stock("NVDA")

    def test_market_data_permission_error_is_recognized(self):
        class UnauthorizedTransport:
            def get(self, path, params): return [{"conid": "1", "error": "Market data subscription required"}]
        provider = IbkrMarketDataProvider(UnauthorizedTransport())
        with self.assertRaises(MarketDataUnauthorizedError):
            provider.get_put_quotes((("1", 100.0),), __import__("datetime").date(2026, 9, 1))

    @patch("options_scanner.ibkr.urlopen", side_effect=URLError("connection refused"))
    def test_transport_wraps_unavailable_gateway(self, mocked_urlopen):
        with self.assertRaisesRegex(GatewayUnavailableError, "no se pudo conectar"):
            ClientPortalTransport().get("/iserver/auth/status", {})

    @patch("options_scanner.ibkr.ssl._create_unverified_context")
    def test_insecure_tls_must_be_enabled_explicitly(self, create_context):
        marker = object()
        create_context.return_value = marker
        transport = ClientPortalTransport(allow_insecure_tls=True)
        self.assertIs(transport._ssl_context, marker)

    def test_main_reports_expected_errors_without_traceback(self):
        stderr = StringIO()
        class BrokenProvider:
            def require_authenticated_session(self): raise GatewayUnavailableError("sin gateway")
        with patch("options_scanner.ibkr_diagnostic.ClientPortalTransport"), patch("options_scanner.ibkr_diagnostic.IbkrMarketDataProvider", return_value=BrokenProvider()), patch("sys.stderr", stderr):
            self.assertEqual(main([]), 2)
        self.assertIn("ERROR: sin gateway", stderr.getvalue())
