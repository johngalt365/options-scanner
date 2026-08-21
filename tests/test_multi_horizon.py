from datetime import date

from options_scanner.historical import HistoricalPeriod
from options_scanner.technical_analysis import PriceZone, ZoneType
from options_scanner.technical_context import (ConfluenceOrigin, TechnicalConfluence, TechnicalContext,
                                               classify_strike_against_confluences, find_confluences)


def zone(lo, hi, kind=ZoneType.SUPPORT, broken=False, strength="media"):
    return PriceZone(lo, hi, (lo+hi)/2, kind, 2, date(2026, 1, 1), 60, strength, broken)


def context(period, zones):
    supports=tuple(z for z in zones if z.kind == ZoneType.SUPPORT and not z.broken)
    resistances=tuple(z for z in zones if z.kind == ZoneType.RESISTANCE and not z.broken)
    return TechnicalContext("X", period, (), 100, tuple(zones), supports, resistances,
                            supports[0] if supports else None, resistances[0] if resistances else None,
                            None, None, ())


P=(HistoricalPeriod.THREE_MONTHS, HistoricalPeriod.SIX_MONTHS, HistoricalPeriod.ONE_YEAR)


def test_two_of_three_and_strike_inside_outside():
    result=find_confluences(tuple(context(p, [zone(78+i*2, 84+i*2)]) for i,p in enumerate(P)), 100)
    # 3M/6M and 6M/1A overlap, but all three only at the shared boundary $82.
    triple=next(item for item in result if len(item.origins)==3)
    assert (triple.lower, triple.upper)==(82, 84)
    assert triple.classify_strike(82)=="dentro"
    assert triple.classify_strike(90)=="por encima"
    assert triple.classify_strike(70)=="por debajo"


def test_pairwise_partial_overlaps_do_not_fabricate_three_of_three():
    zones=(zone(70, 80), zone(78, 88), zone(86, 96))
    result=find_confluences(tuple(context(p, [z]) for p,z in zip(P,zones)), 100)
    assert [(x.lower,x.upper,len(x.origins)) for x in result]==[(78,80,2),(86,88,2)]


def test_no_overlap_and_broken_zones_are_excluded():
    contexts=(context(P[0],[zone(70,72)]), context(P[1],[zone(80,82)]),
              context(P[2],[zone(70,72,broken=True)]))
    assert find_confluences(contexts,100)==()


def test_support_and_resistance_keep_origins_and_strengths():
    contexts=tuple(context(p,[zone(78,84,strength="fuerte"),
                                      zone(108,114,ZoneType.RESISTANCE,strength="débil")]) for p in P)
    result=find_confluences(contexts,100)
    assert {x.kind for x in result}=={ZoneType.SUPPORT,ZoneType.RESISTANCE}
    assert all(len(x.origins)==3 for x in result)
    assert all([o.zone.strength for o in x.origins] for x in result)


def test_one_horizon_without_history_still_allows_two_of_three():
    contexts=(context(P[0],[zone(78,84)]),context(P[1],[]),context(P[2],[zone(80,86)]))
    result=find_confluences(contexts,100)
    assert len(result)==1 and len(result[0].periods)==2


def test_relevant_support_confluence_classifies_strike_and_uses_nearest_edge():
    lower = TechnicalConfluence(50, 55, ZoneType.SUPPORT,
        (ConfluenceOrigin(P[0], zone(48, 55)), ConfluenceOrigin(P[1], zone(50, 57))), 0)
    relevant = TechnicalConfluence(70.73, 79.67, ZoneType.SUPPORT,
        tuple(ConfluenceOrigin(p, zone(70.73, 79.67)) for p in P), 0)

    inside = classify_strike_against_confluences(75, (lower, relevant))
    above = classify_strike_against_confluences(80, (lower, relevant))
    below = classify_strike_against_confluences(69, (lower, relevant))

    assert inside.confluence is relevant and inside.position_label == "Dentro de confluencia"
    assert above.confluence is relevant and above.position_label == "Sobre confluencia"
    assert round(above.distance_percent, 2) == 0.41
    assert below.confluence is relevant and below.position_label == "Bajo confluencia"


def test_confluence_context_preserves_two_and_three_horizons_and_empty_state():
    pair = TechnicalConfluence(70, 75, ZoneType.SUPPORT,
        (ConfluenceOrigin(P[0], zone(70, 76)), ConfluenceOrigin(P[2], zone(69, 75))), 0)
    triple = TechnicalConfluence(80, 85, ZoneType.SUPPORT,
        tuple(ConfluenceOrigin(p, zone(80, 85)) for p in P), 0)

    assert len(classify_strike_against_confluences(72, (pair, triple)).confluence.origins) == 2
    assert len(classify_strike_against_confluences(82, (pair, triple)).confluence.origins) == 3
    missing = classify_strike_against_confluences(75, ())
    assert missing.confluence is None and missing.position_label == "Sin confluencia relevante"


def test_multi_model_distinguishes_requested_available_and_participating_horizons():
    pair = TechnicalConfluence(70.73, 91.04, ZoneType.SUPPORT,
        (ConfluenceOrigin(P[0], zone(70, 92)), ConfluenceOrigin(P[1], zone(70.73, 91.04))), 0)
    multi = TechnicalContext("AEHR", HistoricalPeriod.MULTI, (), 100, (), (), (), None, None,
                             None, None, (), (), (pair,), P, P[:2])

    relationship = multi.classify_strike_against_confluence(80)

    assert multi.requested_horizons == P
    assert multi.available_horizons == P[:2]
    assert pair.participating_horizons == P[:2]
    assert relationship.horizon_ratio == "2/3"
    assert relationship.position_label == "Dentro de confluencia"
    assert relationship.explanation() == (
        "Strike $80.00 dentro de confluencia de soporte $70.73–$91.04 · 2/3 horizontes."
    )
