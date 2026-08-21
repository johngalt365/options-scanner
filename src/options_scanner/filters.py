"""Reglas puras de selección, sin dependencias de proveedores."""

from collections.abc import Iterable
from datetime import date

from options_scanner.models import MarketData, OptionType, Underlying, short_put_theta


def safety_margin(underlying_price: float, strike: float) -> float:
    if underlying_price <= 0:
        raise ValueError("underlying_price debe ser positivo")
    return (underlying_price - strike) / underlying_price


def filter_put_candidates(
    underlying: Underlying,
    quotes: Iterable[MarketData],
    as_of: date,
    *,
    min_dte: int = 30,
    max_dte: int = 45,
    min_safety_margin: float = 0.20,
    min_abs_delta: float = 0.15,
    max_abs_delta: float = 0.30,
    min_iv: float | None = None,
    min_short_theta: float | None = None,
) -> list[MarketData]:
    """Selecciona cotizaciones PUT que cumplen todas las reglas."""

    if min_dte > max_dte:
        raise ValueError("min_dte no puede ser mayor que max_dte")
    if min_abs_delta > max_abs_delta:
        raise ValueError("min_abs_delta no puede ser mayor que max_abs_delta")
    candidates = []
    for quote in quotes:
        contract = quote.contract
        short_theta = short_put_theta(quote.theta)
        if (
            contract.underlying_symbol == underlying.symbol
            and contract.option_type is OptionType.PUT
            and min_dte <= contract.days_to_expiration(as_of) <= max_dte
            and safety_margin(underlying.current_price, contract.strike) >= min_safety_margin
            and min_abs_delta <= abs(quote.delta) <= max_abs_delta
            and (min_iv is None or (quote.implied_volatility is not None and quote.implied_volatility >= min_iv))
            and (min_short_theta is None or (short_theta is not None and short_theta >= min_short_theta))
        ):
            candidates.append(quote)
    return candidates
