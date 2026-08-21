"""CLI de solo lectura para buscar PUTs de NVDA (o un ticker configurable)."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date
import logging
import time
from typing import Callable

from options_scanner.filters import safety_margin
from options_scanner.ibkr import (
    ClientPortalTransport,
    ContractResolutionAccounting,
    IbkrMarketDataProvider,
)
from options_scanner.market_data import FakeMarketDataProvider
from options_scanner.scanner import PutScanCandidate, build_candidates, rank_candidates, scan_puts


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scanner read-only de venta de PUTs")
    parser.add_argument("--ticker", default="NVDA")
    parser.add_argument("--min-dte", type=int, default=30)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--min-safety-margin", type=float, default=.20)
    parser.add_argument("--min-abs-delta", type=float, default=.15)
    parser.add_argument("--max-abs-delta", type=float, default=.30)
    parser.add_argument("--min-iv", type=float, default=None,
                        help="IV mínima canónica (fracción decimal); desactivada por defecto")
    parser.add_argument("--min-short-theta", type=float, default=None,
                        help="theta mínimo de la posición corta; desactivado por defecto")
    parser.add_argument("--base-url", default="https://localhost:5000/v1/api")
    parser.add_argument("--insecure", action="store_true", help="acepta el certificado TLS local")
    parser.add_argument("--fake", action="store_true", help="usa datos deterministas sin conectar a IBKR")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--snapshot-attempts", type=int, default=2)
    parser.add_argument("--contract-workers", type=int, default=8,
                        help="máximo de secdef/info simultáneos (máximo de seguridad: 16)")
    parser.add_argument("--scan-timeout", type=float, default=30.0, help="límite global en segundos")
    parser.add_argument("--market-data-timeout", type=float, default=10.0,
                        help="presupuesto reservado para snapshots dentro del límite global")
    parser.add_argument("--progress", action="store_true", help="muestra progreso del scanner real")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


@dataclass(slots=True)
class ScanSummary:
    considered: int = 0
    complete: int = 0
    incomplete: int = 0
    rejected_margin: int = 0
    rejected_delta: int = 0
    timed_out: bool = False
    timeout_phase: str | None = None
    target_contracts: int = 0
    resolved_contracts: int = 0
    failed_contracts: int = 0
    unresolved_contracts_timeout: int = 0
    deduplicated_contracts: int = 0
    candidate_strikes: int = 0
    secdef_info_calls: int = 0
    contract_cache_hits: int = 0
    contract_validations_succeeded: int = 0
    contract_validations_failed: int = 0
    secdef_info_latency_mean_ms: float = 0.0
    secdef_info_latency_p50_ms: float = 0.0
    secdef_info_latency_p95_ms: float = 0.0
    max_concurrent_contract_requests: int = 0
    with_bid_ask_delta: int = 0
    with_bid_ask_without_delta: int = 0
    with_delta_without_bid_ask: int = 0
    without_bid_ask_delta: int = 0
    market_data_realtime: int = 0
    market_data_frozen: int = 0
    market_data_delayed: int = 0
    market_data_not_subscribed: int = 0
    market_data_unknown: int = 0
    phase_seconds: dict[str, float] = field(default_factory=dict)


def _ibkr_candidates(
    provider: IbkrMarketDataProvider,
    args: argparse.Namespace,
    today: date,
    *,
    summary: ScanSummary | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[PutScanCandidate]:
    stats = summary if summary is not None else ScanSummary()
    started = clock()
    timeout = max(0.0, getattr(args, "scan_timeout", 30.0))
    global_deadline = started + timeout
    market_budget = min(timeout, max(0.0, getattr(args, "market_data_timeout", 10.0)))
    # Contract discovery cannot borrow the market-data reserve. This avoids a
    # nominally global deadline reaching snapshots with only milliseconds left.
    discovery_deadline = global_deadline - market_budget
    show_progress = getattr(args, "progress", False)

    def finish_phase(name: str, phase_started: float) -> None:
        stats.phase_seconds[name] = stats.phase_seconds.get(name, 0.0) + max(0.0, clock() - phase_started)

    def expired(deadline: float, phase: str) -> bool:
        if clock() >= deadline:
            stats.timed_out = True
            stats.timeout_phase = phase
            return True
        return False

    phase_started = clock()
    provider.require_authenticated_session()
    underlying, conid, months = provider.resolve_underlying(args.ticker, deadline=discovery_deadline)
    finish_phase("underlying_resolution", phase_started)
    if expired(discovery_deadline, "underlying_resolution"):
        return []

    confirmed: list[tuple[str, float, date]] = []
    phase_started = clock()
    # Search only provides monthly buckets.  Prefer buckets nearest the DTE
    # window, then strikes nearest the configured margin boundary.  This only
    # changes submission order: without a timeout every contract is retained.
    target_dte = (args.min_dte + args.max_dte) / 2
    ordered_months = sorted(months, key=lambda value: (abs((value - today).days - target_dte), value))
    latency_samples: list[tuple[float, int]] = []
    for month in ordered_months:
        if expired(discovery_deadline, "expirations_strikes"):
            break
        all_strikes = provider.get_put_strikes(conid, month)
        finish_phase("expirations_strikes", phase_started)
        phase_started = clock()
        strikes = tuple(sorted((
            strike for strike in all_strikes
            if safety_margin(underlying.current_price, strike) >= args.min_safety_margin
        ), key=lambda strike: (abs(safety_margin(underlying.current_price, strike) - args.min_safety_margin), strike)))
        stats.rejected_margin += len(all_strikes) - len(strikes)
        finish_phase("dte_margin_filter", phase_started)
        phase_started = clock()
        resolution_progress = (
            (lambda current, total: print(f"Resolviendo contratos {current}/{total}"))
            if show_progress else None
        )
        resolution_accounting: list[ContractResolutionAccounting] = []
        contracts = provider.discover_put_contracts(
            conid, month, strikes, symbol=args.ticker,
            deadline=discovery_deadline, progress=resolution_progress,
            accounting=resolution_accounting.append,
            max_workers=getattr(args, "contract_workers", 8),
        )
        for contract in contracts:
            expiration = provider.contract_expiration(contract)
            if args.min_dte <= (expiration - today).days <= args.max_dte:
                confirmed.append((contract.conid, contract.strike, expiration))
        deadline_reached = clock() >= discovery_deadline
        if resolution_accounting:
            account = resolution_accounting[0]
            stats.target_contracts += account.target
            stats.resolved_contracts += account.resolved
            stats.failed_contracts += account.failed
            stats.unresolved_contracts_timeout += account.unresolved_timeout
            stats.deduplicated_contracts += account.deduplicated
            stats.candidate_strikes += account.candidate_strikes
            stats.secdef_info_calls += account.info_calls
            stats.contract_cache_hits += account.cache_hits
            stats.contract_validations_succeeded += account.validations_succeeded
            stats.contract_validations_failed += account.validations_failed
            stats.max_concurrent_contract_requests = max(stats.max_concurrent_contract_requests, account.max_concurrent_requests)
            if account.info_calls:
                latency_samples.append((account.info_latency_mean_ms, account.info_calls))
                # Per-group approximations; raw timings remain private inside provider.
                stats.secdef_info_latency_p50_ms = max(stats.secdef_info_latency_p50_ms, account.info_latency_p50_ms)
                stats.secdef_info_latency_p95_ms = max(stats.secdef_info_latency_p95_ms, account.info_latency_p95_ms)
        else:  # Compatibility with injected/third-party providers.
            stats.target_contracts += len(tuple(dict.fromkeys(strikes)))
            stats.resolved_contracts += len(contracts)
            remainder = max(0, len(tuple(dict.fromkeys(strikes))) - len(contracts))
            if deadline_reached:
                stats.unresolved_contracts_timeout += remainder
            else:
                stats.failed_contracts += remainder
        finish_phase("contract_resolution", phase_started)
        phase_started = clock()
        # Passing a soft submission deadline is not a partial timeout when all
        # unique validations reached a terminal state while their futures drained.
        if deadline_reached and stats.unresolved_contracts_timeout:
            stats.timed_out = True
            stats.timeout_phase = "contract_resolution"
            break

    if latency_samples:
        calls = sum(count for _, count in latency_samples)
        stats.secdef_info_latency_mean_ms = sum(mean * count for mean, count in latency_samples) / calls

    stats.considered = len(confirmed)
    if not confirmed:
        return []

    # The reserved budget starts here, independently of time spent resolving.
    market_deadline = min(global_deadline, clock() + market_budget)
    expiration_by_conid = {contract_id: expiration for contract_id, _, expiration in confirmed}
    pairs = tuple((contract_id, strike) for contract_id, strike, _ in confirmed)
    progress = (lambda current, total: print(f"Market data batch {current}/{total}")) if show_progress else None
    phase_started = clock()
    quotes = provider.get_put_quotes_batched(
        pairs, today, batch_size=getattr(args, "batch_size", 50),
        attempts=getattr(args, "snapshot_attempts", 2), deadline=market_deadline,
        progress=progress, verbose=getattr(args, "verbose", False),
    )
    finish_phase("market_data_snapshots", phase_started)
    if clock() >= market_deadline:
        stats.timed_out = True
        stats.timeout_phase = "market_data_snapshots"

    phase_started = clock()
    result: list[PutScanCandidate] = []
    for quote in quotes:
        has_book = quote.bid is not None and quote.ask is not None
        has_delta = quote.delta is not None
        if has_book and has_delta:
            stats.with_bid_ask_delta += 1
        elif has_book:
            stats.with_bid_ask_without_delta += 1
        elif has_delta:
            stats.with_delta_without_bid_ask += 1
        else:
            stats.without_bid_ask_delta += 1
        feed = quote.market_data_availability.feed
        if feed == "RealTime":
            stats.market_data_realtime += 1
        elif feed == "Frozen":
            stats.market_data_frozen += 1
        elif feed in ("Delayed", "Frozen-Delayed"):
            stats.market_data_delayed += 1
        elif feed == "Not Subscribed":
            stats.market_data_not_subscribed += 1
        else:
            stats.market_data_unknown += 1
        expiration = expiration_by_conid[quote.conid]
        candidate = PutScanCandidate(
            args.ticker.upper(), expiration, (expiration - today).days, quote.strike,
            underlying.current_price, safety_margin(underlying.current_price, quote.strike),
            quote.bid, quote.ask, quote.delta, quote.gamma, quote.theta, quote.vega,
            quote.implied_volatility, quote.open_interest, quote.market_data_availability.display,
        )
        if quote.delta is None:
            stats.incomplete += 1
            result.append(candidate)
        elif not args.min_abs_delta <= abs(quote.delta) <= args.max_abs_delta:
            stats.rejected_delta += 1
        elif getattr(args, "min_iv", None) is not None and (
            quote.implied_volatility is None or quote.implied_volatility < args.min_iv
        ):
            continue
        elif getattr(args, "min_short_theta", None) is not None and (
            quote.theta is None or -quote.theta < args.min_short_theta
        ):
            continue
        else:
            result.append(candidate)
            if candidate.complete:
                stats.complete += 1
            else:
                stats.incomplete += 1
    # A global timeout can leave whole batches without even a placeholder row.
    stats.incomplete += max(0, stats.considered - stats.complete - stats.incomplete - stats.rejected_delta)
    finish_phase("filtering_ranking", phase_started)
    return result


def _cell(value: object, digits: int = 4) -> str:
    return "N/D" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def _print(candidates: list[PutScanCandidate]) -> None:
    columns = ("ticker", "expiration", "dte", "strike", "underlying", "safety%", "bid", "ask", "mid",
               "delta", "gamma", "theta", "vega", "IV", "OI", "6509", "yield%", "annualized%")
    print("\t".join(columns))
    for c in candidates:
        values = (c.ticker, c.expiration.isoformat(), c.dte, c.strike, c.underlying_price,
                  c.safety_margin * 100, c.bid, c.ask, c.mid, c.delta, c.gamma, c.theta, c.vega,
                  c.implied_volatility, c.open_interest, c.market_data_availability,
                  None if c.premium_yield is None else c.premium_yield * 100,
                  None if c.annualized_premium_yield is None else c.annualized_premium_yield * 100)
        print("\t".join(_cell(value) for value in values))


def main() -> None:
    args = _arguments()
    from options_scanner.scan_service import PutScanService, ScanRequest

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.ERROR)
    if args.fake:
        provider = FakeMarketDataProvider()
    else:
        provider = IbkrMarketDataProvider(ClientPortalTransport(
            args.base_url, allow_insecure_tls=args.insecure,
            timeout=max(0.1, min(10.0, args.scan_timeout)),
        ))
    result = PutScanService().run(
        ScanRequest(
            args.ticker, args.min_dte, args.max_dte, args.min_safety_margin,
            args.min_abs_delta, args.max_abs_delta, args.fake,
            min_iv=args.min_iv, min_short_theta=args.min_short_theta,
        ),
        base_url=args.base_url, allow_insecure_tls=args.insecure,
        scan_timeout=args.scan_timeout, market_data_timeout=args.market_data_timeout,
        provider=provider, batch_size=args.batch_size, snapshot_attempts=args.snapshot_attempts,
        contract_workers=args.contract_workers, progress=args.progress, verbose=args.verbose,
    )
    ranked = list(result.candidates)
    summary = result.summary
    print("CANDIDATOS COMPLETOS (ranking por annualized_premium_yield)")
    _print(ranked)
    if result.incomplete_candidates:
        print("\nCANDIDATOS INCOMPLETOS (fuera del ranking)")
        _print(list(result.incomplete_candidates))
    if not args.fake:
        print(
            "\nRESUMEN: "
            f"considerados={summary.considered} completos={summary.complete} "
            f"incompletos={summary.incomplete} rechazados_por_margen={summary.rejected_margin} "
            f"rechazados_por_delta={summary.rejected_delta} timeout={'sí' if summary.timed_out else 'no'} "
            f"timeout_phase={summary.timeout_phase or 'ninguna'} "
            f"contratos_objetivo={summary.target_contracts} "
            f"contratos_resueltos={summary.resolved_contracts} "
            f"contratos_fallidos={summary.failed_contracts} "
            f"contratos_no_resueltos_timeout={summary.unresolved_contracts_timeout} "
            f"contratos_deduplicados={summary.deduplicated_contracts}\n"
            f"strikes_candidatos={summary.candidate_strikes} secdef_info_calls={summary.secdef_info_calls} "
            f"cache_hits={summary.contract_cache_hits} validaciones_ok={summary.contract_validations_succeeded} "
            f"validaciones_fallidas={summary.contract_validations_failed} "
            f"secdef_info_ms_mean={summary.secdef_info_latency_mean_ms:.1f} "
            f"p50={summary.secdef_info_latency_p50_ms:.1f} p95={summary.secdef_info_latency_p95_ms:.1f} "
            f"concurrencia_max={summary.max_concurrent_contract_requests}\n"
            "MARKET_DATA: "
            f"con_bid_ask_delta={summary.with_bid_ask_delta} "
            f"con_bid_ask_sin_delta={summary.with_bid_ask_without_delta} "
            f"con_delta_sin_bid_ask={summary.with_delta_without_bid_ask} "
            f"sin_bid_ask_delta={summary.without_bid_ask_delta} "
            f"6509_RealTime={summary.market_data_realtime} "
            f"6509_Frozen={summary.market_data_frozen} "
            f"6509_Delayed={summary.market_data_delayed} "
            f"6509_Not_Subscribed={summary.market_data_not_subscribed} "
            f"6509_desconocido={summary.market_data_unknown}"
        )
        if args.progress:
            labels = (
                "underlying_resolution", "expirations_strikes", "dte_margin_filter",
                "contract_resolution", "market_data_snapshots", "filtering_ranking",
            )
            print("TIEMPOS: " + " ".join(f"{name}={summary.phase_seconds.get(name, 0.0):.3f}s" for name in labels))
        if args.progress or args.verbose:
            endpoints = (
                "secdef/search", "secdef/strikes", "secdef/info", "marketdata/snapshot",
                "marketdata/snapshot/underlying", "marketdata/snapshot/options",
            )
            print("HTTP: " + " ".join(f"{name}={provider.http_call_counts[name]}" for name in endpoints))


if __name__ == "__main__":
    main()
