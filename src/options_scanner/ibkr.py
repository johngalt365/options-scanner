"""Adaptador de solo lectura para Interactive Brokers Client Portal Web API."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import json
import logging
import re
import ssl
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from options_scanner.models import MarketData, OptionContract, OptionType, Underlying
from options_scanner.historical import HistoricalBar, HistoricalPeriod, IbkrHistoricalDataProvider


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


class ContractMismatchError(IbkrError):
    """El contrato resuelto por IBKR no es el derivado solicitado."""


class MarketDataFieldStatus(StrEnum):
    """Motivo por el que una celda de un snapshot no contiene un valor."""

    AVAILABLE = "disponible"
    NOT_READY = "todavia_no_disponible"
    UNAVAILABLE = "no_disponible"
    PARTIAL_RESPONSE = "respuesta_parcial"


@dataclass(frozen=True, slots=True)
class MarketDataAvailability:
    """Interpretación conservadora del field 6509 de IBKR."""

    raw: str | None
    feed: str
    incomplete: bool
    book: bool

    @property
    def display(self) -> str:
        details = [self.feed]
        if self.incomplete:
            details.append("incomplete")
        details.append("book disponible" if self.book else "book no indicado")
        raw = self.raw if self.raw is not None else "ausente"
        return f"{raw} ({', '.join(details)})"


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
        work_limiter: threading.BoundedSemaphore | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._ssl_context = ssl._create_unverified_context() if allow_insecure_tls else ssl.create_default_context()
        self._work_limiter = work_limiter

    def get(self, path: str, params: Mapping[str, str]) -> Any:
        query = urlencode(params)
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        if self._work_limiter is not None:
            self._work_limiter.acquire()
        try:
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=self.timeout, context=self._ssl_context) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise NotAuthenticatedError("Client Portal Gateway no tiene una sesión autenticada") from exc
            raise GatewayUnavailableError(f"Gateway respondió HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GatewayUnavailableError("no se pudo conectar con Client Portal Gateway") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GatewayUnavailableError("Gateway devolvió una respuesta que no es JSON válido") from exc
        finally:
            if self._work_limiter is not None:
                self._work_limiter.release()


@dataclass(frozen=True, slots=True)
class IbkrOptionQuote:
    conid: str
    strike: float
    expiration: date
    bid: float | None
    ask: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    implied_volatility: float | None
    open_interest: int | None
    market_data_availability: MarketDataAvailability
    field_statuses: Mapping[str, MarketDataFieldStatus]


@dataclass(frozen=True, slots=True)
class DeepSnapshotAttempt:
    """Una entrega aislada del diagnóstico profundo (sin payload HTTP)."""

    phase: str
    attempt: int
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ConfirmedOptionContract:
    """Identidad segura de un contrato devuelto por ``secdef/info``."""

    conid: str
    symbol: str
    sec_type: str
    exchange: str
    listing_exchange: str
    right: str
    strike: float
    maturity_date: str
    multiplier: str
    trading_class: str
    valid_exchanges: str


@dataclass(frozen=True, slots=True)
class ContractResolutionAccounting:
    """Terminal accounting for the unique validation requests in one group."""

    target: int
    resolved: int
    failed: int
    unresolved_timeout: int
    deduplicated: int
    candidate_strikes: int = 0
    info_calls: int = 0
    cache_hits: int = 0
    validations_succeeded: int = 0
    validations_failed: int = 0
    info_latency_mean_ms: float = 0.0
    info_latency_p50_ms: float = 0.0
    info_latency_p95_ms: float = 0.0
    max_concurrent_requests: int = 0

    def __post_init__(self) -> None:
        if self.resolved + self.failed + self.unresolved_timeout != self.target:
            raise ValueError("la contabilidad de resolución contractual no cuadra")


class IbkrMarketDataProvider:
    """Proveedor del scanner y operaciones de descubrimiento para el diagnóstico."""

    # Client Portal market-data field IDs. 7638 is *Option Open Interest*;
    # 7087/7088 are aggregate put/call OI for an underlying, not a contract.
    UNDERLYING_SNAPSHOT_FIELDS = "31,84,86"
    OPTION_FIELD_IDS = {
        "bid": "84",
        "ask": "86",
        "delta": "7308",
        "gamma": "7309",
        "theta": "7310",
        "vega": "7311",
        "implied_volatility": "7633",
        "open_interest": "7638",
    }
    MARKET_DATA_AVAILABILITY_FIELD = "6509"
    OPTION_SNAPSHOT_FIELDS = ",".join((*OPTION_FIELD_IDS.values(), MARKET_DATA_AVAILABILITY_FIELD))
    DEEP_OPTION_SNAPSHOT_FIELDS = "31,84,86,6509,7308,7309,7310,7311,7633,7635,7638"
    # Kept as a compatibility alias for callers/tests written before fields
    # were split by instrument type.
    SNAPSHOT_FIELDS = UNDERLYING_SNAPSHOT_FIELDS
    # The same backoff is used by production snapshots and by the deep
    # diagnostic.  Client Portal snapshots are asynchronous: the pre-flight
    # response often contains only the contract identifier.
    SNAPSHOT_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 3.0)

    @staticmethod
    def _canonical_implied_volatility(value: float | None) -> float | None:
        """Convert IBKR field 7633 percentage points to a decimal fraction.

        The scanner's canonical IV unit is a decimal fraction: 0.482 means
        48.2%.  Client Portal field 7633 is documented and labelled as a
        percentage, so conversion happens exactly once at the provider edge.
        """
        return None if value is None else value / 100

    def __init__(
        self,
        transport: IbkrTransport,
        *,
        snapshot_attempts: int = 5,
        snapshot_retry_delay: float | None = None,
    ) -> None:
        self._transport = transport
        self._snapshot_attempts = max(1, snapshot_attempts)
        default_delays = self.SNAPSHOT_RETRY_DELAYS + (
            (self.SNAPSHOT_RETRY_DELAYS[-1],) * max(0, self._snapshot_attempts - len(self.SNAPSHOT_RETRY_DELAYS))
        )
        self._snapshot_retry_delays = (
            tuple(max(0.0, snapshot_retry_delay) for _ in range(self._snapshot_attempts))
            if snapshot_retry_delay is not None
            else default_delays[:self._snapshot_attempts]
        )
        self._searched_underlyings: set[str] = set()
        self._strikes_cache: dict[tuple[str, date], tuple[float, ...]] = {}
        self._contract_cache: dict[tuple[str, str, date, float], tuple[ConfirmedOptionContract, ...]] = {}
        self._contract_cache_lock = threading.Lock()
        self.http_call_counts: Counter[str] = Counter()
        self.last_underlying: Underlying | None = None
        self.last_underlying_conid: str | None = None
        self.last_historical_bars_received = 0
        self.last_underlying_market_data_availability: MarketDataAvailability | None = None

    def _get(self, path: str, params: Mapping[str, str]) -> Any:
        """Count safe endpoint names while leaving request details private."""
        endpoint = path.rstrip("/").rsplit("/", 2)[-2:]
        self.http_call_counts["/".join(endpoint)] += 1
        return self._transport.get(path, params)

    def get(self, path: str, params: Mapping[str, str]) -> Any:
        """Expose the counted transport boundary to internal provider adapters."""
        return self._get(path, params)

    def require_authenticated_session(self) -> None:
        data = self._get("/iserver/auth/status", {})
        if not isinstance(data, Mapping) or not data.get("authenticated"):
            raise NotAuthenticatedError("Client Portal Gateway está disponible, pero la sesión no está autenticada")

    def get_historical_bars(
        self, symbol: str, period: HistoricalPeriod = HistoricalPeriod.SIX_MONTHS
    ) -> tuple[HistoricalBar, ...]:
        """Return provider-neutral daily bars using this provider's Gateway session."""
        # Reuse the exact stock contract resolved by the scan. Besides avoiding a
        # redundant secdef request, this prevents an ambiguous symbol search from
        # selecting a different listing for history.
        adapter = IbkrHistoricalDataProvider(
            self,
            lambda value: self.last_underlying_conid or self.locate_stock(value)[0],
        )
        bars = adapter.get_historical_bars(symbol, period)
        self.last_historical_bars_received = adapter.last_bars_received
        return bars

    def locate_stock(self, symbol: str) -> tuple[str, tuple[date, ...]]:
        data = self._get("/iserver/secdef/search", {"symbol": symbol.upper(), "secType": "STK"})
        rows = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else []
        row = next((item for item in rows if isinstance(item, Mapping) and str(item.get("symbol", "")).upper() == symbol.upper()), None)
        if row is None:
            # Some Gateway versions expose exchange-traded funds as ETF rather
            # than STK. This only broadens underlying discovery; option contract
            # identity below remains strictly validated as OPT/right=P.
            data = self._get("/iserver/secdef/search", {"symbol": symbol.upper(), "secType": "ETF"})
            rows = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else []
            row = next((item for item in rows if isinstance(item, Mapping)
                        and str(item.get("symbol", "")).upper() == symbol.upper()
                        and str(item.get("secType", "ETF")).upper() in ("ETF", "STK")), None)
        if row is None or row.get("conid") is None:
            raise TickerNotFoundError(f"ticker no encontrado: {symbol.upper()}")
        self._searched_underlyings.add(str(row["conid"]))
        months = ""
        for section in row.get("sections", ()):
            if isinstance(section, Mapping) and section.get("secType") == "OPT":
                months = str(section.get("months", ""))
                break
        expirations = tuple(filter(None, (_parse_ibkr_month(value) for value in months.split(";") if value)))
        if not expirations:
            raise IncompleteDataError(f"IBKR no devolvió vencimientos de opciones para {symbol.upper()}")
        return str(row["conid"]), expirations

    def get_underlying_by_conid(self, symbol: str, conid: str, *, deadline: float | None = None) -> Underlying:
        rows = self._snapshot(
            (conid,),
            fields=self.UNDERLYING_SNAPSHOT_FIELDS,
            ready=lambda row: _number(row, "31") is not None
            or (_number(row, "84") is not None and _number(row, "86") is not None),
            deadline=deadline,
        )
        if not rows:
            raise IncompleteDataError("IBKR no devolvió el snapshot del subyacente; la respuesta fue parcial o incompleta")
        row = rows[0]
        if "6509" in row:
            self.last_underlying_market_data_availability = _market_data_availability(row.get("6509"))
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

    def resolve_underlying(
        self, symbol: str, *, deadline: float | None = None
    ) -> tuple[Underlying, str, tuple[date, ...]]:
        """Resolve a stock and obtain its price through the canonical snapshot flow."""

        conid, months = self.locate_stock(symbol)
        underlying = self.get_underlying_by_conid(symbol, conid, deadline=deadline)
        self.last_underlying = underlying
        self.last_underlying_conid = conid
        return underlying, conid, months

    def get_put_strikes(self, conid: str, expiration: date) -> tuple[float, ...]:
        self._require_derivative_search(conid)
        key = (str(conid), expiration)
        if key in self._strikes_cache:
            return self._strikes_cache[key]
        data = self._get("/iserver/secdef/strikes", {
            "conid": conid, "secType": "OPT", "month": _format_ibkr_month(expiration), "exchange": "SMART",
        })
        values = data.get("put", ()) if isinstance(data, Mapping) else ()
        strikes = tuple(float(value) for value in values)
        if not strikes:
            raise IncompleteDataError("IBKR no devolvió strikes PUT para el vencimiento seleccionado")
        self._strikes_cache[key] = strikes
        return strikes

    def get_put_contracts(self, conid: str, expiration: date, strikes: Sequence[float], *, symbol: str) -> tuple[tuple[str, float], ...]:
        """Return only contracts whose complete identity was confirmed.

        ``expiration`` must be an exact expiry, rather than the first day used
        merely to represent a monthly secdef bucket.
        """
        self._require_derivative_search(conid)
        contracts: list[tuple[str, float]] = []
        for strike in strikes:
            contract = self.confirm_put_contract(
                conid, symbol, expiration, strike,
                exact_maturity=expiration.strftime("%Y%m%d"),
            )
            contracts.append((contract.conid, contract.strike))
        if not contracts:
            raise IncompleteDataError("IBKR no devolvió contratos PUT para los strikes seleccionados")
        return tuple(contracts)

    def discover_put_contracts(
        self, conid: str, month: date, strikes: Sequence[float], *, symbol: str,
        deadline: float | None = None,
        progress: Callable[[int, int], None] | None = None,
        accounting: Callable[[ContractResolutionAccounting], None] | None = None,
        max_workers: int = 4,
    ) -> tuple[ConfirmedOptionContract, ...]:
        """Descubre identidades PUT válidas con concurrencia pequeña y acotada.

        Cada respuesta sigue ligada a su strike, se valida íntegramente y los
        fallos aislados no invalidan contratos ya confirmados. El orden de
        salida es el orden de los strikes de entrada, no el de finalización.
        """
        self._require_derivative_search(conid)
        requested_strikes = tuple(float(value) for value in strikes)
        unique_strikes = tuple(dict.fromkeys(requested_strikes))
        total = len(unique_strikes)
        deduplicated = len(requested_strikes) - total

        metric_lock = threading.Lock()
        latencies: list[float] = []
        info_calls = 0
        active_requests = 0
        maximum_requests = 0

        def resolve(strike: float) -> tuple[tuple[ConfirmedOptionContract, ...], bool]:
            nonlocal info_calls, active_requests, maximum_requests
            key = (str(conid), symbol.upper(), month, float(strike))
            with self._contract_cache_lock:
                candidates = self._contract_cache.get(key)
            validated = candidates is None
            if candidates is None:
                with metric_lock:
                    info_calls += 1
                    active_requests += 1
                    maximum_requests = max(maximum_requests, active_requests)
                request_started = time.monotonic()
                try:
                    data = self._get("/iserver/secdef/info", {
                        "conid": conid, "secType": "OPT", "month": _format_ibkr_month(month),
                        "exchange": "SMART", "strike": str(strike), "right": "P",
                    })
                finally:
                    elapsed = max(0.0, time.monotonic() - request_started)
                    with metric_lock:
                        active_requests -= 1
                        latencies.append(elapsed)
                rows = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else ()
                found = []
                for row in rows:
                    if not isinstance(row, Mapping) or row.get("conid") is None:
                        continue
                    candidate = _confirmed_option(row)
                    exact = _parse_maturity_date(candidate.maturity_date)
                    if exact is not None and _contract_matches(candidate, symbol, month, strike, candidate.maturity_date):
                        found.append(candidate)
                candidates = tuple(found)
                # Cache only the result of a real secdef/info validation.
                with self._contract_cache_lock:
                    self._contract_cache[key] = candidates
            return candidates, validated

        results: dict[float, tuple[tuple[ConfirmedOptionContract, ...], bool]] = {}
        next_strike = 0
        executor = ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 16)))
        futures = {}

        def submit_one() -> bool:
            nonlocal next_strike
            if deadline is not None and time.monotonic() >= deadline:
                return False
            if next_strike >= total:
                return False
            strike = unique_strikes[next_strike]
            next_strike += 1
            futures[executor.submit(resolve, strike)] = strike
            return True

        try:
            for _ in range(max(1, min(int(max_workers), 16))):
                if not submit_one():
                    break
            completed = 0
            while futures:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    # If every validation was launched, the deadline only caught
                    # the tail of already-running HTTP calls. Drain that tail so
                    # completed work is not reported as a partial timeout.
                    if next_strike == total:
                        done, _ = wait(futures)
                    else:
                        done = {future for future in futures if future.done()}
                    if not done:
                        break
                else:
                    done, _ = wait(futures, timeout=remaining, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                for future in done:
                    strike = futures.pop(future)
                    try:
                        results[strike] = future.result()
                    except Exception as exc:  # one failed secdef/info must not abort the scan
                        logger.warning("No se pudo validar el contrato PUT strike=%g: %s", strike, exc)
                        results[strike] = ((), True)
                    completed += 1
                    if progress is not None:
                        progress(completed, total)
                    submit_one()
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        confirmed: dict[str, ConfirmedOptionContract] = {}
        for strike in unique_strikes:
            candidates, _ = results.get(strike, ((), True))
            for candidate in candidates:
                confirmed.setdefault(candidate.conid, candidate)
        cache_hits = sum(not validated for _, validated in results.values())
        target = total
        resolved = sum(bool(candidates) for candidates, _ in results.values())
        failed = sum(not candidates for candidates, _ in results.values())
        validations_succeeded = sum(bool(candidates) and validated for candidates, validated in results.values())
        validations_failed = sum(not candidates and validated for candidates, validated in results.values())
        unresolved = target - resolved - failed
        ordered_latencies = sorted(latencies)
        def percentile(fraction: float) -> float:
            if not ordered_latencies:
                return 0.0
            return ordered_latencies[min(len(ordered_latencies) - 1, int((len(ordered_latencies) - 1) * fraction))] * 1000
        if accounting is not None:
            accounting(ContractResolutionAccounting(
                target, resolved, failed, unresolved, deduplicated,
                candidate_strikes=len(requested_strikes), info_calls=info_calls,
                cache_hits=cache_hits, validations_succeeded=validations_succeeded,
                validations_failed=validations_failed, info_latency_mean_ms=(sum(latencies) / len(latencies) * 1000 if latencies else 0.0),
                info_latency_p50_ms=percentile(.50), info_latency_p95_ms=percentile(.95),
                max_concurrent_requests=maximum_requests,
            ))
        return tuple(confirmed.values())

    @staticmethod
    def contract_expiration(contract: ConfirmedOptionContract) -> date:
        expiration = _parse_maturity_date(contract.maturity_date)
        if expiration is None:
            raise ContractMismatchError("el contrato confirmado no contiene maturityDate válido")
        return expiration

    def confirm_put_contract(
        self,
        underlying_conid: str,
        symbol: str,
        expiration: date,
        strike: float,
        *,
        exact_maturity: str | None = None,
    ) -> ConfirmedOptionContract:
        """Resuelve y valida un PUT antes de usar su conid para market data.

        ``expiration`` identifica el mes usado por ``secdef/strikes``. Cuando
        se facilita ``exact_maturity`` (YYYYMMDD), también se exige ese día.
        """

        self._require_derivative_search(underlying_conid)
        data = self._get("/iserver/secdef/info", {
            "conid": str(underlying_conid), "secType": "OPT",
            "month": _format_ibkr_month(expiration), "exchange": "SMART",
            "strike": str(strike), "right": "P",
        })
        rows = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else ()
        candidates: list[ConfirmedOptionContract] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("conid") is None:
                continue
            contract = _confirmed_option(row)
            if _contract_matches(contract, symbol, expiration, strike, exact_maturity):
                candidates.append(contract)
        if not candidates:
            raise ContractMismatchError(
                "secdef/info no confirmó exactamente el PUT solicitado "
                f"({symbol.upper()} strike={strike:g} vencimiento={exact_maturity or expiration.strftime('%Y-%m')})"
            )
        if len(candidates) > 1 and exact_maturity is None:
            raise ContractMismatchError(
                "secdef/info devolvió varios PUT compatibles en el mes; indica un vencimiento exacto YYYY-MM-DD"
            )
        return candidates[0]

    def get_put_quotes(self, contracts: Sequence[tuple[str, float]], expiration: date) -> tuple[IbkrOptionQuote, ...]:
        # Preserve the diagnostic/library behavior of the original public API;
        # the productive scanner opts out unless --verbose is requested.
        return self.get_put_quotes_batched(contracts, expiration, verbose=True)

    def get_put_quotes_batched(
        self,
        contracts: Sequence[tuple[str, float]],
        expiration: date,
        *,
        batch_size: int = 50,
        attempts: int = 2,
        deadline: float | None = None,
        progress: Callable[[int, int], None] | None = None,
        verbose: bool = False,
    ) -> tuple[IbkrOptionQuote, ...]:
        """Fetch option snapshots in bounded batches, merging partial fields.

        Bid, ask and delta are the only fields that can end the polling early;
        the remaining greeks, IV and OI are opportunistic display data.
        """
        size = max(1, batch_size)
        batches = [contracts[index:index + size] for index in range(0, len(contracts), size)]
        rows: list[Mapping[str, Any]] = []
        for index, batch in enumerate(batches, 1):
            if deadline is not None and time.monotonic() >= deadline:
                break
            if progress is not None:
                progress(index, len(batches))
            conids = tuple(conid for conid, _ in batch)
            rows.extend(self._snapshot(
                conids,
                fields=self.OPTION_SNAPSHOT_FIELDS,
                ready=lambda row: all(_number(row, field_id) is not None for field_id in ("84", "86", "7308")),
                attempts=max(1, attempts),
                deadline=deadline,
            ))
        by_conid = {str(row.get("conid")): row for row in rows}
        quotes = []
        for conid, strike in contracts:
            row = by_conid.get(conid, {})
            self._raise_if_unauthorized(row)
            statuses = self._option_field_statuses(row)
            missing = [name for name, status in statuses.items() if status is not MarketDataFieldStatus.AVAILABLE]
            if missing and verbose:
                logger.warning(
                    "Snapshot de opción conid=%s incompleto tras pre-flight/reintentos: %s; campos recibidos=%s",
                    conid,
                    ", ".join(f"{name}={statuses[name].value}" for name in missing),
                    _safe_snapshot_summary(row),
                )
            quotes.append(IbkrOptionQuote(
                conid, strike, expiration, _number(row, "84", "bid"), _number(row, "86", "ask"),
                _number(row, "7308", "delta"), _number(row, "7309", "gamma"),
                _number(row, "7310", "theta"), _number(row, "7311", "vega"),
                self._canonical_implied_volatility(_number(row, "7633", "iv")),
                _integer(row, "7638", "open_interest"),
                _market_data_availability(row.get(self.MARKET_DATA_AVAILABILITY_FIELD)),
                statuses,
            ))
        return tuple(quotes)

    def diagnose_put_contract(
        self,
        underlying_conid: str,
        contract_conid: str,
        *,
        retry_delays: Sequence[float] | None = None,
    ) -> tuple[DeepSnapshotAttempt, ...]:
        """Captura la evolución de un único snapshot de opción, sin fusionarla.

        Es deliberadamente una primitiva de diagnóstico de solo lectura. Cada
        elemento conserva únicamente los market-data field IDs solicitados; no
        expone el payload completo, cabeceras ni datos de sesión.
        """

        self._require_derivative_search(underlying_conid)
        params = {"conids": str(contract_conid), "fields": self.DEEP_OPTION_SNAPSHOT_FIELDS}
        observations: list[DeepSnapshotAttempt] = []

        rows = self._snapshot_request(params)
        for row in rows:
            self._raise_if_unauthorized(row)
        observations.append(DeepSnapshotAttempt("pre-flight", 0, _deep_snapshot_fields(rows, contract_conid)))
        delays = self.SNAPSHOT_RETRY_DELAYS if retry_delays is None else retry_delays
        for attempt, delay in enumerate(delays, 1):
            if delay > 0:
                time.sleep(delay)
            rows = self._snapshot_request(params)
            for row in rows:
                self._raise_if_unauthorized(row)
            observations.append(DeepSnapshotAttempt("snapshot", attempt, _deep_snapshot_fields(rows, contract_conid)))
        return tuple(observations)

    def _require_derivative_search(self, conid: str) -> None:
        if str(conid) not in self._searched_underlyings:
            raise IncompleteDataError(
                "antes de resolver o solicitar datos de derivados debe llamarse a "
                "/iserver/secdef/search para el subyacente"
            )

    def _snapshot(
        self,
        conids: Sequence[str],
        *,
        fields: str,
        ready: Callable[[Mapping[str, Any]], bool] | None = None,
        attempts: int | None = None,
        deadline: float | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        params = {"conids": ",".join(conids), "fields": fields}

        # Client Portal usa la primera llamada como pre-flight para iniciar las
        # suscripciones. Puede responder únicamente con conid/conidEx.
        preflight_rows = self._snapshot_request(params)
        logger.debug("Pre-flight snapshot conids=%s: %s", ",".join(conids), [_safe_snapshot_summary(row) for row in preflight_rows])
        for row in preflight_rows:
            self._raise_if_unauthorized(row)

        merged: dict[str, dict[str, Any]] = {}
        self._merge_snapshot_rows(merged, preflight_rows, conids)
        retry_count = self._snapshot_attempts if attempts is None else attempts
        delays = self._snapshot_retry_delays[:retry_count]
        for attempt, delay in enumerate(delays):
            # También se espera después del pre-flight: IBKR documenta que la
            # primera respuesta inicia la suscripción y los datos son asíncronos.
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(delay, remaining)
            if delay:
                time.sleep(delay)
            if deadline is not None and time.monotonic() >= deadline:
                break
            rows = self._snapshot_request(params)
            logger.debug("Snapshot intento %d/%d conids=%s: %s", attempt + 1, retry_count, ",".join(conids), [_safe_snapshot_summary(row) for row in rows])
            for row in rows:
                self._raise_if_unauthorized(row)
            self._merge_snapshot_rows(merged, rows, conids)
            if self._snapshot_is_ready(merged, conids, ready, fields):
                break

        return tuple(merged.get(str(conid), {}) for conid in conids)

    @staticmethod
    def _merge_snapshot_rows(
        merged: dict[str, dict[str, Any]],
        rows: Sequence[Mapping[str, Any]],
        conids: Sequence[str],
    ) -> None:
        """Accumulate asynchronous partial deliveries, including pre-flight."""

        for index, row in enumerate(rows):
            identifier = row.get("conid", row.get("conidEx", conids[index] if index < len(conids) else index))
            key = str(identifier).split("@", 1)[0]
            target = merged.setdefault(key, {"conid": key})
            target.update(row)

    def _snapshot_request(self, params: Mapping[str, str]) -> tuple[Mapping[str, Any], ...]:
        metric = (
            "marketdata/snapshot/underlying"
            if params.get("fields") == self.UNDERLYING_SNAPSHOT_FIELDS
            else "marketdata/snapshot/options"
        )
        self.http_call_counts[metric] += 1
        data = self._get("/iserver/marketdata/snapshot", params)
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
        underlying, _, _ = self.resolve_underlying(symbol)
        return underlying

    def get_option_market_data(self, symbol: str) -> tuple[MarketData, ...]:
        """Discover, confirm and quote PUTs without trusting a monthly conid."""

        underlying_conid, months = self.locate_stock(symbol)
        confirmed: dict[str, ConfirmedOptionContract] = {}
        for month in months:
            strikes = self.get_put_strikes(underlying_conid, month)
            for candidate in self.discover_put_contracts(underlying_conid, month, strikes, symbol=symbol):
                confirmed[candidate.conid] = candidate

        if not confirmed:
            raise IncompleteDataError("secdef/info no confirmó ningún contrato PUT con vencimiento exacto")

        result: list[MarketData] = []
        by_expiry: dict[date, list[ConfirmedOptionContract]] = {}
        for contract in confirmed.values():
            expiry = _parse_maturity_date(contract.maturity_date)
            if expiry is not None:
                by_expiry.setdefault(expiry, []).append(contract)
        for expiry, contracts in by_expiry.items():
            pairs = tuple((contract.conid, contract.strike) for contract in contracts)
            for quote in self.get_put_quotes(pairs, expiry):
                values = (quote.bid, quote.ask, quote.delta, quote.gamma, quote.theta, quote.vega, quote.implied_volatility)
                if any(value is None for value in values) or quote.open_interest is None:
                    continue
                contract = OptionContract(quote.conid, symbol.upper(), OptionType.PUT, quote.strike, expiry)
                result.append(MarketData(
                    contract, *values, 0, quote.open_interest,
                    market_data_availability=quote.market_data_availability.raw,
                ))
        return tuple(result)

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


def _parse_maturity_date(value: str) -> date | None:
    try:
        return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
    except (TypeError, ValueError):
        return None


def _confirmed_option(row: Mapping[str, Any]) -> ConfirmedOptionContract:
    """Copy only documented, non-session contract attributes."""

    maturity = row.get("maturityDate", row.get("maturity", row.get("expiration", "")))
    try:
        strike = float(row.get("strike", "nan"))
    except (TypeError, ValueError):
        strike = float("nan")
    return ConfirmedOptionContract(
        conid=str(row["conid"]),
        symbol=str(row.get("symbol", "")),
        sec_type=str(row.get("secType", "")),
        exchange=str(row.get("exchange", "")),
        listing_exchange=str(row.get("listingExchange", "")),
        right=str(row.get("right", "")),
        strike=strike,
        maturity_date=re.sub(r"[^0-9]", "", str(maturity)),
        multiplier=str(row.get("multiplier", "")),
        trading_class=str(row.get("tradingClass", "")),
        valid_exchanges=str(row.get("validExchanges", "")),
    )


def _contract_matches(
    contract: ConfirmedOptionContract,
    symbol: str,
    expiration: date,
    strike: float,
    exact_maturity: str | None,
) -> bool:
    expected_month = expiration.strftime("%Y%m")
    expected_maturity = re.sub(r"[^0-9]", "", exact_maturity or "")
    maturity_matches = (
        contract.maturity_date == expected_maturity and contract.maturity_date.startswith(expected_month)
        if expected_maturity
        else contract.maturity_date.startswith(expected_month)
    )
    return (
        contract.symbol.upper() == symbol.upper()
        and contract.sec_type.upper() in {"OPT", "OPTION"}
        and contract.right.upper() in {"P", "PUT"}
        and abs(contract.strike - strike) < 1e-9
        and maturity_matches
    )


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

    allowed = {"conid", "conidEx", "error", "message", "84", "86", "7308", "7309", "7310", "7311", "7633", "7638", "6509", "31"}
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


def _deep_snapshot_fields(rows: Sequence[Mapping[str, Any]], conid: str) -> Mapping[str, Any]:
    """Select only requested market fields from the row for ``conid``."""

    row = next(
        (item for item in rows if str(item.get("conid", item.get("conidEx", ""))).split("@", 1)[0] == str(conid)),
        rows[0] if len(rows) == 1 else {},
    )
    allowed = set(IbkrMarketDataProvider.DEEP_OPTION_SNAPSHOT_FIELDS.split(","))
    return {field: _safe_market_value(row[field]) for field in allowed if field in row}


def _safe_market_value(value: Any) -> Any:
    """Keep scalar market values only and bound strings before diagnostics."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return " ".join(value.split())[:80]
    return "valor no escalar omitido"


def _has_permission_message(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in ("not subscribed", "permission", "market data subscription", "unauthorized"))


def _market_data_availability(value: Any) -> MarketDataAvailability:
    """Decode documented 6509 flags without guessing from missing quote fields."""

    raw = None if value is None else str(value).strip()
    first = raw[:1] if raw else ""
    feed = {
        "R": "RealTime",
        "D": "Delayed",
        "Z": "Frozen",
        "Y": "Frozen-Delayed",
        "N": "Not Subscribed",
    }.get(first, "no indicado")
    return MarketDataAvailability(raw or None, feed, bool(raw and "i" in raw), bool(raw and "B" in raw))
