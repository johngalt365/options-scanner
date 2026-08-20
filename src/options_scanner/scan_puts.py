"""CLI de solo lectura para buscar PUTs de NVDA (o un ticker configurable)."""

from __future__ import annotations

import argparse
from datetime import date

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
    return parser.parse_args()


def _ibkr_candidates(provider: IbkrMarketDataProvider, args: argparse.Namespace, today: date) -> list[PutScanCandidate]:
    provider.require_authenticated_session()
    conid, months = provider.locate_stock(args.ticker)
    underlying = provider.get_underlying_by_conid(args.ticker, conid)
    result: list[PutScanCandidate] = []
    for month in months:
        strikes = tuple(
            strike for strike in provider.get_put_strikes(conid, month)
            if safety_margin(underlying.current_price, strike) >= args.min_safety_margin
        )
        contracts = provider.discover_put_contracts(conid, month, strikes, symbol=args.ticker)
        by_expiration: dict[date, list[tuple[str, float]]] = {}
        for contract in contracts:
            expiration = provider.contract_expiration(contract)
            if args.min_dte <= (expiration - today).days <= args.max_dte:
                by_expiration.setdefault(expiration, []).append((contract.conid, contract.strike))
        for expiration, pairs in by_expiration.items():
            for quote in provider.get_put_quotes(pairs, expiration):
                candidate = PutScanCandidate(
                    args.ticker.upper(), expiration, (expiration - today).days, quote.strike,
                    underlying.current_price, safety_margin(underlying.current_price, quote.strike),
                    quote.bid, quote.ask, quote.delta, quote.gamma, quote.theta, quote.vega,
                    quote.implied_volatility, quote.open_interest, quote.market_data_availability.display,
                )
                if quote.delta is None or args.min_abs_delta <= abs(quote.delta) <= args.max_abs_delta:
                    result.append(candidate)
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
        provider = IbkrMarketDataProvider(ClientPortalTransport(args.base_url, allow_insecure_tls=args.insecure))
        candidates = _ibkr_candidates(provider, args, today)
    ranked = rank_candidates(candidates)
    print("CANDIDATOS COMPLETOS (ranking por annualized_premium_yield)")
    _print(ranked)
    incomplete = [candidate for candidate in candidates if not candidate.complete]
    if incomplete:
        print("\nCANDIDATOS INCOMPLETOS (fuera del ranking)")
        _print(incomplete)


if __name__ == "__main__":
    main()
