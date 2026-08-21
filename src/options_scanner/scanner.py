"""Servicio de aplicación y resultados del scanner de venta de PUTs."""

from dataclasses import dataclass
from datetime import date
from typing import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from options_scanner.technical_analysis import PriceZone

from options_scanner.filters import filter_put_candidates, safety_margin
from options_scanner.market_data import MarketDataProvider
from options_scanner.models import MarketData, short_put_theta


@dataclass(frozen=True, slots=True)
class PutScanCandidate:
    ticker: str
    expiration: date
    dte: int
    strike: float
    underlying_price: float
    safety_margin: float
    bid: float | None
    ask: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    implied_volatility: float | None
    open_interest: int | None
    market_data_availability: str | None
    nearest_support_below: "PriceZone | None" = None
    support_position: str | None = None
    distance_to_support_pct: float | None = None
    support_strength: str | None = None
    support_zone_label: str | None = None
    support_position_label: str | None = None
    support_last_contact_sessions: int | None = None
    event_context: str = "normal"

    @property
    def mid(self) -> float | None:
        return None if self.bid is None or self.ask is None else (self.bid + self.ask) / 2

    @property
    def premium_yield(self) -> float | None:
        return None if self.mid is None else self.mid / self.strike

    @property
    def annualized_premium_yield(self) -> float | None:
        value = self.premium_yield
        return None if value is None or self.dte <= 0 else value * 365 / self.dte

    @property
    def contract_theta(self) -> float | None:
        """Theta delivered for the long option contract, preserved unchanged."""
        return self.theta

    @property
    def short_theta(self) -> float | None:
        """Position theta for a short PUT: the opposite of contract theta."""
        return short_put_theta(self.contract_theta)

    @property
    def theta_decay_pct_per_day(self) -> float | None:
        """Theoretical daily decay relative to premium, not a guaranteed return."""
        mid = self.mid
        return None if mid is None or mid <= 0 or self.short_theta is None else self.short_theta / mid * 100

    @property
    def complete(self) -> bool:
        """Los campos necesarios para filtrar y clasificar están presentes."""
        return self.mid is not None and self.delta is not None


def build_candidates(underlying_price: float, quotes: Iterable[MarketData], as_of: date) -> list[PutScanCandidate]:
    return [
        PutScanCandidate(
            q.contract.underlying_symbol, q.contract.expiration, q.contract.days_to_expiration(as_of),
            q.contract.strike, underlying_price, safety_margin(underlying_price, q.contract.strike),
            q.bid, q.ask, q.delta, q.gamma, q.theta, q.vega, q.implied_volatility,
            q.open_interest, q.market_data_availability,
        )
        for q in quotes
    ]


def rank_candidates(candidates: Iterable[PutScanCandidate]) -> list[PutScanCandidate]:
    """Excluye incompletos y ordena por rentabilidad anualizada descendente."""
    return sorted(
        (candidate for candidate in candidates if candidate.complete),
        key=lambda candidate: candidate.annualized_premium_yield or 0.0,
        reverse=True,
    )


def scan_puts(provider: MarketDataProvider, symbol: str, as_of: date, **filters: float | int) -> list[MarketData]:
    underlying = provider.get_underlying(symbol)
    quotes = provider.get_option_market_data(symbol)
    return filter_put_candidates(underlying, quotes, as_of, **filters)
