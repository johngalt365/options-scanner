from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import time
from unittest import TestCase
from options_scanner.multi_scan import run_multi_ticker
from options_scanner.scan_service import ScanMetrics, ScanResult

def result(elapsed=.01, *, timed_out=False):
    return ScanResult((), ScanMetrics(timed_out=timed_out), elapsed,
                      updated_at=datetime.now(timezone.utc))

class MultiTickerScanTest(TestCase):
    def test_global_limit_includes_internal_concurrency_and_has_no_deadlock(self):
        lock = threading.Lock()
        active = maximum = 0
        def scan(ticker, limiter):
            nonlocal active, maximum
            def request(_):
                nonlocal active, maximum
                with limiter:
                    with lock:
                        active += 1; maximum = max(maximum, active)
                    time.sleep(.003)
                    with lock:
                        active -= 1
            with ThreadPoolExecutor(max_workers=8) as inner:
                tuple(inner.map(request, range(8)))
            return result()
        items, metrics = run_multi_ticker(tuple(f"T{i}" for i in range(14)), scan,
                                          ticker_workers=4, global_http_limit=5)
        self.assertEqual(len(items), 14)
        self.assertLessEqual(maximum, 5)
        self.assertEqual(metrics.global_http_limit, 5)

    def test_isolation_timeout_order_and_accounting(self):
        symbols = ("A", "FAIL", "SLOW", "D")
        def scan(ticker, limiter):
            if ticker == "FAIL": raise RuntimeError("private payload")
            return result(.04 if ticker == "SLOW" else .01, timed_out=ticker == "SLOW")
        items, metrics = run_multi_ticker(symbols, scan, ticker_workers=3)
        self.assertEqual(tuple(item[0] for item in items), symbols)
        self.assertIsInstance(items[1][2], RuntimeError)
        self.assertTrue(items[2][1].summary.timed_out)
        self.assertIsNotNone(items[3][1])
        self.assertEqual((metrics.ticker_seconds_p50, metrics.ticker_seconds_p95), (.01, .04))

    def test_sequential_and_concurrent_results_are_equivalent_for_14_tickers(self):
        symbols = tuple(f"T{i:02d}" for i in range(14))
        def scan(ticker, limiter): return result(int(ticker[1:]) / 100)
        sequential, _ = run_multi_ticker(symbols, scan, ticker_workers=1)
        concurrent, _ = run_multi_ticker(symbols, scan, ticker_workers=4)
        self.assertEqual([(t, r.elapsed_seconds) for t, r, _ in sequential],
                         [(t, r.elapsed_seconds) for t, r, _ in concurrent])
