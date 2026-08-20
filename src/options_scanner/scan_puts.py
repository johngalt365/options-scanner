"""CLI de solo lectura para buscar PUTs de NVDA (o un ticker configurable)."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date
import logging
import time
from typing import Callable

from options_scanner.filters import safety_margin
from options_scanner.ibkr import ClientPortalTransport, IbkrMarketDataProvider
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
    parser.add_argument("--base-url", default="https://localhost:5000/v1/api")
    parser.add_argument("--insecure", action="store_true", help="acepta el certificado TLS local")
    parser.add_argument("--fake", action="store_true", help="usa datos deterministas sin conectar a IBKR")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--snapshot-attempts", type=int, default=2)
    parser.add_argument("--contract-workers", type=int, default=4,
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
    unresolved_contracts_timeout: int = 0
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
    for month in months:
        if expired(discovery_deadline, "expirations_strikes"):
            break
        all_strikes = provider.get_put_strikes(conid, month)
        finish_phase("expirations_strikes", phase_started)
        phase_started = clock()
        strikes = tuple(
            strike for strike in all_strikes
            if safety_margin(underlying.current_price, strike) >= args.min_safety_margin
        )
        stats.rejected_margin += len(all_strikes) - len(strikes)
        stats.target_contracts += len(strikes)
        finish_phase("dte_margin_filter", phase_started)
        phase_started = clock()
        resolution_progress = (
            (lambda current, total: print(f"Resolviendo contratos {current}/{total}"))
            if show_progress else None
        )
        contracts = provider.discover_put_contracts(
            conid, month, strikes, symbol=args.ticker,
            deadline=discovery_deadline, progress=resolution_progress,
            max_workers=getattr(args, "contract_workers", 4),
        )
        for contract in contracts:
            expiration = provider.contract_expiration(contract)
            if args.min_dte <= (expiration - today).days <= args.max_dte:
                confirmed.append((contract.conid, contract.strike, expiration))
        stats.resolved_contracts += len(contracts)
        finish_phase("contract_resolution", phase_started)
        phase_started = clock()
        if expired(discovery_deadline, "contract_resolution"):
            stats.unresolved_contracts_timeout = max(
                0, stats.target_contracts - stats.resolved_contracts
            )
            break

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
    today = date.today()
    if args.fake:
        provider = FakeMarketDataProvider()
        quotes = scan_puts(provider, args.ticker, today, min_dte=args.min_dte, max_dte=args.max_dte,
                           min_safety_margin=args.min_safety_margin, min_abs_delta=args.min_abs_delta,
                           max_abs_delta=args.max_abs_delta)
        candidates = build_candidates(provider.get_underlying(args.ticker).current_price, quotes, today)
    else:
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.ERROR)
        provider = IbkrMarketDataProvider(ClientPortalTransport(
            args.base_url, allow_insecure_tls=args.insecure,
            timeout=max(0.1, min(10.0, args.scan_timeout)),
        ))
        summary = ScanSummary()
        candidates = _ibkr_candidates(provider, args, today, summary=summary)
    rank_started = time.monotonic()
    ranked = rank_candidates(candidates)
    if not args.fake:
        summary.phase_seconds["filtering_ranking"] = summary.phase_seconds.get("filtering_ranking", 0.0) + (time.monotonic() - rank_started)
    print("CANDIDATOS COMPLETOS (ranking por annualized_premium_yield)")
    _print(ranked)
    incomplete = [candidate for candidate in candidates if not candidate.complete]
    if incomplete:
        print("\nCANDIDATOS INCOMPLETOS (fuera del ranking)")
        _print(incomplete)
    if not args.fake:
        print(
            "\nRESUMEN: "
            f"considerados={summary.considered} completos={summary.complete} "
            f"incompletos={summary.incomplete} rechazados_por_margen={summary.rejected_margin} "
            f"rechazados_por_delta={summary.rejected_delta} timeout={'sí' if summary.timed_out else 'no'} "
            f"timeout_phase={summary.timeout_phase or 'ninguna'} "
            f"contratos_objetivo={summary.target_contracts} "
            f"contratos_resueltos={summary.resolved_contracts} "
            f"contratos_no_resueltos_timeout={summary.unresolved_contracts_timeout}"
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
