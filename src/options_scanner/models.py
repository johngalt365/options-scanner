"""Modelos de dominio independientes de cualquier proveedor de mercado."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class OptionType(StrEnum):
    """Tipos de opción soportados por el modelo."""

    CALL = "CALL"
    PUT = "PUT"


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} no puede estar vacío")


@dataclass(frozen=True, slots=True)
class User:
    """Identidad mínima de un propietario, sin datos de autenticación."""

    id: str
    display_name: str

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.display_name, "display_name")


@dataclass(frozen=True, slots=True)
class Watchlist:
    """Lista de símbolos que pertenece exclusivamente a un usuario."""

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
    """Configuración guardada de una estrategia para un usuario."""

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
class SavedScanResult:
    """Resultado inmutable guardado en el espacio de un usuario."""

    id: str
    user_id: str
    strategy_parameters_id: str
    contracts: tuple["OptionContract", ...]

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.user_id, "user_id")
        _require_text(self.strategy_parameters_id, "strategy_parameters_id")


@dataclass(frozen=True, slots=True)
class Underlying:
    """Instantánea simplificada del precio de un subyacente."""

    symbol: str
    current_price: float

    def __post_init__(self) -> None:
        _require_text(self.symbol, "symbol")
        if self.current_price <= 0:
            raise ValueError("current_price debe ser positivo")


@dataclass(frozen=True, slots=True)
class OptionContract:
    """Contrato con los campos mínimos necesarios para el primer filtro."""

    symbol: str
    option_type: OptionType
    strike: float
    expiration: date
    delta: float

    def __post_init__(self) -> None:
        _require_text(self.symbol, "symbol")
        if self.strike <= 0:
            raise ValueError("strike debe ser positivo")
        if not -1 <= self.delta <= 1:
            raise ValueError("delta debe estar entre -1 y 1")

    def days_to_expiration(self, as_of: date) -> int:
        """Devuelve los días naturales restantes hasta el vencimiento."""

        return (self.expiration - as_of).days
