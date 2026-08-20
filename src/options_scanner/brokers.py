"""Contratos para futuras conexiones de broker propiedad de un usuario."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class BrokerConnectionProfile:
    """Metadatos no sensibles de una conexión individual de broker."""

    id: str
    user_id: str
    broker: str
    account_reference: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("id", "user_id", "broker"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} no puede estar vacío")


@runtime_checkable
class BrokerConnection(Protocol):
    """Puerto para una sesión de broker no compartida entre usuarios."""

    @property
    def profile(self) -> BrokerConnectionProfile: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...
