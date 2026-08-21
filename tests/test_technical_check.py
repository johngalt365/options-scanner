from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from options_scanner.historical import HistoricalBar, HistoricalPeriod
from options_scanner.models import Underlying
from options_scanner.technical_check import check_tickers, format_summary, main, render_charts


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

    def test_chart_report_can_be_written(self):
        results = check_tickers(("AAA",), HistoricalPeriod.SIX_MONTHS, MultiTickerProvider())
        with TemporaryDirectory() as directory:
            target = Path(directory) / "charts.html"
            target.write_text(render_charts(results), encoding="utf-8")
            self.assertIn("AAA", target.read_text(encoding="utf-8"))
