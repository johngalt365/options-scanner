from unittest import TestCase

from options_scanner.models import User, Watchlist
from options_scanner.universe import TickerUniverseManager, normalize_tickers
from options_scanner.workspace import UserWorkspaceStore


class TickerUniverseTest(TestCase):
    def setUp(self):
        self.store = UserWorkspaceStore()
        self.store.add_user(User("ana", "Ana"))
        self.store.add_user(User("bruno", "Bruno"))
        self.manager = TickerUniverseManager(
            self.store, {"tech": (" nvda ", "AAPL", "NVDA")}
        )

    def test_configurable_group_is_normalized(self):
        universe = self.manager.resolve("group", user_id="ana", group="tech")
        self.assertEqual(universe.tickers, ("NVDA", "AAPL"))

    def test_manual_accepts_commas_spaces_and_preserves_first_occurrence(self):
        universe = self.manager.resolve("manual", user_id="ana", manual=" nvda, aapl NVDA,msft ")
        self.assertEqual(universe.tickers, ("NVDA", "AAPL", "MSFT"))

    def test_manual_can_be_saved_as_watchlist(self):
        saved = self.manager.save_manual(user_id="ana", watchlist_id="mine", name="Mi lista",
                                         manual="spy, qqq SPY")
        self.assertEqual(saved.symbols, ("SPY", "QQQ"))
        self.assertEqual(self.manager.resolve("watchlist", user_id="ana",
                                              watchlist_id="mine").tickers, ("SPY", "QQQ"))

    def test_invalid_ticker_is_rejected(self):
        for value in ("AAPL$", "TOO-LONG-TICKER", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_tickers(value)

    def test_watchlists_are_isolated_by_user(self):
        self.store.save_watchlist(Watchlist("private", "ana", "Privada", ("NVDA",)))
        with self.assertRaises(ValueError):
            self.manager.resolve("watchlist", user_id="bruno", watchlist_id="private")
