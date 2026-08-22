"""Deterministic local latency benchmark for contract resolution."""

from datetime import date
import threading
import time
from unittest import TestCase

from options_scanner.ibkr import IbkrMarketDataProvider


class SimulatedLatencyTransport:
    def __init__(self, delay=.01):
        self.delay = delay
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def get(self, path, params):
        if path.endswith("secdef/info"):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(self.delay)
            with self.lock:
                self.active -= 1
            strike = float(params["strike"])
            return [{"conid": int(9000 + strike), "symbol": "NVDA", "secType": "OPT",
                     "right": "P", "strike": strike, "maturityDate": "20260925"}]
        raise AssertionError(path)


def benchmark(workers, count=32, delay=.01):
    transport = SimulatedLatencyTransport(delay)
    provider = IbkrMarketDataProvider(transport)
    provider._searched_underlyings.add("10")
    accounting = []
    started = time.monotonic()
    contracts = provider.discover_put_contracts(
        "10", date(2026, 9, 1), tuple(range(50, 50 + count)), symbol="NVDA",
        max_workers=workers, accounting=accounting.append,
    )
    return time.monotonic() - started, transport.maximum, contracts, accounting[0]


class ContractResolutionPerformanceTest(TestCase):
    def test_simulated_latency_worker_benchmark(self):
        measurements = {workers: benchmark(workers) for workers in (1, 2, 4, 8, 16)}
        for workers, (_, maximum, contracts, metrics) in measurements.items():
            self.assertEqual(len(contracts), 32)
            self.assertLessEqual(maximum, workers)
            self.assertEqual(metrics.max_concurrent_requests, maximum)
        # Prove actual overlap instead of comparing scheduler-sensitive wall time.
        self.assertGreaterEqual(measurements[8][1], 4)
        self.assertGreaterEqual(measurements[16][1], 8)

    def test_absolute_worker_cap_metrics_cache_and_deduplication(self):
        elapsed, maximum, _, metrics = benchmark(99, count=20, delay=.005)
        self.assertLess(elapsed, .1)
        self.assertLessEqual(maximum, 16)
        self.assertGreater(maximum, 4)
        self.assertEqual((metrics.candidate_strikes, metrics.info_calls,
                          metrics.validations_succeeded, metrics.validations_failed),
                         (20, 20, 20, 0))

        transport = SimulatedLatencyTransport(0)
        provider = IbkrMarketDataProvider(transport)
        provider._searched_underlyings.add("10")
        first, second = [], []
        provider.discover_put_contracts("10", date(2026, 9, 1), (80, 80, 81),
                                        symbol="NVDA", accounting=first.append)
        provider.discover_put_contracts("10", date(2026, 9, 1), (81, 80),
                                        symbol="NVDA", accounting=second.append)
        self.assertEqual((first[0].deduplicated, first[0].info_calls), (1, 2))
        self.assertEqual((second[0].cache_hits, second[0].info_calls), (2, 0))
        self.assertEqual(second[0].resolved + second[0].failed + second[0].unresolved_timeout,
                         second[0].target)
