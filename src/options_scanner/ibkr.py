"""Adaptador de solo lectura para Interactive Brokers Client Portal Web API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import json
import logging
import re
import ssl
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from options_scanner.models import MarketData, OptionContract, OptionType, Underlying


logger = logging.getLogger(__name__)


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


class MarketDataFieldStatus(StrEnum):
    """Motivo por el que una celda de un snapshot no contiene un valor."""

    AVAILABLE = "disponible"
    NOT_READY = "todavia_no_disponible"
    UNAVAILABLE = "no_disponible"
    PARTIAL_RESPONSE = "respuesta_parcial"


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
    field_statuses: Mapping[str, MarketDataFieldStatus]


class IbkrMarketDataProvider:
    """Proveedor del scanner y operaciones de descubrimiento para el diagnóstico."""

    # Client Portal market-data field IDs. 7698 is *Option Open Interest*;
    # 7087/7088 are aggregate put/call OI for an underlying, not a contract.
    UNDERLYING_SNAPSHOT_FIELDS = "31,84,86"
    OPTION_FIELD_IDS = {
        "bid": "84",
        "ask": "86",
        "delta": "7308",
        "theta": "7310",
        "implied_volatility": "7633",
        "open_interest": "7698",
    }
    OPTION_SNAPSHOT_FIELDS = ",".join(OPTION_FIELD_IDS.values())
    # Kept as a compatibility alias for callers/tests written before fields
    # were split by instrument type.
    SNAPSHOT_FIELDS = UNDERLYING_SNAPSHOT_FIELDS

    def __init__(
        self,
        transport: IbkrTransport,
        *,
        snapshot_attempts: int = 3,
        snapshot_retry_delay: float = 0.1,
    ) -> None:
        self._transport = transport
        self._snapshot_attempts = max(1, snapshot_attempts)
        self._snapshot_retry_delay = max(0.0, snapshot_retry_delay)

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
        rows = self._snapshot(
            (conid,),
            fields=self.UNDERLYING_SNAPSHOT_FIELDS,
            ready=lambda row: _number(row, "31") is not None
            or (_number(row, "84") is not None and _number(row, "86") is not None),
        )
        if not rows:
            raise IncompleteDataError("IBKR no devolvió el snapshot del subyacente; la respuesta fue parcial o incompleta")
        row = rows[0]
        price = _number(row, "31")
        if price is None:
            bid = _number(row, "84")
            ask = _number(row, "86")
            if bid is not None and ask is not None:
                price = (bid + ask) / 2
                logger.warning(
                    "IBKR no devolvió last (field 31) para %s; se usa el mid de bid (84) y ask (86)",
                    symbol.upper(),
                )
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
        conids = tuple(conid for conid, _ in contracts)
        rows = self._snapshot(
            conids,
            fields=self.OPTION_SNAPSHOT_FIELDS,
            ready=lambda row: all(_number(row, field_id) is not None for field_id in self.OPTION_FIELD_IDS.values()),
        )
        by_conid = {str(row.get("conid")): row for row in rows}
        quotes = []
        for conid, strike in contracts:
            row = by_conid.get(conid, {})
            self._raise_if_unauthorized(row)
            statuses = self._option_field_statuses(row)
            missing = [name for name, status in statuses.items() if status is not MarketDataFieldStatus.AVAILABLE]
            if missing:
                logger.warning(
                    "Snapshot de opción conid=%s incompleto tras pre-flight/reintentos: %s; campos recibidos=%s",
                    conid,
                    ", ".join(f"{name}={statuses[name].value}" for name in missing),
                    _safe_snapshot_summary(row),
                )
            quotes.append(IbkrOptionQuote(
                conid, strike, expiration, _number(row, "84", "bid"), _number(row, "86", "ask"),
                _number(row, "7308", "delta"), _number(row, "7310", "theta"),
                _number(row, "7633", "iv"), _integer(row, "7698", "open_interest"),
                statuses,
            ))
        return tuple(quotes)

    def _snapshot(
        self,
        conids: Sequence[str],
        *,
        fields: str,
        ready: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        params = {"conids": ",".join(conids), "fields": fields}

        # Client Portal usa la primera llamada como pre-flight para iniciar las
        # suscripciones. Puede responder únicamente con conid/conidEx.
        preflight_rows = self._snapshot_request(params)
        logger.debug("Pre-flight snapshot conids=%s: %s", ",".join(conids), [_safe_snapshot_summary(row) for row in preflight_rows])
        for row in preflight_rows:
            self._raise_if_unauthorized(row)

        merged: dict[str, dict[str, Any]] = {}
        for attempt in range(self._snapshot_attempts):
            # También se espera después del pre-flight: IBKR documenta que la
            # primera respuesta inicia la suscripción y los datos son asíncronos.
            if self._snapshot_retry_delay:
                time.sleep(self._snapshot_retry_delay)
            rows = self._snapshot_request(params)
            logger.debug("Snapshot intento %d/%d conids=%s: %s", attempt + 1, self._snapshot_attempts, ",".join(conids), [_safe_snapshot_summary(row) for row in rows])
            for index, row in enumerate(rows):
                self._raise_if_unauthorized(row)
                identifier = row.get("conid", row.get("conidEx", conids[index] if index < len(conids) else index))
                key = str(identifier).split("@", 1)[0]
                target = merged.setdefault(key, {"conid": key})
                target.update(row)
            if self._snapshot_is_ready(merged, conids, ready, fields):
                break

        return tuple(merged.get(str(conid), {}) for conid in conids)

    def _snapshot_request(self, params: Mapping[str, str]) -> tuple[Mapping[str, Any], ...]:
        data = self._transport.get("/iserver/marketdata/snapshot", params)
        rows = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else (data,)
        return tuple(row for row in rows if isinstance(row, Mapping))

    def _snapshot_is_ready(
        self,
        rows: Mapping[str, Mapping[str, Any]],
        conids: Sequence[str],
        ready: Callable[[Mapping[str, Any]], bool] | None,
        fields: str,
    ) -> bool:
        requested_fields = fields.split(",")
        return all(
            str(conid) in rows
            and (
                ready(rows[str(conid)])
                if ready is not None
                else all(_number(rows[str(conid)], field) is not None for field in requested_fields)
            )
            for conid in conids
        )

    @staticmethod
    def _raise_if_unauthorized(row: Mapping[str, Any]) -> None:
        message = str(row.get("error", row.get("message", "")))
        if _has_permission_message(message):
            raise MarketDataUnauthorizedError("la sesión no tiene autorización/suscripción para estos datos de mercado")

    def _raise_market_data_or_incomplete(self, row: Mapping[str, Any], item: str) -> None:
        self._raise_if_unauthorized(row)
        raise IncompleteDataError(f"IBKR no devolvió {item}; la respuesta fue parcial o incompleta")

    def _option_field_statuses(self, row: Mapping[str, Any]) -> Mapping[str, MarketDataFieldStatus]:
        has_any_value = any(_number(row, field_id) is not None for field_id in self.OPTION_FIELD_IDS.values())
        statuses: dict[str, MarketDataFieldStatus] = {}
        for name, field_id in self.OPTION_FIELD_IDS.items():
            if _number(row, field_id, name if name != "implied_volatility" else "iv") is not None:
                statuses[name] = MarketDataFieldStatus.AVAILABLE
            elif field_id in row and _is_explicitly_unavailable(row[field_id]):
                statuses[name] = MarketDataFieldStatus.UNAVAILABLE
            elif has_any_value:
                statuses[name] = MarketDataFieldStatus.PARTIAL_RESPONSE
            else:
                statuses[name] = MarketDataFieldStatus.NOT_READY
        return statuses

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
        if isinstance(value, bool) or _is_explicitly_unavailable(value):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        # Snapshot values are normally strings and may carry an IBKR tick
        # attribute prefix (for example "C1.25"), thousands separators,
        # percent signs, or K/M suffixes.
        text = str(value).strip().replace("−", "-").replace(",", "")
        match = re.fullmatch(r"[A-Za-z]*\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s*([KkMm]))?%?", text)
        if match:
            multiplier = {None: 1.0, "k": 1_000.0, "m": 1_000_000.0}[match.group(2).lower() if match.group(2) else None]
            return float(match.group(1)) * multiplier
    return None


def _integer(row: Mapping[str, Any], *keys: str) -> int | None:
    value = _number(row, *keys)
    return int(value) if value is not None else None


def _is_explicitly_unavailable(value: Any) -> bool:
    return value is None or str(value).strip().upper() in {"", "-", "--", "N/A", "N/D", "NA", "NULL"}


def _safe_snapshot_summary(row: Mapping[str, Any]) -> str:
    """Expose solo identificadores/campos solicitados, nunca la respuesta HTTP."""

    allowed = {"conid", "conidEx", "error", "message", "84", "86", "7308", "7310", "7633", "7698", "31"}
    parts = []
    for key in sorted((str(key) for key in row if str(key) in allowed)):
        if key in {"error", "message"}:
            value = "permiso_denegado" if _has_permission_message(str(row.get(key, ""))) else "mensaje_ibkr"
        else:
            value = repr(row.get(key))[:80]
        parts.append(f"{key}={value}")
    unknown = sorted(str(key) for key in row if str(key) not in allowed)
    if unknown:
        parts.append(f"otras_claves={unknown}")
    return "{" + ", ".join(parts) + "}"


def _has_permission_message(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in ("not subscribed", "permission", "market data subscription", "unauthorized"))
