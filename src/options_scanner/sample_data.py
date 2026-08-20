"""Datos ficticios y deterministas para el ejemplo del MVP."""

from datetime import date, timedelta

from options_scanner.models import OptionContract, OptionType, Underlying

EXAMPLE_DATE = date(2026, 8, 20)
NVDA = Underlying(symbol="NVDA", current_price=180.0)

NVDA_OPTIONS = (
    OptionContract("NVDA", OptionType.PUT, 140.0, EXAMPLE_DATE + timedelta(days=35), -0.22),
    OptionContract("NVDA", OptionType.PUT, 144.0, EXAMPLE_DATE + timedelta(days=45), -0.30),
    OptionContract("NVDA", OptionType.PUT, 145.0, EXAMPLE_DATE + timedelta(days=38), -0.24),
    OptionContract("NVDA", OptionType.PUT, 135.0, EXAMPLE_DATE + timedelta(days=25), -0.20),
    OptionContract("NVDA", OptionType.PUT, 140.0, EXAMPLE_DATE + timedelta(days=40), -0.10),
)
