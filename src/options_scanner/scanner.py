"""Servicio de aplicación que orquesta un proveedor mediante su puerto."""

from datetime import date

from options_scanner.filters import filter_put_candidates
from options_scanner.market_data import MarketDataProvider
from options_scanner.models import MarketData


def scan_puts(provider: MarketDataProvider, symbol: str, as_of: date, **filters: float | int) -> list[MarketData]:
    underlying = provider.get_underlying(symbol)
    quotes = provider.get_option_market_data(symbol)
    return filter_put_candidates(underlying, quotes, as_of, **filters)
