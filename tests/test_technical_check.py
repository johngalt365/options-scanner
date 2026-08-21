from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from options_scanner.historical import HistoricalBar, HistoricalPeriod
from options_scanner.models import Underlying
from options_scanner.technical_check import check_tickers, format_summary, main, render_charts
from options_scanner.ibkr import MarketDataAvailability
from options_scanner.technical_analysis import PriceZone, ZoneType
from options_scanner.technical_context import (SupportProximity, classify_support_proximity,
                                               distance_to_zone_percent)


def series(base: float) -> tuple[HistoricalBar, ...]:
    start = date(2026, 1, 1)
    values = tuple(base + ((index % 9) - 4) * 2 for index in range(80))
    return tuple(
        HistoricalBar(start + timedelta(days=index), value, value + 1, value - 1, value, 1000)
        for index, value in enumerate(values)
    )


class MultiTickerProvider:
    def __init__(self, failed: str | None = None) -> None:
        self.failed = failed
        self.price_calls: list[str] = []
        self.history_calls: list[tuple[str, HistoricalPeriod]] = []

    def get_underlying(self, symbol: str) -> Underlying:
        self.price_calls.append(symbol)
        if symbol == self.failed:
            raise KeyError(symbol)
        return Underlying(symbol, {"AAA": 100, "BBB": 200, "CCC": 300}[symbol])

    def get_historical_bars(self, symbol: str, period: HistoricalPeriod) -> tuple[HistoricalBar, ...]:
        self.history_calls.append((symbol, period))
        return series({"AAA": 100, "BBB": 200, "CCC": 300}[symbol])


class TechnicalCheckTest(TestCase):
    def test_support_proximity_uses_s1_upper_edge_and_exact_boundaries(self):
        support = PriceZone(90, 100, 95, ZoneType.SUPPORT, 3, date(2026, 1, 1), 1, "Media")
        cases = (
            (89.99, SupportProximity.BELOW),
            (90, SupportProximity.INSIDE),
            (100, SupportProximity.INSIDE),
            (102, SupportProximity.VERY_CLOSE),
            (102.01, SupportProximity.CLOSE),
            (105, SupportProximity.CLOSE),
            (105.01, SupportProximity.FAR),
        )
        for price, expected in cases:
            with self.subTest(price=price):
                self.assertEqual(classify_support_proximity(price, support), expected)
        self.assertIsNone(classify_support_proximity(100, None))
        self.assertEqual(distance_to_zone_percent(100, support), 0)
        self.assertAlmostEqual(distance_to_zone_percent(102, support), 2)

    def test_five_tickers_keep_deterministic_input_order(self):
        symbols = ("NVDA", "AAPL", "MSFT", "AMZN", "TSLA")
        class Provider:
            def get_underlying(self, symbol):
                return Underlying(symbol, 100 + symbols.index(symbol))
            def get_historical_bars(self, symbol, period):
                return series(100 + symbols.index(symbol))
        results = check_tickers(symbols, HistoricalPeriod.SIX_MONTHS, Provider())
        self.assertEqual(tuple(item.symbol for item in results), symbols)
        self.assertEqual(len({id(item.context) for item in results}), 5)

    def test_empty_history_is_safe_and_instrumented(self):
        class Provider:
            last_historical_bars_received = 7
            def get_underlying(self, symbol): return Underlying(symbol, 100)
            def get_historical_bars(self, symbol, period): return ()
        result = check_tickers(("NVDA",), HistoricalPeriod.SIX_MONTHS, Provider())[0]
        self.assertEqual((result.historical_status, result.bars_received, result.bar_count), ("empty", 7, 0))

    def test_mixed_realtime_and_frozen_states_are_captured_per_ticker(self):
        class Provider:
            def get_underlying(self, symbol):
                feed = "Frozen" if symbol == "AAPL" else "RealTime"
                self.last_underlying_market_data_availability = MarketDataAvailability(None, feed, False, False)
                return Underlying(symbol, 100)
            def get_historical_bars(self, symbol, period): return series(100)
        results = check_tickers(("NVDA", "AAPL"), HistoricalPeriod.SIX_MONTHS, Provider())
        self.assertEqual([item.market_data_status for item in results], ["RealTime", "Frozen"])

    def test_ticker_contexts_are_isolated(self):
        provider = MultiTickerProvider()
        results = check_tickers(("aaa", "bbb"), HistoricalPeriod.SIX_MONTHS, provider)

        self.assertEqual([item.symbol for item in results], ["AAA", "BBB"])
        self.assertEqual([item.price for item in results], [100, 200])
        self.assertTrue(all(bar.close < 150 for bar in results[0].context.bars))
        self.assertTrue(all(bar.close > 150 for bar in results[1].context.bars))
        self.assertEqual(provider.history_calls, [
            ("AAA", HistoricalPeriod.SIX_MONTHS), ("BBB", HistoricalPeriod.SIX_MONTHS),
        ])

    def test_one_failure_does_not_prevent_remaining_tickers(self):
        provider = MultiTickerProvider(failed="BBB")
        results = check_tickers(("AAA", "BBB", "CCC"), HistoricalPeriod.SIX_MONTHS, provider)

        self.assertEqual([item.historical_status for item in results], ["ok", "not_requested", "ok"])
        self.assertIsNone(results[1].context)
        self.assertIn("KeyError", results[1].error)
        self.assertEqual(provider.price_calls, ["AAA", "BBB", "CCC"])
        self.assertEqual([symbol for symbol, _ in provider.history_calls], ["AAA", "CCC"])

    def test_summary_has_all_levels_and_collapsed_charts(self):
        results = check_tickers(("AAA",), HistoricalPeriod.SIX_MONTHS, MultiTickerProvider())
        summary = format_summary(results)
        for value in ("Precio", "S1", "S2", "S3", "R1", "R2", "Fuerza", "Contactos", "80", "ok"):
            self.assertIn(value, summary)
        report = render_charts(results)
        self.assertIn("<details><summary>AAA", report)
        self.assertNotIn("<details open", report)
        self.assertIn('<svg role="img"', report)

    def test_cli_rejects_an_empty_ticker_list(self):
        self.assertEqual(main(["--tickers", " , "]), 2)

    def test_cli_runs_multiple_tickers_and_prints_safe_compact_rows(self):
        provider = MultiTickerProvider()
        with patch("options_scanner.technical_check.IbkrMarketDataProvider", return_value=provider), \
             redirect_stdout(StringIO()) as output:
            code = main(["--tickers", "CCC,AAA,BBB", "--period", "6M"])
        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertLess(text.index("CCC"), text.index("AAA"))
        self.assertLess(text.index("AAA"), text.index("BBB"))
        for heading in ("Precio", "Estado", "S1", "R2", "Fuerza", "Contactos", "Barras"):
            self.assertIn(heading, text)

    def test_chart_report_can_be_written(self):
        results = check_tickers(("AAA",), HistoricalPeriod.SIX_MONTHS, MultiTickerProvider())
        with TemporaryDirectory() as directory:
            target = Path(directory) / "charts.html"
            target.write_text(render_charts(results), encoding="utf-8")
            self.assertIn("AAA", target.read_text(encoding="utf-8"))
