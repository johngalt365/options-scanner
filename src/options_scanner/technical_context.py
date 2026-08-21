"""Application service composing history, technical zones and strike context."""

from dataclasses import dataclass, field
from itertools import combinations, product
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


class SupportProximity(StrEnum):
    """Descriptive price position relative to the upper edge of S1."""

    INSIDE = "Dentro de soporte"
    VERY_CLOSE = "Muy cerca"
    CLOSE = "Cerca"
    FAR = "Alejado"
    BELOW = "Por debajo"


def classify_support_proximity(current_price: float, support: PriceZone | None) -> SupportProximity | None:
    """Classify proximity to an existing support without creating a zone.

    The thresholds are presentation-only and deliberately do not feed zone
    detection, scanner filters, scoring, or ranking.
    """
    if support is None:
        return None
    if current_price < support.lower:
        return SupportProximity.BELOW
    if current_price <= support.upper:
        return SupportProximity.INSIDE
    distance = (current_price - support.upper) / support.upper * 100
    if distance <= 2:
        return SupportProximity.VERY_CLOSE
    if distance <= 5:
        return SupportProximity.CLOSE
    return SupportProximity.FAR


def distance_to_zone_percent(current_price: float, zone: PriceZone | None) -> float | None:
    """Signed distance from price to the closest edge of an existing zone."""
    if zone is None:
        return None
    if zone.lower <= current_price <= zone.upper:
        return 0.0
    edge = zone.upper if current_price > zone.upper else zone.lower
    return (current_price - edge) / edge * 100


@dataclass(frozen=True, slots=True)
class StrikeContext:
    strike: float
    support: PriceZone | None
    distance_percent: float | None
    position: StrikePosition | None
    zone_label: str | None = None
    position_label: str | None = None


@dataclass(frozen=True, slots=True)
class ConfluenceOrigin:
    period: HistoricalPeriod
    zone: PriceZone


@dataclass(frozen=True, slots=True)
class TechnicalConfluence:
    lower: float
    upper: float
    kind: ZoneType
    origins: tuple[ConfluenceOrigin, ...]
    distance_percent: float

    @property
    def periods(self) -> tuple[HistoricalPeriod, ...]:
        return tuple(origin.period for origin in self.origins)

    def classify_strike(self, strike: float) -> str:
        if strike > self.upper:
            return "por encima"
        if strike < self.lower:
            return "por debajo"
        return "dentro"


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
    horizon_contexts: tuple["TechnicalContext", ...] = field(default=())
    confluences: tuple[TechnicalConfluence, ...] = field(default=())


def classify_strike_against_zones(
    strike: float, supports: tuple[PriceZone, ...] | list[PriceZone], current_price: float
) -> StrikeContext:
    """Classify *strike* in the complete ordered map of active supports.

    Containing zones take precedence. Otherwise relevance is the shortest
    distance to a zone boundary. ``distance_percent`` is zero within a zone;
    outside it, it is the signed percentage from the nearest boundary of the
    relevant support (positive above, negative below). ``current_price``
    validates the shared price frame and keeps this pure function explicit at
    call sites.
    """
    if current_price <= 0:
        raise ValueError("current_price must be positive")
    active = tuple(z for z in supports if z.kind == ZoneType.SUPPORT and not z.broken)
    if not active:
        return StrikeContext(strike, None, None, None)
    active = tuple(sorted(active, key=lambda zone: zone.center, reverse=True))
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
    support_index = active.index(support)
    zone_label = f"S{support_index + 1}"
    containing_index = next((i for i, zone in enumerate(active) if zone.lower <= strike <= zone.upper), None)
    if containing_index is not None:
        position_label = f"Dentro de S{containing_index + 1}"
    elif strike > active[0].upper:
        position_label = "Por encima de S1"
    elif strike < active[-1].lower:
        position_label = f"Por debajo de S{len(active)}"
    else:
        position_label = next(
            (f"Entre S{i + 1}/S{i + 2}" for i, (upper, lower) in
             enumerate(zip(active, active[1:])) if lower.upper < strike < upper.lower),
            f"Por {'encima' if position == StrikePosition.ABOVE_SUPPORT else 'debajo'} de {zone_label}",
        )
    relevant_boundary = (support.upper if strike > support.upper else
                         support.lower if strike < support.lower else strike)
    distance_percent = (strike - relevant_boundary) / relevant_boundary * 100
    return StrikeContext(strike, support, distance_percent,
                         position, zone_label, position_label)


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


def find_confluences(contexts: tuple[TechnicalContext, ...], current_price: float) -> tuple[TechnicalConfluence, ...]:
    """Return only real intersections between active zones from distinct horizons."""
    found: dict[tuple, TechnicalConfluence] = {}
    for kind in (ZoneType.SUPPORT, ZoneType.RESISTANCE):
        by_period = []
        for context in contexts:
            zones = (context.supports_below_price if kind == ZoneType.SUPPORT
                     else context.resistances_above_price)
            active = tuple(zone for zone in zones if not zone.broken)
            if active:
                by_period.append((context.period, active))
        for size in range(2, len(by_period) + 1):
            for selected in combinations(by_period, size):
                for zones in product(*(item[1] for item in selected)):
                    lower, upper = max(z.lower for z in zones), min(z.upper for z in zones)
                    if lower > upper:
                        continue
                    origins = tuple(ConfluenceOrigin(selected[i][0], zone) for i, zone in enumerate(zones))
                    # A genuine larger intersection supersedes its redundant pair intersections.
                    key = (kind, round(lower, 10), round(upper, 10), tuple(o.period for o in origins))
                    edge = upper if current_price > upper else lower if current_price < lower else current_price
                    found[key] = TechnicalConfluence(lower, upper, kind, origins,
                                                      (current_price - edge) / edge * 100)
    values = list(found.values())
    values = [item for item in values if not any(
        item.kind == other.kind and len(other.origins) > len(item.origins)
        and set(item.origins).issubset(set(other.origins)) for other in values
    )]
    return tuple(sorted(values, key=lambda item: (item.kind.value, item.lower, item.upper)))


def build_multi_technical_context(symbol, histories, current_price, strikes=(), *, window=3, atr_period=14):
    """Analyze each horizon independently, without changing any zone calibration."""
    periods = (HistoricalPeriod.THREE_MONTHS, HistoricalPeriod.SIX_MONTHS, HistoricalPeriod.ONE_YEAR)
    contexts = tuple(build_technical_context(symbol, period, tuple(histories.get(period, ())),
                                             current_price, strikes, window=window, atr_period=atr_period)
                     for period in periods)
    confluences = find_confluences(contexts, current_price)
    # Use the longest *available* horizon for the combined chart and visible
    # zones. A missing 1A response must not hide valid 3M/6M analysis.
    available = tuple(context for context in contexts if context.bars)
    display = available[-1] if available else None
    bars = display.bars if display else ()
    return TechnicalContext(symbol, HistoricalPeriod.MULTI, bars, current_price, (), (), (), None, None,
                            None, None, (), contexts, confluences)
