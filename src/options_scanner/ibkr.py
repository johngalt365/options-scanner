"""Adaptador de solo lectura preparado para IBKR Web API."""
from datetime import date
from typing import Any, Mapping, Protocol
from options_scanner.models import MarketData, OptionContract, OptionType, Underlying

class IbkrTransport(Protocol):
    """Frontera HTTP mínima; autenticación y red quedan fuera del adaptador."""
    def get(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]: ...

class IbkrMarketDataProvider:
    def __init__(self, transport: IbkrTransport) -> None:
        self._transport = transport

    def get_underlying(self, symbol: str) -> Underlying:
        data = self._transport.get("/iserver/marketdata/snapshot", {"symbol": symbol})
        return Underlying(symbol.upper(), float(data["last"]))

    def get_option_market_data(self, symbol: str) -> tuple[MarketData, ...]:
        data = self._transport.get("/iserver/secdef/strikes", {"symbol": symbol})
        return tuple(self._map_option(symbol.upper(), row) for row in data["options"])

    @staticmethod
    def _map_option(symbol: str, row: Mapping[str, Any]) -> MarketData:
        right = str(row["right"]).upper()
        option_type = {
            "P": OptionType.PUT,
            "C": OptionType.CALL,
            "PUT": OptionType.PUT,
            "CALL": OptionType.CALL,
        }[right]
        contract = OptionContract(str(row["conid"]), symbol, option_type, float(row["strike"]), date.fromisoformat(str(row["expiration"])))
        return MarketData(contract, float(row["bid"]), float(row["ask"]), float(row["delta"]), float(row["gamma"]), float(row["theta"]), float(row["vega"]), float(row["iv"]), int(row["volume"]), int(row["open_interest"]))
