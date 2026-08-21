"""Persistencia en memoria de datos pertenecientes a cada usuario."""

from collections import defaultdict
from typing import TypeVar

from options_scanner.models import (
    SavedScanResult,
    StrategyParameters,
    User,
    Watchlist,
)

WorkspaceItem = TypeVar("WorkspaceItem", Watchlist, StrategyParameters, SavedScanResult)


class UserWorkspaceStore:
    """Repositorio efímero con un espacio de nombres aislado por usuario."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._watchlists: dict[str, dict[str, Watchlist]] = defaultdict(dict)
        self._strategy_parameters: dict[
            str, dict[str, StrategyParameters]
        ] = defaultdict(dict)
        self._scan_results: dict[
            str, dict[str, SavedScanResult]
        ] = defaultdict(dict)

    def add_user(self, user: User) -> None:
        if user.id in self._users:
            raise ValueError(f"ya existe el usuario {user.id!r}")
        self._users[user.id] = user

    def save_watchlist(self, watchlist: Watchlist) -> None:
        self._save(watchlist, self._watchlists)

    def watchlists_for(self, user_id: str) -> tuple[Watchlist, ...]:
        return self._items_for(user_id, self._watchlists)

    def delete_watchlist(self, user_id: str, watchlist_id: str) -> None:
        """Delete one of ``user_id``'s lists without crossing user boundaries."""
        self._require_user(user_id)
        try:
            del self._watchlists[user_id][watchlist_id]
        except KeyError as error:
            raise KeyError("watchlist desconocida para este usuario") from error

    def save_strategy_parameters(self, parameters: StrategyParameters) -> None:
        self._save(parameters, self._strategy_parameters)

    def strategy_parameters_for(self, user_id: str) -> tuple[StrategyParameters, ...]:
        return self._items_for(user_id, self._strategy_parameters)

    def save_scan_result(self, result: SavedScanResult) -> None:
        parameters = self._strategy_parameters.get(result.user_id, {})
        if result.strategy_parameters_id not in parameters:
            raise ValueError(
                "el resultado debe referenciar parámetros del mismo usuario"
            )
        self._save(result, self._scan_results)

    def scan_results_for(self, user_id: str) -> tuple[SavedScanResult, ...]:
        return self._items_for(user_id, self._scan_results)

    def _save(
        self,
        item: WorkspaceItem,
        collection: dict[str, dict[str, WorkspaceItem]],
    ) -> None:
        self._require_user(item.user_id)
        collection[item.user_id][item.id] = item

    def _items_for(
        self,
        user_id: str,
        collection: dict[str, dict[str, WorkspaceItem]],
    ) -> tuple[WorkspaceItem, ...]:
        self._require_user(user_id)
        return tuple(collection.get(user_id, {}).values())

    def _require_user(self, user_id: str) -> User:
        try:
            return self._users[user_id]
        except KeyError as error:
            raise KeyError(f"usuario desconocido: {user_id!r}") from error
