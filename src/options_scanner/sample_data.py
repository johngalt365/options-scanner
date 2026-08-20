"""Alias compatibles para el snapshot ficticio del ejemplo."""
from datetime import date
from options_scanner.market_data import FakeMarketDataProvider

EXAMPLE_DATE = date(2026, 8, 20)
_PROVIDER = FakeMarketDataProvider()
NVDA = _PROVIDER.get_underlying("NVDA")
NVDA_OPTIONS = _PROVIDER.get_option_market_data("NVDA")
