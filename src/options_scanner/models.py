"""Modelos de dominio inmutables e independientes del proveedor."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class OptionType(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


def short_put_theta(contract_theta: float | None) -> float | None:
    """Invert contractual theta once to obtain a short position exposure.

    No absolute-value normalization is intentional: the wire sign remains
    observable and a positive contractual value yields a negative exposure.
    """
    return None if contract_theta is None else -contract_theta


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} no puede estar vacío")


@dataclass(frozen=True, slots=True)
class User:
    id: str
    display_name: str

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.display_name, "display_name")


@dataclass(frozen=True, slots=True)
class Watchlist:
    id: str
    user_id: str
    name: str
    symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.user_id, "user_id")
        _require_text(self.name, "name")
        if any(not symbol.strip() for symbol in self.symbols):
            raise ValueError("symbols no puede contener símbolos vacíos")


@dataclass(frozen=True, slots=True)
class StrategyParameters:
    id: str
    user_id: str
    name: str
    min_dte: int = 30
    max_dte: int = 45
    min_safety_margin: float = 0.20
    min_abs_delta: float = 0.15
    max_abs_delta: float = 0.30

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.user_id, "user_id")
        _require_text(self.name, "name")
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte no puede ser mayor que max_dte")
        if self.min_abs_delta > self.max_abs_delta:
            raise ValueError("min_abs_delta no puede ser mayor que max_abs_delta")


@dataclass(frozen=True, slots=True)
class Underlying:
    symbol: str
    current_price: float

    def __post_init__(self) -> None:
        _require_text(self.symbol, "symbol")
        if self.current_price <= 0:
            raise ValueError("current_price debe ser positivo")


@dataclass(frozen=True, slots=True)
class OptionContract:
    """Identidad y términos del contrato; no contiene datos de mercado."""

    id: str
    underlying_symbol: str
    option_type: OptionType
    strike: float
    expiration: date

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.underlying_symbol, "underlying_symbol")
        if self.strike <= 0:
            raise ValueError("strike debe ser positivo")

    def days_to_expiration(self, as_of: date) -> int:
        return (self.expiration - as_of).days


@dataclass(frozen=True, slots=True)
class MarketData:
    """Cotización completa de un contrato en un instante lógico.

    ``implied_volatility`` uses a decimal fraction internally (``0.482`` is
    48.2%). Providers must normalize their wire format at the integration edge.
    """

    contract: OptionContract
    bid: float
    ask: float
    delta: float
    gamma: float
    theta: float
    vega: float
    implied_volatility: float | None
    volume: int
    open_interest: int
    # Raw IBKR 6509 value when supplied.  It is intentionally preserved so
    # policy layers, rather than the provider, decide whether Frozen data is fit.
    market_data_availability: str | None = None

    def __post_init__(self) -> None:
        if self.bid < 0 or self.ask < 0 or self.ask < self.bid:
            raise ValueError("bid/ask no forman un mercado válido")
        if not -1 <= self.delta <= 1:
            raise ValueError("delta debe estar entre -1 y 1")
        if (self.implied_volatility is not None and self.implied_volatility < 0) or self.volume < 0 or self.open_interest < 0:
            raise ValueError("IV, volumen y open interest no pueden ser negativos")


@dataclass(frozen=True, slots=True)
class SavedScanResult:
    id: str
    user_id: str
    strategy_parameters_id: str
    contracts: tuple[OptionContract, ...]

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.user_id, "user_id")
        _require_text(self.strategy_parameters_id, "strategy_parameters_id")
