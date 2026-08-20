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
    NotAuthenticatedError,
    TickerNotFoundError,
)
from options_scanner.ibkr_diagnostic import main, run


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
                {"conid": conid, "84": "1.1", "86": "1.2", "7308": "-0.25", "7310": "-0.04", "7633": "0.32", "7698": "1200"}
                for conid in params["conids"].split(",")
            ]
        raise AssertionError(path)


class DiagnosticTest(TestCase):
    def test_complete_workflow_uses_provider_and_prints_quotes(self):
        transport = FakeTransport()
        lines = []
        run(IbkrMarketDataProvider(transport), "nvda", "2026-09", 2, output=lines.append)
        text = "\n".join(lines)
        self.assertIn("Ticker: NVDA (conid 4815747)", text)
        self.assertIn("Precio subyacente: 101.25", text)
        self.assertIn("Strikes PUT seleccionados: 100, 105", text)
        self.assertIn("-0.25 | -0.04 | 0.32 | 1200", text)
        self.assertEqual([call[0] for call in transport.calls].count("/iserver/secdef/info"), 2)

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
