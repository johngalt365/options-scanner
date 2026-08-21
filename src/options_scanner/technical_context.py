"""Application service composing history, technical zones and strike context."""

from dataclasses import dataclass
from enum import StrEnum

from options_scanner.historical import HistoricalBar, HistoricalPeriod
from options_scanner.technical_analysis import PriceZone, ZoneType, cluster_zones, detect_pivots


class StrikePosition(StrEnum):
    ABOVE_SUPPORT = "ABOVE_SUPPORT"
    INSIDE_SUPPORT = "INSIDE_SUPPORT"
    BELOW_SUPPORT = "BELOW_SUPPORT"

    # Backwards-compatible names for callers of the original API.
    ABOVE = ABOVE_SUPPORT
    INSIDE = INSIDE_SUPPORT
    BELOW = BELOW_SUPPORT


@dataclass(frozen=True, slots=True)
class StrikeContext:
    strike: float
    support: PriceZone | None
    distance_percent: float | None
    position: StrikePosition | None


@dataclass(frozen=True, slots=True)
class TechnicalContext:
    symbol: str
    period: HistoricalPeriod
    bars: tuple[HistoricalBar, ...]
    current_price: float
    zones: tuple[PriceZone, ...]
    supports_below_price: tuple[PriceZone, ...]
    resistances_above_price: tuple[PriceZone, ...]
    nearest_support: PriceZone | None
    nearest_resistance: PriceZone | None
    support_distance_percent: float | None
    resistance_distance_percent: float | None
    strikes: tuple[StrikeContext, ...]


def classify_strike_against_zones(
    strike: float, supports: tuple[PriceZone, ...] | list[PriceZone], current_price: float
) -> StrikeContext:
    """Classify *strike* against the closest relevant active support.

    Containing zones take precedence. Otherwise relevance is the shortest
    distance to a zone boundary, which preserves the useful distinction for a
    strike located between S1 and S2. ``current_price`` validates the shared
    price frame and keeps this pure function explicit at call sites.
    """
    if current_price <= 0:
        raise ValueError("current_price must be positive")
    active = tuple(z for z in supports if z.kind == ZoneType.SUPPORT and not z.broken)
    if not active:
        return StrikeContext(strike, None, None, None)
    containing = next((z for z in active if z.lower <= strike <= z.upper), None)
    support = containing or min(
        active,
        key=lambda z: (z.lower - strike if strike < z.lower else strike - z.upper, abs(current_price - z.center)),
    )
    position = (
        StrikePosition.ABOVE_SUPPORT if strike > support.upper else
        StrikePosition.BELOW_SUPPORT if strike < support.lower else
        StrikePosition.INSIDE_SUPPORT
    )
    return StrikeContext(strike, support, (strike - support.center) / support.center * 100, position)


def strike_context(strike: float, support: PriceZone | None) -> StrikeContext:
    """Compatibility helper for classifying against one support."""
    if support is None:
        return StrikeContext(strike, None, None, None)
    return classify_strike_against_zones(strike, (support,), max(strike, support.center, 1e-9))


def build_technical_context(symbol, period, bars, current_price, strikes=(), *, window=3, atr_period=14):
    zones = cluster_zones(detect_pivots(bars, window, atr_period), bars, atr_period=atr_period)
    supports = tuple(sorted(
        (z for z in zones if z.kind == ZoneType.SUPPORT and not z.broken and z.center <= current_price),
        key=lambda z: z.center,
        reverse=True,
    ))
    resistances = tuple(sorted(
        (z for z in zones if z.kind == ZoneType.RESISTANCE and not z.broken and z.center >= current_price),
        key=lambda z: z.center,
    ))
    support = supports[0] if supports else None
    resistance = resistances[0] if resistances else None
    distance = lambda z: abs(current_price - z.center) / current_price * 100 if z else None
    return TechnicalContext(
        symbol, period, bars, current_price, zones, supports, resistances, support, resistance,
        distance(support), distance(resistance),
        tuple(classify_strike_against_zones(s, supports, current_price) for s in strikes),
    )
