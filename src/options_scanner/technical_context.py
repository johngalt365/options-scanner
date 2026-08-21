"""Application service composing history, technical zones and strike context."""
from dataclasses import dataclass
from enum import StrEnum
from options_scanner.historical import HistoricalBar, HistoricalPeriod
from options_scanner.technical_analysis import PriceZone, ZoneType, detect_pivots, cluster_zones
class StrikePosition(StrEnum):
    ABOVE="above"; INSIDE="inside"; BELOW="below"
@dataclass(frozen=True,slots=True)
class StrikeContext:
    strike: float; support: PriceZone|None; distance_percent: float|None; position: StrikePosition|None
@dataclass(frozen=True,slots=True)
class TechnicalContext:
    symbol:str; period:HistoricalPeriod; bars:tuple[HistoricalBar,...]; current_price:float
    zones:tuple[PriceZone,...]; nearest_support:PriceZone|None; nearest_resistance:PriceZone|None
    support_distance_percent:float|None; resistance_distance_percent:float|None; strikes:tuple[StrikeContext,...]
def strike_context(strike:float,support:PriceZone|None)->StrikeContext:
    if support is None:return StrikeContext(strike,None,None,None)
    position=StrikePosition.ABOVE if strike>support.upper else StrikePosition.BELOW if strike<support.lower else StrikePosition.INSIDE
    return StrikeContext(strike,support,(strike-support.center)/support.center*100,position)
def build_technical_context(symbol,period,bars,current_price,strikes=(),*,window=3,atr_period=14):
    zones=cluster_zones(detect_pivots(bars,window,atr_period),bars,atr_period=atr_period)
    supports=[z for z in zones if z.kind==ZoneType.SUPPORT and not z.broken and z.center<=current_price]
    resistances=[z for z in zones if z.kind==ZoneType.RESISTANCE and not z.broken and z.center>=current_price]
    support=max(supports,key=lambda z:z.center,default=None); resistance=min(resistances,key=lambda z:z.center,default=None)
    distance=lambda z: abs(current_price-z.center)/current_price*100 if z else None
    return TechnicalContext(symbol,period,bars,current_price,zones,support,resistance,distance(support),distance(resistance),tuple(strike_context(s,support) for s in strikes))
