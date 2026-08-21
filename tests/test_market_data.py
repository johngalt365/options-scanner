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
    def __init__(self): self.calls = []; self.snapshots = {}
    def get(self, path, params):
        self.calls.append((path, params))
        if path.endswith("secdef/search"):
            return [{"symbol": "NVDA", "conid": 1, "sections": [{"secType": "OPT", "months": "SEP26"}]}]
        if path.endswith("secdef/strikes"): return {"put": [80]}
        if path.endswith("secdef/info"):
            return [{"conid": 987, "symbol": "NVDA", "secType": "OPT", "right": "P", "strike": 80, "maturityDate": "20260924"}]
        conids = params["conids"]
        self.snapshots[conids] = self.snapshots.get(conids, 0) + 1
        if self.snapshots[conids] == 1: return [{"conid": int(conids)}]
        if conids == "1": return [{"conid": 1, "31": "100.50"}]
        conid = int(params["conids"])
        return [{"conid": conid, "84": "1.75", "86": "1.90", "7308": "-0.25", "7309": "0.016", "7310": "-0.05", "7311": "0.09", "7633": "35", "7638": "1750", "6509": "ZBd"}]

class IbkrMappingTest(TestCase):
    def test_etf_underlying_fallback_keeps_option_discovery_strict(self):
        class EtfTransport:
            def __init__(self): self.calls = []
            def get(self, path, params):
                self.calls.append((path, params.copy()))
                if params.get("secType") == "STK": return []
                return [{"symbol": "SPY", "conid": 756733, "secType": "ETF",
                         "sections": [{"secType": "OPT", "months": "SEP26"}]}]
        transport = EtfTransport()
        conid, expirations = IbkrMarketDataProvider(transport).locate_stock("spy")
        self.assertEqual(conid, "756733")
        self.assertTrue(expirations)
        self.assertEqual([params["secType"] for _, params in transport.calls], ["STK", "ETF"])

    def test_ibkr_percentage_points_are_normalized_to_canonical_fraction(self):
        normalize = IbkrMarketDataProvider._canonical_implied_volatility
        for wire_value, canonical in ((0.0, 0.0), (12.5, .125), (48.2, .482), (100.0, 1.0)):
            with self.subTest(wire_value=wire_value):
                self.assertAlmostEqual(normalize(wire_value), canonical)
        self.assertIsNone(normalize(None))

    def test_maps_transport_payload_to_internal_immutable_models(self):
        transport = StubTransport(); provider = IbkrMarketDataProvider(transport)
        underlying = provider.get_underlying("nvda"); quote = provider.get_option_market_data("nvda")[0]
        self.assertEqual((underlying.symbol, underlying.current_price), ("NVDA", 100.5))
        self.assertEqual((quote.contract.id, quote.contract.option_type), ("987", OptionType.PUT))
        self.assertEqual((quote.bid, quote.ask, quote.delta, quote.implied_volatility), (1.75, 1.9, -0.25, 0.35))
        self.assertEqual((quote.gamma, quote.theta, quote.vega), (0.016, -0.05, 0.09))
        self.assertEqual((quote.volume, quote.open_interest), (0, 1750))
        self.assertEqual(quote.market_data_availability, "ZBd")

    def test_theta_wire_sign_is_preserved_for_both_signs(self):
        for wire_theta in ("-0.134", "-0.112", "+0.118"):
            transport = StubTransport()
            original_get = transport.get

            def get(path, params, *, _original=original_get, _theta=wire_theta):
                rows = _original(path, params)
                for row in rows:
                    if "7310" in row:
                        row["7310"] = _theta
                return rows

            transport.get = get
            quote = IbkrMarketDataProvider(transport).get_option_market_data("NVDA")[0]
            self.assertEqual(quote.theta, float(wire_theta))

    def test_ambiguous_month_uses_only_exactly_confirmed_contracts(self):
        class AmbiguousTransport(StubTransport):
            def get(self, path, params):
                if path.endswith("secdef/info"):
                    self.calls.append((path, params))
                    return [
                        {"conid": 111, "symbol": "NVDA", "secType": "OPT", "right": "C", "strike": 80, "maturityDate": "20260924"},
                        {"conid": 112, "symbol": "NVDA", "secType": "OPT", "right": "P", "strike": 81, "maturityDate": "20260924"},
                        {"conid": 222, "symbol": "NVDA", "secType": "OPT", "right": "P", "strike": 80, "maturityDate": "20260924"},
                        {"conid": 333, "symbol": "NVDA", "secType": "OPT", "right": "P", "strike": 80, "maturityDate": "20261001"},
                    ]
                return super().get(path, params)

        transport = AmbiguousTransport()
        quote = IbkrMarketDataProvider(transport, snapshot_retry_delay=0).get_option_market_data("NVDA")[0]
        snapshot_calls = [params for path, params in transport.calls if path.endswith("marketdata/snapshot")]
        self.assertTrue(all(params["conids"] == "222" for params in snapshot_calls))
        self.assertEqual((quote.contract.id, quote.contract.expiration), ("222", date(2026, 9, 24)))
