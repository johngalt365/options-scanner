"""Diagnóstico manual, de solo lectura, de Client Portal Gateway."""

from __future__ import annotations

import argparse
from datetime import date
import logging
import sys

from options_scanner.ibkr import (
    ClientPortalTransport,
    DeepSnapshotAttempt,
    IbkrError,
    IbkrMarketDataProvider,
    MarketDataFieldStatus,
    _is_explicitly_unavailable,
    _market_data_availability,
)
from options_scanner.ibkr_websocket import (
    ClientPortalWebSocket,
    compare_market_fields,
    observe_smd_stream,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Comprueba market data de opciones PUT mediante IBKR Client Portal Gateway")
    parser.add_argument("--symbol", default="NVDA")
    parser.add_argument("--base-url", default="https://localhost:5000/v1/api")
    parser.add_argument("--insecure-tls", action="store_true", help="acepta explícitamente el certificado local/self-signed (solo desarrollo)")
    parser.add_argument("--expiration", help="mes YYYY-MM; por defecto selecciona el primero disponible")
    parser.add_argument("--contracts", type=int, default=3, help="número máximo de strikes/contratos")
    parser.add_argument("--verbose", action="store_true", help="muestra el resumen seguro de cada respuesta snapshot")
    parser.add_argument("--deep", action="store_true", help="diagnóstico profundo de un único contrato PUT")
    parser.add_argument("--strike", type=float, help="strike PUT exacto (obligatorio con --deep)")
    parser.add_argument("--maturity", help="vencimiento exacto YYYY-MM-DD del PUT (recomendado si el mes tiene varios)")
    parser.add_argument("--websocket", action="store_true", help="compara el snapshot con el stream oficial smd")
    parser.add_argument("--stream-seconds", type=float, default=7.0, help="duración del stream smd (5–10 s recomendado)")
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
    output("conid | strike | 6509 Market Data Availability | bid | ask | delta | theta | IV | open interest")
    for quote in provider.get_put_quotes(contracts, selected):
        values = ((quote.conid, None), (quote.strike, None), (quote.market_data_availability.display, None)) + tuple(
            (getattr(quote, attribute), quote.field_statuses[name])
            for attribute, name in (
                ("bid", "bid"), ("ask", "ask"), ("delta", "delta"), ("theta", "theta"),
                ("implied_volatility", "implied_volatility"), ("open_interest", "open_interest"),
            )
        )
        output(" | ".join(_display(value, status) for value, status in values))


def run_deep(
    provider: IbkrMarketDataProvider,
    symbol: str,
    expiration: str | None,
    strike: float,
    *,
    maturity: str | None = None,
    websocket_factory=None,
    stream_seconds: float = 7.0,
    output=print,
) -> None:
    """Diagnostica exactamente un PUT y muestra solo fields de market data."""

    provider.require_authenticated_session()
    underlying_conid, expirations = provider.locate_stock(symbol)  # secdef/search obligatorio
    selected = _select_expiration(expirations, expiration)
    available = provider.get_put_strikes(underlying_conid, selected)
    matching = next((value for value in available if value == strike), None)
    if matching is None:
        raise ValueError(f"el strike PUT {strike:g} no está disponible para {selected:%Y-%m}")
    exact_maturity = _parse_maturity(maturity)
    contract = provider.confirm_put_contract(
        underlying_conid, symbol, selected, matching, exact_maturity=exact_maturity
    )
    contract_conid = contract.conid
    output(
        "Contrato PUT confirmado: "
        f"conid={contract.conid} symbol={contract.symbol} secType={contract.sec_type} "
        f"exchange={contract.exchange} listingExchange={contract.listing_exchange} "
        f"right={contract.right} strike={contract.strike:g} maturityDate={contract.maturity_date} "
        f"multiplier={contract.multiplier} tradingClass={contract.trading_class} "
        f"validExchanges={contract.valid_exchanges}"
    )
    output(f"Fields solicitados: {provider.DEEP_OPTION_SNAPSHOT_FIELDS}")
    snapshots = provider.diagnose_put_contract(underlying_conid, contract_conid)
    for observation in snapshots:
        output(_display_deep_attempt(observation))
    if websocket_factory is not None:
        stream = observe_smd_stream(websocket_factory(), contract_conid, stream_seconds)
        for observation in stream:
            values = " | ".join(f"{field}={value}" for field, value in observation.fields.items())
            output(f"WebSocket +{observation.elapsed_seconds:.3f}s: {values}")
        comparison = compare_market_fields(snapshots, stream)
        output("Comparación fields: " + " | ".join(f"{name}={','.join(fields) or '-'}" for name, fields in comparison.items()))


def _parse_maturity(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--maturity debe tener formato YYYY-MM-DD") from exc
    return parsed.strftime("%Y%m%d")


def _display_deep_attempt(observation: DeepSnapshotAttempt) -> str:
    entries = []
    for field in IbkrMarketDataProvider.DEEP_OPTION_SNAPSHOT_FIELDS.split(","):
        if field not in observation.fields:
            value = "field no recibido"
        elif _is_explicitly_unavailable(observation.fields[field]):
            value = "field recibido con valor N/A"
        elif field == "6509":
            value = _market_data_availability(observation.fields[field]).display
        else:
            value = str(observation.fields[field])[:80]
        entries.append(f"{field}={value}")
    label = "pre-flight" if observation.phase == "pre-flight" else f"intento {observation.attempt}"
    return f"{label}: " + " | ".join(entries)


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


def _display(value: object | None, status: MarketDataFieldStatus | None = None) -> str:
    if value is not None:
        return f"{value:g}" if isinstance(value, float) else str(value)
    labels = {
        MarketDataFieldStatus.NOT_READY: "pendiente tras pre-flight",
        MarketDataFieldStatus.UNAVAILABLE: "campo no disponible",
        MarketDataFieldStatus.PARTIAL_RESPONSE: "respuesta parcial",
    }
    return f"N/D ({labels[status]})" if status in labels else "N/D"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.deep and args.strike is None:
        print("ERROR: --strike es obligatorio con --deep", file=sys.stderr)
        return 2
    if args.websocket and not args.deep:
        print("ERROR: --websocket requiere --deep", file=sys.stderr)
        return 2
    if not 0 < args.stream_seconds <= 60:
        print("ERROR: --stream-seconds debe estar entre 0 y 60", file=sys.stderr)
        return 2
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    provider = IbkrMarketDataProvider(ClientPortalTransport(args.base_url, allow_insecure_tls=args.insecure_tls))
    try:
        if args.deep:
            websocket_factory = None
            if args.websocket:
                websocket_factory = lambda: ClientPortalWebSocket(
                    args.base_url, allow_insecure_tls=args.insecure_tls
                )
            run_deep(
                provider, args.symbol, args.expiration, args.strike,
                maturity=args.maturity, websocket_factory=websocket_factory,
                stream_seconds=args.stream_seconds,
            )
        else:
            run(provider, args.symbol, args.expiration, args.contracts)
    except (IbkrError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
