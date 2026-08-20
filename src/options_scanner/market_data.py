"""Puerto de acceso a market data y proveedor ficticio reproducible."""

from datetime import date
from typing import Protocol, runtime_checkable

from options_scanner.models import MarketData, OptionContract, OptionType, Underlying


@runtime_checkable
class MarketDataProvider(Protocol):
    """Puerto de solo lectura compartido por todos los proveedores."""

    def get_underlying(self, symbol: str) -> Underlying: ...

    def get_option_market_data(self, symbol: str) -> tuple[MarketData, ...]: ...


class FakeMarketDataProvider:
    """Snapshot determinista; no está vinculado a usuarios ni cuentas."""

    def get_underlying(self, symbol: str) -> Underlying:
        if symbol.upper() != "NVDA":
            raise KeyError(symbol)
        return Underlying("NVDA", 100.0)

    def get_option_market_data(self, symbol: str) -> tuple[MarketData, ...]:
        self.get_underlying(symbol)
        rows = (
            ("NVDA-20260924-P75", 75.0, -0.20, 1.10, 1.20, 0.012, -0.04, 0.08, 0.32, 420, 2100),
            ("NVDA-20260924-P80", 80.0, -0.30, 1.75, 1.90, 0.016, -0.05, 0.09, 0.35, 310, 1750),
            ("NVDA-20260924-P85", 85.0, -0.38, 2.60, 2.75, 0.020, -0.06, 0.10, 0.38, 250, 980),
        )
        return tuple(
            MarketData(
                OptionContract(identifier, "NVDA", OptionType.PUT, strike, date(2026, 9, 24)),
                bid, ask, delta, gamma, theta, vega, iv, volume, oi,
            )
            for identifier, strike, delta, bid, ask, gamma, theta, vega, iv, volume, oi in rows
        )
