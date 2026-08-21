"""Synthetic latency benchmark; it does not predict Client Portal performance."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from options_scanner.multi_scan import run_multi_ticker
from options_scanner.scan_service import ScanMetrics, ScanResult

TICKERS = tuple(f"T{i:02d}" for i in range(14))

def simulated_scan(ticker, limiter):
    started = time.monotonic()
    with limiter:
        time.sleep(.010)
    def request(_):
        with limiter:
            time.sleep(.020)
    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(request, range(8)))
    with limiter:
        time.sleep(.010)
    return ScanResult((), ScanMetrics(), time.monotonic() - started,
                      updated_at=datetime.now(timezone.utc))

if __name__ == "__main__":
    print("Benchmark sintético (14 tickers, 8 workers internos, límite HTTP global=8)")
    for workers in (1, 2, 3, 4):
        _, metrics = run_multi_ticker(TICKERS, simulated_scan, ticker_workers=workers,
                                      global_http_limit=8)
        print(f"ticker_workers={workers}: total={metrics.elapsed_seconds:.3f}s "
              f"p50={metrics.ticker_seconds_p50:.3f}s p95={metrics.ticker_seconds_p95:.3f}s")
    print("Solo compara la orquestación con latencia simulada; no mide ni pronostica IBKR.")
