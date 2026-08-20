"""Diagnóstico manual, de solo lectura, de Client Portal Gateway."""

from __future__ import annotations

import argparse
from datetime import date
import sys

from options_scanner.ibkr import ClientPortalTransport, IbkrError, IbkrMarketDataProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Comprueba market data de opciones PUT mediante IBKR Client Portal Gateway")
    parser.add_argument("--symbol", default="NVDA")
    parser.add_argument("--base-url", default="https://localhost:5000/v1/api")
    parser.add_argument("--insecure-tls", action="store_true", help="acepta explícitamente el certificado local/self-signed (solo desarrollo)")
    parser.add_argument("--expiration", help="mes YYYY-MM; por defecto selecciona el primero disponible")
    parser.add_argument("--contracts", type=int, default=3, help="número máximo de strikes/contratos")
    return parser


def run(provider: IbkrMarketDataProvider, symbol: str, expiration: str | None, limit: int, *, output=print) -> None:
    provider.require_authenticated_session()
    conid, expirations = provider.locate_stock(symbol)
    output(f"Ticker: {symbol.upper()} (conid {conid})")
    underlying = provider.get_underlying_by_conid(symbol, conid)
    output(f"Precio subyacente: {underlying.current_price:g}")
    output("Vencimientos: " + ", ".join(item.strftime("%Y-%m") for item in expirations))
    selected = _select_expiration(expirations, expiration)
    output(f"Vencimiento seleccionado: {selected:%Y-%m}")
    strikes = provider.get_put_strikes(conid, selected)
    nearby = tuple(sorted(strikes, key=lambda strike: abs(strike - underlying.current_price))[:max(1, limit)])
    output("Strikes PUT seleccionados: " + ", ".join(f"{strike:g}" for strike in nearby))
    contracts = provider.get_put_contracts(conid, selected, nearby)
    output(f"Contratos PUT encontrados: {len(contracts)}")
    output("conid | strike | bid | ask | delta | theta | IV | open interest")
    for quote in provider.get_put_quotes(contracts, selected):
        values = (quote.conid, quote.strike, quote.bid, quote.ask, quote.delta, quote.theta, quote.implied_volatility, quote.open_interest)
        output(" | ".join(_display(value) for value in values))


def _select_expiration(expirations: tuple[date, ...], requested: str | None) -> date:
    if requested is None:
        return min(expirations)
    try:
        year, month = map(int, requested.split("-"))
        wanted = date(year, month, 1)
    except (ValueError, TypeError) as exc:
        raise ValueError("--expiration debe tener formato YYYY-MM") from exc
    if wanted not in expirations:
        raise ValueError(f"el vencimiento {requested} no está disponible")
    return wanted


def _display(value: object | None) -> str:
    return "N/D" if value is None else f"{value:g}" if isinstance(value, float) else str(value)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    provider = IbkrMarketDataProvider(ClientPortalTransport(args.base_url, allow_insecure_tls=args.insecure_tls))
    try:
        run(provider, args.symbol, args.expiration, args.contracts)
    except (IbkrError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
