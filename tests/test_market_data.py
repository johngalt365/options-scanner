from datetime import date
from unittest import TestCase
from options_scanner.ibkr import IbkrMarketDataProvider
from options_scanner.market_data import FakeMarketDataProvider, MarketDataProvider
from options_scanner.models import OptionType
from options_scanner.scanner import scan_puts

class FakeProviderScannerTest(TestCase):
    def test_fake_implements_port_and_scanner_is_reproducible(self):
        provider = FakeMarketDataProvider()
        self.assertIsInstance(provider, MarketDataProvider)
        result = scan_puts(provider, "NVDA", date(2026, 8, 20))
        self.assertEqual([q.contract.strike for q in result], [75.0, 80.0])

class StubTransport:
    def __init__(self): self.calls = []
    def get(self, path, params):
        self.calls.append((path, params))
        if "snapshot" in path: return {"last": "100.50"}
        return {"options": [{"conid": 987, "right": "P", "strike": "80", "expiration": "2026-09-24", "bid": "1.75", "ask": "1.90", "delta": "-0.25", "gamma": "0.016", "theta": "-0.05", "vega": "0.09", "iv": "0.35", "volume": "310", "open_interest": "1750"}]}

class IbkrMappingTest(TestCase):
    def test_maps_transport_payload_to_internal_immutable_models(self):
        transport = StubTransport(); provider = IbkrMarketDataProvider(transport)
        underlying = provider.get_underlying("nvda"); quote = provider.get_option_market_data("nvda")[0]
        self.assertEqual((underlying.symbol, underlying.current_price), ("NVDA", 100.5))
        self.assertEqual((quote.contract.id, quote.contract.option_type), ("987", OptionType.PUT))
        self.assertEqual((quote.bid, quote.ask, quote.delta, quote.implied_volatility), (1.75, 1.9, -0.25, 0.35))
        self.assertEqual((quote.volume, quote.open_interest), (310, 1750))
        self.assertEqual(len(transport.calls), 2)
