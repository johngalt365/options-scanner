"""Explainable v1 ranking applied only after the existing Short PUT filters.

The deliberately bounded formula is documented in ``docs/short-put-ranking-v1.md``.
It is comparative, not trading advice, and delta is not treated as a probability.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, TYPE_CHECKING

from options_scanner.historical import HistoricalPeriod
from options_scanner.technical_context import ConfluenceStrikePosition, StrikePosition

if TYPE_CHECKING:
    from options_scanner.scanner import PutScanCandidate
    from options_scanner.technical_context import TechnicalContext

WEIGHTS = {"risk": 30.0, "technical": 25.0, "premium": 20.0, "theta": 15.0, "liquidity": 10.0}
LABEL_THRESHOLDS = ((80, "Muy sólida"), (65, "Sólida"), (45, "Intermedia"), (0, "Débil"))


@dataclass(frozen=True, slots=True)
class ShortPutEvaluation:
    total_score: float
    risk_score: float
    technical_score: float
    premium_score: float
    theta_score: float
    liquidity_score: float
    label: str
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    reasons: tuple[str, ...]
    missing_data: tuple[str, ...] = ()


def _clamp(value: float, maximum: float) -> float:
    return round(max(0.0, min(maximum, value)), 2)


def risk_score(delta: float | None, distance: float | None) -> tuple[float, tuple[str, ...]]:
    """18 delta points (peak plateau .15-.20) plus 12 distance points (sat. 30%)."""
    missing = []
    if delta is None:
        delta_points = 0
        missing.append("Delta")
    else:
        d = abs(delta)
        delta_points = 18 if d <= .20 else 18 - (d - .20) / .10 * 10
    if distance is None:
        distance_points = 0
        missing.append("distancia al strike")
    else:
        distance_points = 12 * max(0, distance) / .30
    return _clamp(delta_points, 18) + _clamp(distance_points, 12), tuple(missing)


def technical_score(candidate: "PutScanCandidate", context: "TechnicalContext | None") -> tuple[float, str, tuple[str, ...]]:
    if context is None:
        return 0.0, "contexto técnico no disponible", ("contexto técnico",)
    if context.period == HistoricalPeriod.MULTI:
        rel = context.classify_strike_against_confluence(candidate.strike)
        if rel.confluence is None:
            return 0.0, "sin confluencia de soporte", ()
        participating = len(rel.participating_horizons)
        requested = len(rel.requested_horizons)
        available = len(rel.available_horizons)
        # Coverage is against requested horizons, while availability is reported separately.
        coverage = participating / requested if requested else 0
        base = 18 * coverage
        distance = abs(rel.distance_percent or 0)
        position = {ConfluenceStrikePosition.INSIDE: 7, ConfluenceStrikePosition.BELOW: max(2, 6-distance),
                    ConfluenceStrikePosition.ABOVE: max(0, 6-2*distance)}[rel.position]
        text = f"confluencia {participating}/{requested} (disponibles {available}/{requested}), {rel.position.value.lower()}"
        return _clamp(base + position, 25), text, ()
    if candidate.nearest_support_below is None or candidate.support_position is None:
        return 0.0, "sin soporte relevante", ()
    distance = abs(candidate.distance_to_support_pct or 0)
    position = (20 if candidate.support_position == StrikePosition.INSIDE_SUPPORT else
                max(5, 18-distance) if candidate.support_position == StrikePosition.BELOW_SUPPORT else
                max(0, 16-2*distance))
    strength = {"fuerte": 5, "media": 3, "débil": 1}.get((candidate.support_strength or "").lower(), 2)
    return _clamp(position + strength, 25), f"{candidate.support_position_label}, soporte {candidate.support_strength or 'N/D'}", ()


def premium_score(premium_yield: float | None, iv: float | None) -> tuple[float, tuple[str, ...]]:
    """Premium yield supplies all points, linearly to a 5% saturation; IV is context only."""
    missing = (() if premium_yield is not None else ("premium yield",)) + (() if iv is not None else ("IV",))
    return _clamp(20 * (premium_yield or 0) / .05, 20), missing


def theta_score(theta_pct: float | None) -> tuple[float, tuple[str, ...]]:
    """Positive relative decay earns points linearly up to saturation at 5%/day."""
    return _clamp(15 * (theta_pct or 0) / 5, 15), (() if theta_pct is not None else ("theta relativo",))


def liquidity_score(spread: float | None, oi: int | None) -> tuple[float, tuple[str, ...]]:
    """Six spread points (0 at >=50%) and four log-like OI points saturated at 500."""
    missing = (() if spread is not None else ("spread relativo",)) + (() if oi is not None else ("open interest",))
    spread_points = 6 * max(0, 1 - (spread or 0) / .50) if spread is not None else 0
    oi_points = 4 * min(max(oi or 0, 0), 500) ** .5 / 500 ** .5
    return _clamp(spread_points + oi_points, 10), missing


def evaluate(candidate: "PutScanCandidate", context: "TechnicalContext | None" = None) -> ShortPutEvaluation:
    risk, m1 = risk_score(candidate.delta, candidate.safety_margin)
    technical, technical_reason, m2 = technical_score(candidate, context)
    premium, m3 = premium_score(candidate.premium_yield, candidate.implied_volatility)
    theta, m4 = theta_score(candidate.theta_decay_pct_per_day)
    liquidity, m5 = liquidity_score(candidate.relative_spread, candidate.open_interest)
    total = _clamp(risk + technical + premium + theta + liquidity, 100)
    label = next(label for threshold, label in LABEL_THRESHOLDS if total >= threshold)
    reasons = (f"|Delta| {abs(candidate.delta):.2f}" if candidate.delta is not None else "Delta N/D",
               f"distancia {candidate.safety_margin*100:.1f}%", technical_reason,
               f"premium yield {(candidate.premium_yield or 0)*100:.2f}%",
               f"IV {candidate.implied_volatility*100:.1f}%" if candidate.implied_volatility is not None else "IV N/D",
               f"theta relativo {candidate.theta_decay_pct_per_day:.2f}%/día" if candidate.theta_decay_pct_per_day is not None else "theta relativo N/D",
               f"spread relativo {candidate.relative_spread*100:.1f}%" if candidate.relative_spread is not None else "spread relativo N/D",
               f"OI {candidate.open_interest}" if candidate.open_interest is not None else "OI N/D")
    strengths = tuple(r for r in reasons if ("N/D" not in r and
        (r.startswith(("|Delta|", "distancia", "confluencia", "Dentro", "theta")))))
    weaknesses = tuple([*(f"Falta {x}" for x in (*m1, *m2, *m3, *m4, *m5)),
                        *(r for r in reasons if (r.startswith("spread") and candidate.relative_spread is not None and candidate.relative_spread >= .25) or
                                                 (r.startswith("OI ") and (candidate.open_interest or 0) < 50))])
    return ShortPutEvaluation(total, risk, technical, premium, theta, liquidity, label,
                              strengths, weaknesses, reasons, (*m1, *m2, *m3, *m4, *m5))


def rank_by_score(candidates: Iterable["PutScanCandidate"], context: "TechnicalContext | None" = None) -> list["PutScanCandidate"]:
    evaluated = [replace(c, evaluation=evaluate(c, context)) for c in candidates if c.complete]
    return sorted(evaluated, key=lambda c: (-c.evaluation.total_score, abs(c.delta),
        -(c.premium_yield or 0), -(c.open_interest or 0), c.ticker, c.expiration.isoformat(), c.strike))
