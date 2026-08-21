"""Bounded, deterministic orchestration for isolated per-ticker scans."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import math
import threading
import time
from typing import Callable, Sequence

from options_scanner.scan_service import ScanResult


@dataclass(frozen=True, slots=True)
class MultiScanMetrics:
    elapsed_seconds: float
    ticker_seconds_p50: float
    ticker_seconds_p95: float
    ticker_workers: int
    global_http_limit: int


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def run_multi_ticker(
    tickers: Sequence[str], scan: Callable[[str, threading.BoundedSemaphore], ScanResult],
    *, ticker_workers: int = 3, global_http_limit: int = 8,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[tuple[tuple[str, ScanResult | None, Exception | None], ...], MultiScanMetrics]:
    """Run isolated scans, returning results in the input order.

    The shared semaphore bounds all Gateway work, including each scan's internal
    ``secdef/info`` executor. Exceptions are values so one ticker cannot cancel peers.
    """
    if not 1 <= ticker_workers <= 4:
        raise ValueError("ticker_workers debe estar entre 1 y 4")
    if not 1 <= global_http_limit <= 16:
        raise ValueError("global_http_limit debe estar entre 1 y 16")
    limiter = threading.BoundedSemaphore(global_http_limit)
    started = clock()

    def one(ticker: str) -> tuple[str, ScanResult | None, Exception | None]:
        try:
            return ticker, scan(ticker, limiter), None
        except Exception as exc:  # isolation is the public contract of this layer
            return ticker, None, exc

    with ThreadPoolExecutor(max_workers=ticker_workers, thread_name_prefix="ticker-scan") as executor:
        items = tuple(executor.map(one, tickers))
    durations = [result.elapsed_seconds for _, result, _ in items if result is not None]
    metrics = MultiScanMetrics(max(0.0, clock() - started), _percentile(durations, .50),
                               _percentile(durations, .95), ticker_workers, global_http_limit)
    return items, metrics
