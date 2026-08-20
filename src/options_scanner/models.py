"""Modelos de dominio independientes de cualquier proveedor de mercado."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class OptionType(StrEnum):
    """Tipos de opción soportados por el modelo."""

    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True, slots=True)
class Underlying:
    """Instantánea simplificada del precio de un subyacente."""

    symbol: str
    current_price: float

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol no puede estar vacío")
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
        if not self.symbol.strip():
            raise ValueError("symbol no puede estar vacío")
        if self.strike <= 0:
            raise ValueError("strike debe ser positivo")
        if not -1 <= self.delta <= 1:
            raise ValueError("delta debe estar entre -1 y 1")

    def days_to_expiration(self, as_of: date) -> int:
        """Devuelve los días naturales restantes hasta el vencimiento."""

        return (self.expiration - as_of).days
