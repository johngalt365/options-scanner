"""Ticker universe selection, independent from scanning and persistence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import re

from options_scanner.models import Watchlist
from options_scanner.workspace import UserWorkspaceStore


DEFAULT_GROUPS: dict[str, tuple[str, ...]] = {
    "mega-cap-tech": ("AAPL", "MSFT", "NVDA", "AMZN", "META"),
    "indices": ("SPY", "QQQ", "IWM"),
}
_VALID_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,11}$")


class UniverseSource(StrEnum):
    GROUP = "group"
    WATCHLIST = "watchlist"
    MANUAL = "manual"


def normalize_tickers(value: str | Iterable[str]) -> tuple[str, ...]:
    """Normalize, validate and de-duplicate symbols while preserving order."""
    raw = re.split(r"[\s,]+", value.strip()) if isinstance(value, str) else value
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        symbol = str(item).strip().upper()
        if not symbol:
            continue
        if not _VALID_TICKER.fullmatch(symbol):
            raise ValueError(f"Ticker inválido: {symbol}")
        if symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    if not normalized:
        raise ValueError("El universo debe contener al menos un ticker.")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class TickerUniverse:
    source: UniverseSource
    source_name: str
    tickers: tuple[str, ...]


class TickerUniverseManager:
    """Resolve exactly one source using configurable groups and the memory store."""

    def __init__(self, store: UserWorkspaceStore, groups: Mapping[str, Iterable[str]] | None = None):
        self.store = store
        configured = groups if groups is not None else DEFAULT_GROUPS
        self.groups = {name: normalize_tickers(symbols) for name, symbols in configured.items()}

    def resolve(self, source: UniverseSource | str, *, user_id: str,
                group: str = "", watchlist_id: str = "", manual: str = "") -> TickerUniverse:
        selected = UniverseSource(source)
        if selected is UniverseSource.GROUP:
            if group not in self.groups:
                raise ValueError("Grupo de tickers desconocido.")
            return TickerUniverse(selected, group, self.groups[group])
        if selected is UniverseSource.WATCHLIST:
            watchlist = next((item for item in self.store.watchlists_for(user_id)
                              if item.id == watchlist_id), None)
            if watchlist is None:
                raise ValueError("Watchlist desconocida para este usuario.")
            return TickerUniverse(selected, watchlist.name, normalize_tickers(watchlist.symbols))
        return TickerUniverse(selected, "Lista manual temporal", normalize_tickers(manual))

    def save_manual(self, *, user_id: str, watchlist_id: str, name: str,
                    manual: str) -> Watchlist:
        watchlist = Watchlist(watchlist_id.strip(), user_id, name.strip(), normalize_tickers(manual))
        self.store.save_watchlist(watchlist)
        return watchlist
