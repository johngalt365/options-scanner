import unittest

from ibkr_diagnostic.provider import (
    DataNotAuthorized, IbkrMarketDataProvider, IncompleteMarketData, SessionNotAuthenticated,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses

    def get(self, path, params=None):
        value = self.responses[path]
        return value(params) if callable(value) else value


class ProviderTest(unittest.TestCase):
    def provider(self):
        return IbkrMarketDataProvider(transport=FakeTransport({
            "/iserver/auth/status": {"authenticated": True, "connected": True},
            "/iserver/secdef/search": [{"symbol": "NVDA", "conid": 4815747, "sections": [{"secType": "OPT", "months": "SEP26;OCT26"}]}],
            "/iserver/marketdata/snapshot": lambda p: ([{"conid": 4815747, "31": "180.25"}] if p["conids"] == "4815747" else [{"conid": 11, "84": "2.1", "86": "2.2", "7308": "-0.4", "7310": "-0.1", "7633": "0.35", "7089": "120"}]),
            "/iserver/secdef/strikes": {"put": [175, 180, 185]},
            "/iserver/secdef/info": lambda p: [{"conid": 11, "strike": p["strike"]}],
        }))

    def test_complete_diagnostic_flow(self):
        provider = self.provider()
        provider.require_authenticated_session()
        stock = provider.find_stock("NVDA")
        self.assertEqual(provider.stock_price(stock["conid"]), 180.25)
        self.assertEqual(provider.option_expirations(stock)[0], "SEP26")
        strikes = provider.put_strikes(stock["conid"], "SEP26")
        contracts = provider.put_contracts(stock["conid"], "SEP26", strikes[:1])
        self.assertEqual(provider.option_market_data(contracts)[0]["delta"], -0.4)

    def test_unauthenticated_session(self):
        provider = IbkrMarketDataProvider(transport=FakeTransport({"/iserver/auth/status": {"authenticated": False}}))
        with self.assertRaises(SessionNotAuthenticated):
            provider.require_authenticated_session()

    def test_permission_error_and_incomplete_data(self):
        provider = IbkrMarketDataProvider(transport=FakeTransport({"/iserver/marketdata/snapshot": [{"error": "No market data permissions"}]}))
        with self.assertRaises(DataNotAuthorized):
            provider.snapshot([1])
        provider = IbkrMarketDataProvider(transport=FakeTransport({"/iserver/marketdata/snapshot": []}))
        with self.assertRaises(IncompleteMarketData):
            provider.stock_price(1)


if __name__ == "__main__":
    unittest.main()
