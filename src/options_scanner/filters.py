"""Reglas de selección del MVP."""

from collections.abc import Iterable
from datetime import date

from options_scanner.models import OptionContract, OptionType, Underlying


def safety_margin(underlying_price: float, strike: float) -> float:
    """Calcula cuánto está el strike por debajo del precio del subyacente."""

    if underlying_price <= 0:
        raise ValueError("underlying_price debe ser positivo")
    return (underlying_price - strike) / underlying_price


def filter_put_candidates(
    underlying: Underlying,
    contracts: Iterable[OptionContract],
    as_of: date,
    *,
    min_dte: int = 30,
    max_dte: int = 45,
    min_safety_margin: float = 0.20,
    min_abs_delta: float = 0.15,
    max_abs_delta: float = 0.30,
) -> list[OptionContract]:
    """Selecciona PUTs del subyacente que cumplen todas las reglas indicadas."""

    if min_dte > max_dte:
        raise ValueError("min_dte no puede ser mayor que max_dte")
    if min_abs_delta > max_abs_delta:
        raise ValueError("min_abs_delta no puede ser mayor que max_abs_delta")

    candidates = []
    for contract in contracts:
        dte = contract.days_to_expiration(as_of)
        matches = (
            contract.symbol == underlying.symbol
            and contract.option_type is OptionType.PUT
            and min_dte <= dte <= max_dte
            and safety_margin(underlying.current_price, contract.strike)
            >= min_safety_margin
            and min_abs_delta <= abs(contract.delta) <= max_abs_delta
        )
        if matches:
            candidates.append(contract)
    return candidates
