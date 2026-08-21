"""Diagnóstico técnico multi-ticker, independiente del scanner de opciones."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import escape
import logging
from pathlib import Path
import sys
import time
from typing import Protocol

from options_scanner.historical import HistoricalDataProvider, HistoricalPeriod
from options_scanner.ibkr import ClientPortalTransport, IbkrError, IbkrMarketDataProvider
from options_scanner.models import Underlying
from options_scanner.technical_analysis import PriceZone
from options_scanner.technical_context import TechnicalContext, build_technical_context


DEFAULT_TICKERS = ("NVDA", "AAPL", "MSFT", "AMZN", "TSLA")
logger = logging.getLogger(__name__)


class PriceProvider(Protocol):
    def get_underlying(self, symbol: str) -> Underlying: ...


@dataclass(frozen=True, slots=True)
class TechnicalCheckResult:
    symbol: str
    period: HistoricalPeriod
    price: float | None
    context: TechnicalContext | None
    historical_status: str
    error: str | None = None
    market_data_status: str = "Disponible"
    bars_received: int = 0
    historical_seconds: float = 0.0
    technical_seconds: float = 0.0

    @property
    def bar_count(self) -> int:
        return len(self.context.bars) if self.context else 0


def check_tickers(
    tickers: tuple[str, ...],
    period: HistoricalPeriod,
    price_provider: PriceProvider,
    history_provider: HistoricalDataProvider | None = None,
    *, clock=time.monotonic,
) -> tuple[TechnicalCheckResult, ...]:
    """Build each ticker independently and retain partial results on failure."""
    history_provider = history_provider or price_provider  # type: ignore[assignment]
    results: list[TechnicalCheckResult] = []
    for raw_symbol in tickers:
        symbol = raw_symbol.strip().upper()
        try:
            price = price_provider.get_underlying(symbol).current_price
        except Exception as exc:
            results.append(TechnicalCheckResult(
                symbol, period, None, None, "not_requested", _safe_error(exc), "No disponible",
            ))
            continue
        try:
            historical_started = clock()
            bars = history_provider.get_historical_bars(symbol, period)
            historical_seconds = max(0.0, clock() - historical_started)
            bars_received = getattr(history_provider, "last_historical_bars_received",
                                    getattr(history_provider, "last_bars_received", len(bars)))
            status = "ok" if bars else "empty"
            technical_started = clock()
            context = build_technical_context(symbol, period, bars, price)
            technical_seconds = max(0.0, clock() - technical_started)
            availability = getattr(price_provider, "last_underlying_market_data_availability", None)
            market_status = availability.feed if availability is not None else "Disponible"
            result = TechnicalCheckResult(symbol, period, price, context, status, None, market_status,
                                          bars_received, historical_seconds, technical_seconds)
            results.append(result)
            logger.info(
                "ticker=%s historical/bars_received=%d historical/bars_valid=%d "
                "technical/supports_active=%d technical/resistances_active=%d "
                "historical_time=%.3fs technical_time=%.3fs",
                symbol, result.bars_received, result.bar_count,
                len(context.supports_below_price), len(context.resistances_above_price),
                historical_seconds, technical_seconds,
            )
        except Exception as exc:
            results.append(TechnicalCheckResult(symbol, period, price, None, "error", _safe_error(exc),
                                                "No disponible"))
    return tuple(results)


def _safe_error(exc: Exception) -> str:
    """Expose only an exception class and its human-safe domain message."""
    message = str(exc) if isinstance(exc, (IbkrError, ValueError, KeyError)) else "fallo inesperado"
    return f"{type(exc).__name__}: {message}"


def _visible_zones(result: TechnicalCheckResult) -> tuple[tuple[str, PriceZone | None], ...]:
    context = result.context
    supports = context.supports_below_price[:3] if context else ()
    resistances = context.resistances_above_price[:2] if context else ()
    return tuple(
        [(f"S{i}", supports[i - 1] if len(supports) >= i else None) for i in range(1, 4)]
        + [(f"R{i}", resistances[i - 1] if len(resistances) >= i else None) for i in range(1, 3)]
    )


def format_summary(results: tuple[TechnicalCheckResult, ...]) -> str:
    rows = ["Ticker | Precio | Estado | Nivel | Centro | Fuerza | Contactos | Último contacto | Barras | Histórico"]
    rows.append("-" * 104)
    for result in results:
        for index, (label, zone) in enumerate(_visible_zones(result)):
            rows.append(" | ".join((
                result.symbol if index == 0 else "",
                f"${result.price:.2f}" if index == 0 and result.price is not None else ("N/D" if index == 0 else ""),
                result.market_data_status if index == 0 else "",
                label,
                f"${zone.center:.2f}" if zone else "N/D",
                zone.strength if zone else "N/D",
                str(zone.contacts) if zone else "N/D",
                zone.last_contact.isoformat() if zone else "N/D",
                str(result.bar_count) if index == 0 else "",
                result.historical_status if index == 0 else "",
            )))
        if result.error:
            rows.append(f"  ERROR {result.symbol}: {result.error}")
    return "\n".join(rows)


def render_charts(results: tuple[TechnicalCheckResult, ...]) -> str:
    """Return a standalone report whose charts remain collapsed by default."""
    sections = []
    for result in results:
        title = f"{result.symbol} · " + (f"${result.price:.2f}" if result.price is not None else "N/D")
        chart = _svg_chart(result.context) if result.context and result.context.bars else "<p>Histórico no disponible.</p>"
        sections.append(f"<details><summary>{escape(title)}</summary>{chart}</details>")
    return """<!doctype html><html lang="es"><meta charset="utf-8"><title>Validación técnica</title>
<style>body{font:15px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}details{border:1px solid #ccc;border-radius:8px;margin:.7rem 0;padding:.7rem}summary{cursor:pointer;font-weight:700}svg{width:100%;height:auto;background:#fafafa}.price{fill:none;stroke:#2463eb;stroke-width:2}.support{fill:#22c55e33}.resistance{fill:#ef444433}.current{stroke:#111;stroke-dasharray:6 4}text{font-size:12px}</style>
<h1>Validación técnica multi-ticker</h1><p>Los gráficos están colapsados por defecto.</p>""" + "".join(sections) + "</html>"


def _svg_chart(context: TechnicalContext) -> str:
    bars = context.bars
    zones = context.supports_below_price[:3] + context.resistances_above_price[:2]
    values = [value for bar in bars for value in (bar.low, bar.high)] + [context.current_price]
    values.extend(value for zone in zones for value in (zone.lower, zone.upper))
    low, high = min(values), max(values)
    span = max(high - low, 1e-9)
    x = lambda i: 55 + i * 925 / max(1, len(bars) - 1)
    y = lambda value: 15 + (high - value) * 320 / span
    areas = []
    for zone in context.supports_below_price[:3]:
        areas.append(f'<rect class="support" x="55" y="{y(zone.upper):.1f}" width="925" height="{max(2, y(zone.lower)-y(zone.upper)):.1f}"/>')
    for zone in context.resistances_above_price[:2]:
        areas.append(f'<rect class="resistance" x="55" y="{y(zone.upper):.1f}" width="925" height="{max(2, y(zone.lower)-y(zone.upper)):.1f}"/>')
    path = " ".join(("M" if i == 0 else "L") + f"{x(i):.1f},{y(bar.close):.1f}" for i, bar in enumerate(bars))
    current_y = y(context.current_price)
    return (f'<svg role="img" aria-label="Gráfico técnico de {escape(context.symbol)}" viewBox="0 0 1000 360">'
            + "".join(areas) + f'<path class="price" d="{path}"/><line class="current" x1="55" x2="980" y1="{current_y:.1f}" y2="{current_y:.1f}"/>'
            f'<text x="60" y="352">{bars[0].session}</text><text x="900" y="352">{bars[-1].session}</text></svg>')


def _period(value: str) -> HistoricalPeriod:
    try:
        return HistoricalPeriod(value.lower())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("period debe ser 3M, 6M o 1Y") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Valida zonas técnicas sin ejecutar el scanner de opciones")
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    parser.add_argument("--period", type=_period, default=HistoricalPeriod.SIX_MONTHS)
    parser.add_argument("--base-url", default="https://localhost:5000/v1/api")
    parser.add_argument("--insecure-tls", action="store_true")
    parser.add_argument("--charts", type=Path, metavar="HTML", help="genera gráficos HTML colapsados bajo demanda")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tickers = tuple(dict.fromkeys(value.strip().upper() for value in args.tickers.split(",") if value.strip()))
    if not tickers:
        print("ERROR: --tickers no puede estar vacío", file=sys.stderr)
        return 2
    provider = IbkrMarketDataProvider(ClientPortalTransport(args.base_url, allow_insecure_tls=args.insecure_tls))
    results = check_tickers(tickers, args.period, provider)
    print(format_summary(results))
    if args.charts:
        args.charts.write_text(render_charts(results), encoding="utf-8")
        print(f"Gráficos: {args.charts}")
    return 1 if all(result.price is None for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
