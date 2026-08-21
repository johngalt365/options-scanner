"""Reusable application service for read-only PUT scans."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
import time
import logging
import threading
from typing import Callable

from options_scanner.ibkr import ClientPortalTransport, IbkrMarketDataProvider
from options_scanner.market_data import FakeMarketDataProvider
from options_scanner.scanner import PutScanCandidate, build_candidates, rank_candidates, scan_puts
from options_scanner.historical import DemoHistoricalDataProvider, HistoricalPeriod
from options_scanner.technical_context import (TechnicalContext, build_multi_technical_context,
                                               build_technical_context)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanRequest:
    ticker: str = "NVDA"
    min_dte: int = 30
    max_dte: int = 45
    min_safety_margin: float = .20
    min_abs_delta: float = .15
    max_abs_delta: float = .30
    fake: bool = False
    historical_period: HistoricalPeriod = HistoricalPeriod.SIX_MONTHS
    min_iv: float | None = None
    min_short_theta: float | None = None

    def __post_init__(self) -> None:
        ticker = self.ticker.strip().upper()
        if not ticker or len(ticker) > 12 or not ticker.replace(".", "").replace("-", "").isalnum():
            raise ValueError("El ticker debe ser un símbolo válido.")
        object.__setattr__(self, "ticker", ticker)
        if self.min_dte < 0 or self.max_dte < 0 or self.min_dte > self.max_dte:
            raise ValueError("El DTE mínimo debe ser menor o igual que el máximo.")
        if not 0 <= self.min_safety_margin <= 1:
            raise ValueError("La distancia al strike debe estar entre 0 % y 100 %.")
        if not 0 <= self.min_abs_delta <= self.max_abs_delta <= 1:
            raise ValueError("Las deltas deben estar entre 0 y 1, con mínimo menor o igual que máximo.")
        if self.min_iv is not None and self.min_iv < 0:
            raise ValueError("La IV mínima no puede ser negativa.")
        if self.min_short_theta is not None and self.min_short_theta < 0:
            raise ValueError("El theta short mínimo no puede ser negativo.")


@dataclass(slots=True)
class DiscardedContract:
    """A locally observed contract/strike and every reason it did not qualify."""

    ticker: str
    expiration: date | None
    strike: float
    reasons: tuple[str, ...]


@dataclass(slots=True)
class ScanMetrics:
    considered: int = 0
    complete: int = 0
    incomplete: int = 0
    rejected_margin: int = 0
    rejected_delta: int = 0
    rejected_dte: int = 0
    rejected_iv: int = 0
    rejected_theta: int = 0
    timed_out: bool = False
    timeout_phase: str | None = None
    target_contracts: int = 0
    resolved_contracts: int = 0
    failed_contracts: int = 0
    unresolved_contracts_timeout: int = 0
    deduplicated_contracts: int = 0
    candidate_strikes: int = 0
    secdef_info_calls: int = 0
    contract_cache_hits: int = 0
    contract_validations_succeeded: int = 0
    contract_validations_failed: int = 0
    secdef_info_latency_mean_ms: float = 0.0
    secdef_info_latency_p50_ms: float = 0.0
    secdef_info_latency_p95_ms: float = 0.0
    max_concurrent_contract_requests: int = 0
    with_bid_ask_delta: int = 0
    with_bid_ask_without_delta: int = 0
    with_delta_without_bid_ask: int = 0
    without_bid_ask_delta: int = 0
    market_data_realtime: int = 0
    market_data_frozen: int = 0
    market_data_delayed: int = 0
    market_data_not_subscribed: int = 0
    market_data_unknown: int = 0
    phase_seconds: dict[str, float] = field(default_factory=dict)
    historical_request: int = 0
    historical_bars_received: int = 0
    historical_bars_valid: int = 0
    historical_period: str = "6m"
    historical_status: str = "not_requested"
    technical_supports: int = 0
    technical_resistances: int = 0
    technical_supports_visible: int = 0
    technical_resistances_visible: int = 0
    http_calls: dict[str, int] = field(default_factory=dict)
    discarded_contracts: list[DiscardedContract] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ScanResult:
    candidates: tuple[PutScanCandidate, ...]
    summary: ScanMetrics
    elapsed_seconds: float
    incomplete_candidates: tuple[PutScanCandidate, ...] = ()
    underlying_price: float | None = None
    market_data_status: str | None = None
    updated_at: datetime | None = None
    simulated: bool = False
    technical_context: TechnicalContext | None = None


class PutScanService:
    """Runs the same productive flow for CLI and web; it never submits orders."""

    def __init__(self, *, today: Callable[[], date] = date.today, clock: Callable[[], float] = time.monotonic):
        self._today = today
        self._clock = clock

    def run(
        self, request: ScanRequest, *, base_url: str = "https://localhost:5000/v1/api",
        allow_insecure_tls: bool = False, scan_timeout: float = 30.0,
        market_data_timeout: float = 10.0, provider: object | None = None,
        batch_size: int = 50, snapshot_attempts: int = 2, contract_workers: int = 8,
        progress: bool = False, verbose: bool = False,
        work_limiter: threading.BoundedSemaphore | None = None,
    ) -> ScanResult:
        started = self._clock()
        as_of = self._today()
        summary = ScanMetrics()
        if request.fake:
            fake = provider or FakeMarketDataProvider()
            underlying = fake.get_underlying(request.ticker)
            all_quotes = fake.get_option_market_data(request.ticker)
            quotes = scan_puts(
                fake, request.ticker, as_of, min_dte=request.min_dte, max_dte=request.max_dte,
                min_safety_margin=request.min_safety_margin, min_abs_delta=request.min_abs_delta,
                max_abs_delta=request.max_abs_delta,
                min_iv=request.min_iv, min_short_theta=request.min_short_theta,
            )
            candidates = build_candidates(underlying.current_price, quotes, as_of)
            summary.considered = len(all_quotes)
            summary.complete = len(candidates)
            for quote in all_quotes:
                contract = quote.contract
                dte = contract.days_to_expiration(as_of)
                margin = (underlying.current_price - contract.strike) / underlying.current_price
                reasons = []
                if not request.min_dte <= dte <= request.max_dte:
                    summary.rejected_dte += 1
                    reasons.append(f"DTE {dte} fuera de {request.min_dte}–{request.max_dte}")
                if margin < request.min_safety_margin:
                    summary.rejected_margin += 1
                    reasons.append(
                        f"Distancia {margin * 100:.4f}% < mínimo {request.min_safety_margin * 100:.4f}%"
                    )
                incomplete = quote.delta is None or quote.bid is None or quote.ask is None
                if quote.delta is None:
                    reasons.append("Market data incompleta/no disponible: Delta N/D")
                elif not request.min_abs_delta <= abs(quote.delta) <= request.max_abs_delta:
                    summary.rejected_delta += 1
                    reasons.append(
                        f"|Delta| {abs(quote.delta):.6f} fuera de "
                        f"{request.min_abs_delta:.6f}–{request.max_abs_delta:.6f}"
                    )
                if request.min_iv is not None:
                    if quote.implied_volatility is None:
                        incomplete = True
                        reasons.append("Market data incompleta/no disponible: IV N/D")
                    elif quote.implied_volatility < request.min_iv:
                        summary.rejected_iv += 1
                        reasons.append(
                            f"IV {quote.implied_volatility * 100:.4f}% < mínimo {request.min_iv * 100:.4f}%"
                        )
                short_theta = None if quote.theta is None else -quote.theta
                if request.min_short_theta is not None:
                    if short_theta is None:
                        incomplete = True
                        reasons.append("Market data incompleta/no disponible: Theta short N/D")
                    elif short_theta < request.min_short_theta:
                        summary.rejected_theta += 1
                        reasons.append(
                            f"Theta short {short_theta:.6f} < mínimo {request.min_short_theta:.6f}"
                        )
                if quote.bid is None or quote.ask is None:
                    reasons.append("Market data incompleta/no disponible: bid/ask N/D")
                if incomplete:
                    summary.incomplete += 1
                if reasons:
                    summary.discarded_contracts.append(DiscardedContract(
                        request.ticker, contract.expiration, contract.strike, tuple(reasons)
                    ))
            market_status = "Simulado"
        else:
            # Kept here as a lazy import so legacy imports from scan_puts remain compatible.
            from options_scanner.scan_puts import _ibkr_candidates
            real_provider = provider or IbkrMarketDataProvider(ClientPortalTransport(
                base_url, allow_insecure_tls=allow_insecure_tls,
                timeout=max(.1, min(10.0, scan_timeout)),
                work_limiter=work_limiter,
            ))
            args = Namespace(
                ticker=request.ticker, min_dte=request.min_dte, max_dte=request.max_dte,
                min_safety_margin=request.min_safety_margin, min_abs_delta=request.min_abs_delta,
                max_abs_delta=request.max_abs_delta, scan_timeout=scan_timeout,
                min_iv=request.min_iv, min_short_theta=request.min_short_theta,
                market_data_timeout=market_data_timeout, batch_size=batch_size,
                snapshot_attempts=snapshot_attempts, contract_workers=contract_workers,
                progress=progress, verbose=verbose,
            )
            candidates = _ibkr_candidates(real_provider, args, as_of, summary=summary)
            market_status = next((name for name, count in (
                ("RealTime", summary.market_data_realtime), ("Frozen", summary.market_data_frozen),
                ("Delayed", summary.market_data_delayed), ("Not Subscribed", summary.market_data_not_subscribed),
            ) if count), None)
        ranked = tuple(rank_candidates(candidates))
        incomplete = tuple(candidate for candidate in candidates if not candidate.complete)
        resolved_underlying = underlying if request.fake else getattr(real_provider, "last_underlying", None)
        price = resolved_underlying.current_price if resolved_underlying is not None else None
        technical = None
        if price is not None:
            history_provider = DemoHistoricalDataProvider(as_of) if request.fake else real_provider
            summary.historical_request = 1
            summary.historical_period = request.historical_period.value
            history_started = self._clock()
            try:
                requested_period = (HistoricalPeriod.ONE_YEAR if request.historical_period == HistoricalPeriod.MULTI
                                    else request.historical_period)
                bars = history_provider.get_historical_bars(request.ticker, requested_period)
                summary.historical_bars_received = getattr(history_provider, "last_historical_bars_received", len(bars))
                summary.historical_bars_valid = len(bars)
                summary.historical_status = "ok" if bars else "empty"
            except Exception as exc:
                # History is supplementary. Never let its HTTP/parsing failure
                # invalidate option results and never log request/session data.
                bars = ()
                summary.historical_status = "error"
                logger.warning("Historical data unavailable (%s)", type(exc).__name__)
            summary.phase_seconds["historical_data"] = max(0.0, self._clock() - history_started)
            technical_started = self._clock()
            strikes = tuple(candidate.strike for candidate in ranked if candidate.complete)
            if request.historical_period == HistoricalPeriod.MULTI:
                histories = {period: tuple(bars[-period.sessions:]) for period in (
                    HistoricalPeriod.THREE_MONTHS, HistoricalPeriod.SIX_MONTHS, HistoricalPeriod.ONE_YEAR)}
                technical = build_multi_technical_context(request.ticker, histories, price, strikes)
            else:
                technical = build_technical_context(request.ticker, request.historical_period, bars, price, strikes)
            summary.technical_supports = len(technical.supports_below_price)
            summary.technical_resistances = len(technical.resistances_above_price)
            summary.technical_supports_visible = min(3, summary.technical_supports)
            summary.technical_resistances_visible = min(2, summary.technical_resistances)
            strike_contexts = {item.strike: item for item in technical.strikes}
            sessions_after_contact = {
                zone: sum(bar.session > zone.last_contact for bar in technical.bars)
                for zone in technical.supports_below_price
            }
            ranked = tuple(
                replace(candidate,
                    nearest_support_below=strike_contexts[candidate.strike].support,
                    support_position=strike_contexts[candidate.strike].position,
                    distance_to_support_pct=strike_contexts[candidate.strike].distance_percent,
                    support_strength=(strike_contexts[candidate.strike].support.strength
                                      if strike_contexts[candidate.strike].support else None),
                    support_zone_label=strike_contexts[candidate.strike].zone_label,
                    support_position_label=strike_contexts[candidate.strike].position_label,
                    support_last_contact_sessions=(
                        sessions_after_contact[strike_contexts[candidate.strike].support]
                        if strike_contexts[candidate.strike].support else None
                    )) if candidate.strike in strike_contexts else candidate
                for candidate in ranked
            )
            summary.phase_seconds["technical_analysis"] = max(0.0, self._clock() - technical_started)
            if verbose:
                logger.info(
                    "historical/request=%d historical/bars_received=%d historical/bars_valid=%d "
                    "historical/period=%s historical/status=%s technical/supports_active=%d "
                    "technical/resistances_active=%d technical/supports_visible=%d "
                    "technical/resistances_visible=%d historical_data=%.3fs technical_analysis=%.3fs",
                    summary.historical_request, summary.historical_bars_received,
                    summary.historical_bars_valid, summary.historical_period,
                    summary.historical_status, summary.technical_supports,
                    summary.technical_resistances, summary.technical_supports_visible,
                    summary.technical_resistances_visible, summary.phase_seconds["historical_data"],
                    summary.phase_seconds["technical_analysis"],
                )
        if not request.fake:
            summary.http_calls = dict(getattr(real_provider, "http_call_counts", {}))
        return ScanResult(ranked, summary, max(0.0, self._clock() - started), incomplete,
                          price, market_status, datetime.now(timezone.utc), request.fake, technical)
