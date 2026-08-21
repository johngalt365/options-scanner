from datetime import date
from unittest import TestCase

from options_scanner.market_data import FakeMarketDataProvider
from options_scanner.models import (
    SavedScanResult,
    StrategyParameters,
    User,
    Watchlist,
)
from options_scanner.scanner import scan_puts
from options_scanner.workspace import UserWorkspaceStore


class UserWorkspaceStoreTest(TestCase):
    def setUp(self) -> None:
        self.store = UserWorkspaceStore()
        self.ana = User("ana", "Ana")
        self.bruno = User("bruno", "Bruno")
        self.store.add_user(self.ana)
        self.store.add_user(self.bruno)

    def test_keeps_two_users_workspaces_separate(self) -> None:
        ana_watchlist = Watchlist("main", "ana", "Tecnología", ("NVDA",))
        bruno_watchlist = Watchlist("main", "bruno", "Índices", ("SPY",))
        ana_parameters = StrategyParameters("put", "ana", "Conservadora", min_dte=40)
        bruno_parameters = StrategyParameters("put", "bruno", "Corta", max_dte=35)
        ana_result = SavedScanResult("latest", "ana", "put", ())
        bruno_result = SavedScanResult("latest", "bruno", "put", ())

        for item in (ana_watchlist, bruno_watchlist):
            self.store.save_watchlist(item)
        for item in (ana_parameters, bruno_parameters):
            self.store.save_strategy_parameters(item)
        for item in (ana_result, bruno_result):
            self.store.save_scan_result(item)

        self.assertEqual(self.store.watchlists_for("ana"), (ana_watchlist,))
        self.assertEqual(self.store.watchlists_for("bruno"), (bruno_watchlist,))
        self.assertEqual(
            self.store.strategy_parameters_for("ana"), (ana_parameters,)
        )
        self.assertEqual(
            self.store.strategy_parameters_for("bruno"), (bruno_parameters,)
        )
        self.assertEqual(self.store.scan_results_for("ana"), (ana_result,))
        self.assertEqual(self.store.scan_results_for("bruno"), (bruno_result,))

    def test_rejects_data_for_an_unknown_user(self) -> None:
        with self.assertRaises(KeyError):
            self.store.save_watchlist(Watchlist("main", "unknown", "Lista"))

    def test_delete_watchlist_is_user_scoped(self) -> None:
        item = Watchlist("main", "ana", "Tecnología", ("NVDA",))
        self.store.save_watchlist(item)
        with self.assertRaises(KeyError):
            self.store.delete_watchlist("bruno", "main")
        self.assertEqual(self.store.watchlists_for("ana"), (item,))
        self.store.delete_watchlist("ana", "main")
        self.assertEqual(self.store.watchlists_for("ana"), ())

    def test_scan_result_cannot_reference_another_users_configuration(self) -> None:
        self.store.save_strategy_parameters(
            StrategyParameters("private", "ana", "Privada")
        )

        with self.assertRaises(ValueError):
            self.store.save_scan_result(
                SavedScanResult("result", "bruno", "private", ())
            )


class UserIndependentScannerTest(TestCase):
    def test_scanner_runs_without_user_context(self) -> None:
        as_of = date(2026, 8, 20)
        result = scan_puts(FakeMarketDataProvider(), "NVDA", as_of)
        self.assertEqual(len(result), 2)
