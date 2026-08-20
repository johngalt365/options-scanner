"""Adaptador de solo lectura para Interactive Brokers Client Portal Web API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import ssl
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from options_scanner.models import MarketData, OptionContract, OptionType, Underlying


class IbkrError(RuntimeError):
    """Error esperado y presentable durante el diagnóstico."""


class GatewayUnavailableError(IbkrError):
    pass


class NotAuthenticatedError(IbkrError):
    pass


class MarketDataUnauthorizedError(IbkrError):
    pass


class TickerNotFoundError(IbkrError):
    pass


class IncompleteDataError(IbkrError):
    pass


class IbkrTransport(Protocol):
    """Frontera HTTP mínima, fácil de reemplazar por un fake en tests."""

    def get(self, path: str, params: Mapping[str, str]) -> Any: ...


class ClientPortalTransport:
    """Transporte JSON sin credenciales; usa la sesión externa del Gateway."""

    def __init__(
        self,
        base_url: str = "https://localhost:5000/v1/api",
        *,
        allow_insecure_tls: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._ssl_context = ssl._create_unverified_context() if allow_insecure_tls else ssl.create_default_context()

    def get(self, path: str, params: Mapping[str, str]) -> Any:
        query = urlencode(params)
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        try:
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=self.timeout, context=self._ssl_context) as response:
                return json.load(response)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403):
                raise NotAuthenticatedError("Client Portal Gateway no tiene una sesión autenticada") from exc
            raise GatewayUnavailableError(f"Gateway respondió HTTP {exc.code}: {body[:200]}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GatewayUnavailableError(f"no se pudo conectar con Client Portal Gateway: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GatewayUnavailableError("Gateway devolvió una respuesta que no es JSON válido") from exc


@dataclass(frozen=True, slots=True)
class IbkrOptionQuote:
    conid: str
    strike: float
    expiration: date
    bid: float | None
    ask: float | None
    delta: float | None
    theta: float | None
    implied_volatility: float | None
    open_interest: int | None


class IbkrMarketDataProvider:
    """Proveedor del scanner y operaciones de descubrimiento para el diagnóstico."""

    SNAPSHOT_FIELDS = "31,84,86,7308,7310,7633,7698"

    def __init__(self, transport: IbkrTransport) -> None:
        self._transport = transport

    def require_authenticated_session(self) -> None:
        data = self._transport.get("/iserver/auth/status", {})
        if not isinstance(data, Mapping) or not data.get("authenticated"):
            raise NotAuthenticatedError("Client Portal Gateway está disponible, pero la sesión no está autenticada")

    def locate_stock(self, symbol: str) -> tuple[str, tuple[date, ...]]:
        data = self._transport.get("/iserver/secdef/search", {"symbol": symbol.upper(), "secType": "STK"})
        rows = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else []
        row = next((item for item in rows if isinstance(item, Mapping) and str(item.get("symbol", "")).upper() == symbol.upper()), None)
        if row is None or row.get("conid") is None:
            raise TickerNotFoundError(f"ticker no encontrado: {symbol.upper()}")
        months = ""
        for section in row.get("sections", ()):
            if isinstance(section, Mapping) and section.get("secType") == "OPT":
                months = str(section.get("months", ""))
                break
        expirations = tuple(filter(None, (_parse_ibkr_month(value) for value in months.split(";") if value)))
        if not expirations:
            raise IncompleteDataError(f"IBKR no devolvió vencimientos de opciones para {symbol.upper()}")
        return str(row["conid"]), expirations

    def get_underlying_by_conid(self, symbol: str, conid: str) -> Underlying:
        rows = self._snapshot((conid,))
        if not rows:
            raise IncompleteDataError("IBKR no devolvió el snapshot del subyacente; la respuesta fue parcial o incompleta")
        row = rows[0]
        price = _number(row, "31", "last", "7295")
        if price is None:
            self._raise_market_data_or_incomplete(row, "precio del subyacente")
        return Underlying(symbol.upper(), price)

    def get_put_strikes(self, conid: str, expiration: date) -> tuple[float, ...]:
        data = self._transport.get("/iserver/secdef/strikes", {
            "conid": conid, "secType": "OPT", "month": _format_ibkr_month(expiration), "exchange": "SMART",
        })
        values = data.get("put", ()) if isinstance(data, Mapping) else ()
        strikes = tuple(float(value) for value in values)
        if not strikes:
            raise IncompleteDataError("IBKR no devolvió strikes PUT para el vencimiento seleccionado")
        return strikes

    def get_put_contracts(self, conid: str, expiration: date, strikes: Sequence[float]) -> tuple[tuple[str, float], ...]:
        contracts: list[tuple[str, float]] = []
        for strike in strikes:
            data = self._transport.get("/iserver/secdef/info", {
                "conid": conid, "secType": "OPT", "month": _format_ibkr_month(expiration),
                "exchange": "SMART", "strike": str(strike), "right": "P",
            })
            rows = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else ()
            row = next((item for item in rows if isinstance(item, Mapping) and str(item.get("right", "P")).upper() in ("P", "PUT")), None)
            if row is not None and row.get("conid") is not None:
                contracts.append((str(row["conid"]), strike))
        if not contracts:
            raise IncompleteDataError("IBKR no devolvió contratos PUT para los strikes seleccionados")
        return tuple(contracts)

    def get_put_quotes(self, contracts: Sequence[tuple[str, float]], expiration: date) -> tuple[IbkrOptionQuote, ...]:
        rows = self._snapshot(tuple(conid for conid, _ in contracts))
        by_conid = {str(row.get("conid")): row for row in rows}
        quotes = []
        for conid, strike in contracts:
            row = by_conid.get(conid, {})
            self._raise_if_unauthorized(row)
            quotes.append(IbkrOptionQuote(
                conid, strike, expiration, _number(row, "84", "bid"), _number(row, "86", "ask"),
                _number(row, "7308", "delta"), _number(row, "7310", "theta"),
                _number(row, "7633", "iv"), _integer(row, "7698", "open_interest"),
            ))
        return tuple(quotes)

    def _snapshot(self, conids: Sequence[str]) -> tuple[Mapping[str, Any], ...]:
        data = self._transport.get("/iserver/marketdata/snapshot", {"conids": ",".join(conids), "fields": self.SNAPSHOT_FIELDS})
        rows = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else (data,)
        return tuple(row for row in rows if isinstance(row, Mapping))

    @staticmethod
    def _raise_if_unauthorized(row: Mapping[str, Any]) -> None:
        message = str(row.get("error", row.get("message", ""))).lower()
        if any(word in message for word in ("not subscribed", "permission", "market data subscription", "unauthorized")):
            raise MarketDataUnauthorizedError("la sesión no tiene autorización/suscripción para estos datos de mercado")

    def _raise_market_data_or_incomplete(self, row: Mapping[str, Any], item: str) -> None:
        self._raise_if_unauthorized(row)
        raise IncompleteDataError(f"IBKR no devolvió {item}; la respuesta fue parcial o incompleta")

    # Compatibilidad con el puerto MarketDataProvider y con transportes normalizados.
    def get_underlying(self, symbol: str) -> Underlying:
        data = self._transport.get("/iserver/marketdata/snapshot", {"symbol": symbol})
        if isinstance(data, Mapping) and "last" in data:
            return Underlying(symbol.upper(), float(data["last"]))
        conid, _ = self.locate_stock(symbol)
        return self.get_underlying_by_conid(symbol, conid)

    def get_option_market_data(self, symbol: str) -> tuple[MarketData, ...]:
        data = self._transport.get("/iserver/secdef/strikes", {"symbol": symbol})
        if not isinstance(data, Mapping) or "options" not in data:
            raise IncompleteDataError("el endpoint no devolvió opciones normalizadas para el scanner")
        return tuple(self._map_option(symbol.upper(), row) for row in data["options"])

    @staticmethod
    def _map_option(symbol: str, row: Mapping[str, Any]) -> MarketData:
        option_type = {"P": OptionType.PUT, "C": OptionType.CALL, "PUT": OptionType.PUT, "CALL": OptionType.CALL}[str(row["right"]).upper()]
        contract = OptionContract(str(row["conid"]), symbol, option_type, float(row["strike"]), date.fromisoformat(str(row["expiration"])))
        return MarketData(contract, float(row["bid"]), float(row["ask"]), float(row["delta"]), float(row["gamma"]), float(row["theta"]), float(row["vega"]), float(row["iv"]), int(row["volume"]), int(row["open_interest"]))


_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _parse_ibkr_month(value: str) -> date | None:
    value = value.strip().upper()
    if len(value) != 5 or value[:3] not in _MONTHS or not value[3:].isdigit():
        return None
    return date(2000 + int(value[3:]), _MONTHS.index(value[:3]) + 1, 1)


def _format_ibkr_month(value: date) -> str:
    return f"{_MONTHS[value.month - 1]}{value.year % 100:02d}"


def _number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        try:
            if value not in (None, "", "N/A", "-"):
                return float(str(value).replace("%", ""))
        except ValueError:
            pass
    return None


def _integer(row: Mapping[str, Any], *keys: str) -> int | None:
    value = _number(row, *keys)
    return int(value) if value is not None else None
