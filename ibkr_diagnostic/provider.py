"""Minimal Client Portal Web API market-data provider."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class IbkrError(RuntimeError):
    """Base class for diagnostic-friendly IBKR errors."""


class GatewayUnavailable(IbkrError):
    pass


class SessionNotAuthenticated(IbkrError):
    pass


class DataNotAuthorized(IbkrError):
    pass


class TickerNotFound(IbkrError):
    pass


class IncompleteMarketData(IbkrError):
    pass


class Transport(Protocol):
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


@dataclass
class UrlTransport:
    base_url: str
    verify_tls: bool = True
    timeout: float = 10

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode(params, doseq=True)
        context = None if self.verify_tls else ssl._create_unverified_context()
        try:
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=self.timeout, context=context) as response:
                return json.load(response)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise DataNotAuthorized(f"IBKR rechazó la petición ({exc.code}): {body}") from exc
            raise GatewayUnavailable(f"El gateway respondió HTTP {exc.code}: {body}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GatewayUnavailable(f"No se pudo conectar con Client Portal Gateway: {exc}") from exc


class IbkrMarketDataProvider:
    """Thin wrapper around only the read-only endpoints needed by the diagnostic."""

    SNAPSHOT_FIELDS = "31,84,86,7308,7310,7633,7089"

    def __init__(self, base_url: str = "https://localhost:5000/v1/api", *, transport: Transport | None = None,
                 verify_tls: bool = True) -> None:
        self.transport = transport or UrlTransport(base_url, verify_tls=verify_tls)

    def require_authenticated_session(self) -> None:
        status = self.transport.get("/iserver/auth/status")
        if not status.get("authenticated") or not status.get("connected", True):
            raise SessionNotAuthenticated(
                "La sesión IBKR no está autenticada. Inicia sesión manualmente en Client Portal Gateway."
            )

    def find_stock(self, symbol: str) -> dict[str, Any]:
        results = self.transport.get("/iserver/secdef/search", {"symbol": symbol, "secType": "STK"})
        match = next((item for item in results if item.get("symbol", "").upper() == symbol.upper()), None)
        if not match:
            raise TickerNotFound(f"No se encontró el ticker {symbol}.")
        return match

    def snapshot(self, conids: list[int]) -> list[dict[str, Any]]:
        rows = self.transport.get("/iserver/marketdata/snapshot", {
            "conids": ",".join(map(str, conids)), "fields": self.SNAPSHOT_FIELDS,
        })
        for row in rows:
            error = str(row.get("error", ""))
            if "permission" in error.lower() or "subscription" in error.lower():
                raise DataNotAuthorized(f"Sin autorización de market data: {error}")
        return rows

    def stock_price(self, conid: int) -> float:
        rows = self.snapshot([conid])
        if not rows or self._number(rows[0].get("31")) is None:
            raise IncompleteMarketData("IBKR no devolvió un precio para el subyacente.")
        return self._number(rows[0]["31"])  # type: ignore[return-value]

    def option_expirations(self, stock: dict[str, Any]) -> list[str]:
        months: list[str] = []
        for section in stock.get("sections", []):
            if section.get("secType") == "OPT":
                months.extend(filter(None, str(section.get("months", "")).split(";")))
        if not months:
            raise IncompleteMarketData("IBKR no devolvió expiraciones de opciones.")
        return months

    def put_strikes(self, conid: int, month: str) -> list[float]:
        data = self.transport.get("/iserver/secdef/strikes", {
            "conid": conid, "secType": "OPT", "month": month, "exchange": "SMART",
        })
        strikes = [float(value) for value in data.get("put", [])]
        if not strikes:
            raise IncompleteMarketData(f"IBKR no devolvió strikes PUT para {month}.")
        return strikes

    def put_contracts(self, conid: int, month: str, strikes: list[float]) -> list[dict[str, Any]]:
        contracts = []
        for strike in strikes:
            rows = self.transport.get("/iserver/secdef/info", {
                "conid": conid, "secType": "OPT", "month": month, "strike": strike,
                "right": "P", "exchange": "SMART",
            })
            contracts.extend(rows or [])
        return contracts

    def option_market_data(self, contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not contracts:
            raise IncompleteMarketData("No se encontraron contratos PUT.")
        rows = self.snapshot([int(contract["conid"]) for contract in contracts])
        by_conid = {str(row.get("conid")): row for row in rows}
        output = []
        for contract in contracts:
            raw = by_conid.get(str(contract["conid"]), {})
            output.append({
                "conid": contract["conid"], "strike": contract.get("strike"),
                "bid": self._number(raw.get("84")), "ask": self._number(raw.get("86")),
                "delta": self._number(raw.get("7308")), "theta": self._number(raw.get("7310")),
                "iv": self._number(raw.get("7633")), "open_interest": self._number(raw.get("7089")),
            })
        if all(item["bid"] is None and item["ask"] is None for item in output):
            raise IncompleteMarketData("Market data incompleta: ningún contrato tiene bid o ask.")
        return output

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None
