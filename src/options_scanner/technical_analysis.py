"""Deterministic, explainable support/resistance analysis."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from options_scanner.historical import HistoricalBar

class PivotType(StrEnum):
    LOW = "low"
    HIGH = "high"
class ZoneType(StrEnum):
    SUPPORT = "support"
    RESISTANCE = "resistance"

@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    session: date
    price: float
    kind: PivotType
    reaction_atr: float = 0.0

@dataclass(frozen=True, slots=True)
class PriceZone:
    lower: float
    upper: float
    center: float
    kind: ZoneType
    contacts: int
    last_contact: date
    score: float
    strength: str
    broken: bool = False
    inverted: bool = False
    first_contact: date | None = None
    reaction: float = 0.0
    persistence: float = 0.0


def atr(bars: tuple[HistoricalBar, ...], period: int = 14) -> tuple[float, ...]:
    """Wilder ATR: TR=max(H-L, |H-prevC|, |L-prevC|); initial/running mean up to period, then Wilder smoothing."""
    if not bars: return ()
    trs=[]
    for i,b in enumerate(bars):
        prev=bars[i-1].close if i else b.close
        trs.append(max(b.high-b.low, abs(b.high-prev), abs(b.low-prev)))
    out=[]
    for i,tr in enumerate(trs):
        if i == 0: value=tr
        elif i < period: value=(out[-1]*i+tr)/(i+1)
        else: value=(out[-1]*(period-1)+tr)/period
        out.append(value)
    return tuple(out)


def detect_pivots(bars: tuple[HistoricalBar, ...], window: int = 3, atr_period: int = 14) -> tuple[Pivot, ...]:
    if window < 1: raise ValueError("window must be positive")
    if len(bars) < 2*window+1: return ()
    volatility=atr(bars, atr_period); result=[]
    for i in range(window, len(bars)-window):
        neighbors=bars[i-window:i]+bars[i+1:i+window+1]
        future=bars[i+1:i+window+1]
        if bars[i].low < min(b.low for b in neighbors):
            reaction=max(b.high for b in future)-bars[i].low
            result.append(Pivot(i,bars[i].session,bars[i].low,PivotType.LOW,reaction/max(volatility[i],1e-9)))
        if bars[i].high > max(b.high for b in neighbors):
            reaction=bars[i].high-min(b.low for b in future)
            result.append(Pivot(i,bars[i].session,bars[i].high,PivotType.HIGH,reaction/max(volatility[i],1e-9)))
    return tuple(result)


def cluster_zones(pivots: tuple[Pivot, ...], bars: tuple[HistoricalBar, ...], *, atr_period: int=14,
                  tolerance_atr: float=.6, break_atr: float=.5, min_contacts: int=1) -> tuple[PriceZone, ...]:
    if not pivots or not bars: return ()
    current_atr=atr(bars,atr_period)[-1]; tolerance=max(current_atr*tolerance_atr, bars[-1].close*.002)
    zones=[]
    for kind,zone_kind in ((PivotType.LOW,ZoneType.SUPPORT),(PivotType.HIGH,ZoneType.RESISTANCE)):
        groups=[]
        for pivot in sorted((p for p in pivots if p.kind==kind),key=lambda p:p.price):
            group=next((g for g in groups if abs(pivot.price-sum(x.price for x in g)/len(g))<=tolerance),None)
            if group is None:
                groups.append([pivot])
            else:
                group.append(pivot)
        for group in groups:
            if len(group)<min_contacts: continue
            prices=[p.price for p in group]; center=sum(prices)/len(prices)
            lower=min(prices)-tolerance/2; upper=max(prices)+tolerance/2
            last=max(p.session for p in group); first=min(p.session for p in group)
            age=max(0,(bars[-1].session-last).days); recency=max(0,1-age/180)
            reaction=min(2,sum(min(2,p.reaction_atr) for p in group)/len(group))/2
            persistence=min(1,max(0,(last-first).days)/120)
            score=min(100, 15 + min(40,len(group)*10) + 20*recency + 15*reaction + 10*persistence)
            strength="fuerte" if score>=70 else "media" if score>=45 else "débil"
            later=[b for b in bars if b.session>last]
            broken=(any(b.close < lower-break_atr*current_atr for b in later) if zone_kind==ZoneType.SUPPORT
                    else any(b.close > upper+break_atr*current_atr for b in later))
            zones.append(PriceZone(lower,upper,center,zone_kind,len(group),last,round(score,1),strength,
                                   broken,False,first,round(reaction,3),round(persistence,3)))
    return tuple(sorted(zones,key=lambda z:z.center))
