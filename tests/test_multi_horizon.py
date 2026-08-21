from datetime import date

from options_scanner.historical import HistoricalPeriod
from options_scanner.technical_analysis import PriceZone, ZoneType
from options_scanner.technical_context import TechnicalContext, find_confluences


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
