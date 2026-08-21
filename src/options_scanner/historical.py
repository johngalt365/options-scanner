"""Read-only daily history providers, independent from presentation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from math import sin
from typing import Any, Mapping, Protocol, Sequence


class HistoricalPeriod(StrEnum):
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    ONE_YEAR = "1y"

    @property
    def sessions(self) -> int:
        return {self.THREE_MONTHS: 66, self.SIX_MONTHS: 132, self.ONE_YEAR: 264}[self]


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class HistoricalDataProvider(Protocol):
    def get_historical_bars(self, symbol: str, period: HistoricalPeriod = HistoricalPeriod.SIX_MONTHS) -> tuple[HistoricalBar, ...]: ...


def map_ibkr_historical_bars(payload: Any) -> tuple[HistoricalBar, ...]:
    """Map Client Portal history response into provider-neutral domain bars."""
    rows = payload.get("data", ()) if isinstance(payload, Mapping) else ()
    result = []
    for row in rows if isinstance(rows, Sequence) else ():
        if not isinstance(row, Mapping):
            continue
        try:
            raw_time = row.get("t")
            session = (datetime.fromtimestamp(float(raw_time) / 1000, timezone.utc).date()
                       if isinstance(raw_time, (int, float)) else date.fromisoformat(str(raw_time)[:10]))
            result.append(HistoricalBar(session, float(row["o"]), float(row["h"]),
                                        float(row["l"]), float(row["c"]),
                                        float(row["v"]) if row.get("v") is not None else None))
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return tuple(sorted(result, key=lambda bar: bar.session))


class IbkrHistoricalDataProvider:
    """History adapter reusing an authenticated Client Portal transport (no orders)."""
    def __init__(self, transport: object, conid_resolver=None) -> None:
        self._transport = transport
        self._resolver = conid_resolver

    def get_historical_bars(self, symbol: str, period: HistoricalPeriod = HistoricalPeriod.SIX_MONTHS) -> tuple[HistoricalBar, ...]:
        if self._resolver:
            conid = self._resolver(symbol)
        else:
            rows = self._transport.get("/iserver/secdef/search", {"symbol": symbol.upper(), "secType": "STK"})
            match = next((r for r in rows if isinstance(r, Mapping) and str(r.get("symbol", "")).upper() == symbol.upper()), None)
            if not match or match.get("conid") is None:
                return ()
            conid = str(match["conid"])
        payload = self._transport.get("/iserver/marketdata/history", {
            "conid": str(conid), "period": period.value, "bar": "1d", "outsideRth": "true",
        })
        return map_ibkr_historical_bars(payload)


class DemoHistoricalDataProvider:
    """Reproducible synthetic daily OHLC series for local demonstration."""
    def __init__(self, today: date = date(2026, 8, 21)) -> None:
        self.today = today

    def get_historical_bars(self, symbol: str, period: HistoricalPeriod = HistoricalPeriod.SIX_MONTHS) -> tuple[HistoricalBar, ...]:
        bars, cursor, i = [], self.today - timedelta(days=period.sessions * 2), 0
        while len(bars) < period.sessions:
            if cursor.weekday() < 5:
                base = 100 + i * .045 + 5.2 * sin(i / 8) + 1.7 * sin(i / 3.1)
                close = base + .65 * sin(i / 2.3)
                bars.append(HistoricalBar(cursor, base - .3, max(base, close) + 1.15,
                                          min(base, close) - 1.1, close, 1_000_000 + i * 137))
                i += 1
            cursor += timedelta(days=1)
        return tuple(bars[-period.sessions:])
