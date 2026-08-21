"""Small dependency-free local WSGI interface for the read-only scanner."""

from __future__ import annotations

from html import escape
import json
import logging
import re
from uuid import uuid4
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from options_scanner.ibkr import GatewayUnavailableError, IbkrError, NotAuthenticatedError
from options_scanner.scan_service import PutScanService, ScanRequest, ScanResult
from options_scanner.historical import HistoricalPeriod
from options_scanner.technical_context import (StrikePosition, classify_support_proximity,
                                               distance_to_zone_percent)
from options_scanner.technical_check import (DEFAULT_TICKERS, TechnicalCheckResult,
                                             _svg_chart, _visible_zones, check_tickers)
from options_scanner.ibkr import ClientPortalTransport, IbkrMarketDataProvider
from options_scanner.models import User, Watchlist
from options_scanner.multi_scan import MultiScanMetrics, run_multi_ticker
from options_scanner.workspace import UserWorkspaceStore

logger = logging.getLogger(__name__)

TICKER_SEPARATOR = re.compile(r"[\s,]+")
PREDEFINED_UNIVERSES: dict[str, tuple[str, tuple[str, ...]]] = {
    "mega-cap": ("Mega-cap tecnología", ("AAPL", "MSFT", "NVDA", "AMZN", "META")),
    "indices": ("ETFs de índices", ("SPY", "QQQ", "IWM")),
}


def parse_tickers(value: str) -> tuple[str, ...]:
    """Normalize and validate a comma/whitespace separated ticker list."""
    symbols = tuple(dict.fromkeys(part.upper() for part in TICKER_SEPARATOR.split(value.strip()) if part))
    if not symbols:
        raise ValueError("Debes indicar al menos un ticker.")
    # Reuse the domain request validation rather than creating a second symbol policy.
    for symbol in symbols:
        ScanRequest(ticker=symbol)
    return symbols


def resolve_universe(source: str, manual_value: str,
                     watchlists: dict[str, tuple[str, ...]] | None = None) -> tuple[str, ...]:
    """Resolve every universe source through the canonical ticker normalizer."""
    if source == "manual":
        raw_symbols = manual_value
    elif source.startswith("group:"):
        try:
            raw_symbols = " ".join(PREDEFINED_UNIVERSES[source.removeprefix("group:")][1])
        except KeyError as error:
            raise ValueError("Grupo de tickers desconocido.") from error
    elif source.startswith("watchlist:"):
        try:
            raw_symbols = " ".join((watchlists or {})[source.removeprefix("watchlist:")])
        except KeyError as error:
            raise ValueError("Watchlist desconocida.") from error
    else:
        raise ValueError("Fuente de universo desconocida.")
    return parse_tickers(raw_symbols)


def _scan_state(result: ScanResult) -> str:
    return "Parcial" if result.summary.timed_out or result.summary.historical_status == "error" else "Completado"


def render_technical_screener(results: tuple[TechnicalCheckResult, ...]) -> bytes:
    """Render the compact validation view; charts are fetched separately."""
    rows = []
    for result in results:
        zones = dict(_visible_zones(result))
        support, resistance = zones["S1"], zones["R1"]
        def level(item) -> str:
            return ('<span class="na">N/D</span>' if item is None else
                    f'<strong>${item.lower:.2f}–${item.upper:.2f}</strong>')
        def value(item, attribute: str) -> str:
            return ('<span class="na">N/D</span>' if item is None else
                    escape(str(getattr(item, attribute))))
        def distance(item, *, proximity=False) -> str:
            amount = distance_to_zone_percent(result.price, item) if result.price is not None else None
            if amount is None:
                return '<span class="na">N/D</span>'
            if proximity and amount == 0:
                return 'Dentro S1<small class="proximity">Dentro de soporte</small>'
            detail = ""
            if proximity:
                classification = classify_support_proximity(result.price, item)
                detail = f'<small class="proximity">{escape(classification.value)}</small>' if classification else ""
            return f'{amount:+.2f} %{detail}'
        state_class = " unavailable" if result.error or result.historical_status != "ok" else ""
        price = f"${result.price:.2f}" if result.price is not None else "N/D"
        rows.append(
            f'<tr data-ticker="{escape(result.symbol)}" class="{state_class.strip()}">'
            f'<th scope="row">{escape(result.symbol)}</th><td>{price}</td>'
            f'<td>{escape(result.market_data_status)}</td>'
            f'<td>{level(support)}</td><td>{distance(support, proximity=True)}</td>'
            f'<td>{value(support, "strength")}</td><td>{value(support, "contacts")}</td>'
            f'<td>{level(resistance)}</td><td>{distance(resistance)}</td>'
            f'<td>{value(resistance, "strength")}</td>'
            f'<td>{result.bar_count}</td><td>{escape(result.historical_status)}</td>'
              f'<td><button class="chart-button" data-ticker="{escape(result.symbol)}" type="button">📈 Ver gráfico</button>'
              f'<div class="chart-drawer" data-chart="{escape(result.symbol)}" hidden></div></td></tr>'
        )
    body = "".join(rows)
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Validación técnica</title><style>
body{{font:14px system-ui;background:#f4f6fa;color:#182033;margin:0}}main{{max-width:1600px;margin:auto;padding:1.5rem}}a{{color:#2358d5}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;background:white;white-space:nowrap}}th,td{{padding:.65rem;border-bottom:1px solid #dde2ea;text-align:right;vertical-align:top}}th{{text-align:left;background:#263451;color:white}}thead th{{text-align:right}}thead th:first-child{{text-align:left}}small{{display:block;color:#596273}}button{{border:0;border-radius:5px;background:#2358d5;color:white;padding:.55rem;cursor:pointer}}.na,.unavailable{{color:#788190}}.chart-drawer{{position:fixed;inset:8% 5%;z-index:2;background:white;padding:1rem;box-shadow:0 5px 35px #0005;overflow:auto}}.chart-drawer svg{{width:100%;height:auto}}.chart-drawer button{{float:right}}</style></head><body><main>
<nav><a href="/">← Scanner principal</a></nav><h1>Validación técnica multi-ticker</h1><p>Histórico 6M. Los gráficos se generan únicamente al abrir cada activo.</p>
<div class="scroll"><table><thead><tr><th>Ticker</th><th>Precio</th><th>Estado market data</th><th>S1</th><th>Distancia a S1</th><th>Fuerza S1</th><th>Contactos S1</th><th>R1</th><th>Distancia a R1</th><th>Fuerza R1</th><th>Barras</th><th>Estado histórico</th><th>Gráfico</th></tr></thead><tbody>{body}</tbody></table></div>
<script>document.addEventListener('click',async event=>{{const open=event.target.closest('.chart-button');if(open){{const ticker=open.dataset.ticker,drawer=document.querySelector('[data-chart="'+ticker+'"]');document.querySelectorAll('.chart-drawer').forEach(item=>{{if(item!==drawer)item.hidden=true}});if(drawer.hidden){{drawer.hidden=false;drawer.innerHTML='<button type="button" class="chart-close">Cerrar</button><p>Cargando gráfico…</p>';const response=await fetch('/technical-check/chart?ticker='+encodeURIComponent(ticker));drawer.innerHTML='<button type="button" class="chart-close">Cerrar</button>'+(response.ok?await response.text():'<p>Gráfico no disponible.</p>')}}return}}const close=event.target.closest('.chart-close');if(close)close.parentElement.hidden=true}});</script>
</main></body></html>'''.encode()


def render_technical_chart(result: TechnicalCheckResult | None) -> bytes:
    if result is None or result.context is None or not result.context.bars:
        return b'<p role="status">Grafico no disponible.</p>'
    return _svg_chart(result.context).encode()


def ibkr_connection_status(transport: object) -> dict[str, str]:
    """Reduce the read-only auth response to non-sensitive UI information."""
    try:
        data = transport.get("/iserver/auth/status", {})
        ready = isinstance(data, dict) and bool(data.get("authenticated")) and bool(data.get("connected", True))
        if ready:
            return {"state": "connected", "text": "IBKR conectado", "message": "Gateway y sesión disponibles."}
        return {"state": "login", "text": "IBKR requiere login", "message": "Gateway accesible; inicia sesión manualmente."}
    except Exception:
        logger.info("IBKR status check failed", exc_info=True)
        return {"state": "disconnected", "text": "IBKR desconectado", "message": "No se pudo contactar con Client Portal Gateway."}


def _number(value: object, digits: int = 2) -> str:
    if value is None:
        return '<span class="na">N/D</span>'
    return escape(f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def _percent(value: float | None) -> str:
    return '<span class="na">N/D</span>' if value is None else f"{value * 100:.2f} %"


def _candidate_technical_context(candidate) -> str:
    """Render context stored on the candidate, isolated from other tickers."""
    zone = candidate.nearest_support_below
    if zone is None or not candidate.support_position_label:
        return '<span class="na technical-compact">Sin contexto técnico</span>'
    strength = candidate.support_strength or zone.strength
    compact_position = (candidate.support_position_label.replace("Dentro de ", "Dentro ")
                        .replace("Por debajo de ", "Debajo ")
                        .replace("Por encima de ", "Encima ").replace(" y ", "/"))
    relation = {
        StrikePosition.ABOVE_SUPPORT: "sobre",
        StrikePosition.BELOW_SUPPORT: "bajo",
        StrikePosition.INSIDE_SUPPORT: "en",
    }.get(candidate.support_position)
    relevant = f"{relation} {candidate.support_zone_label} {strength}" if relation else strength
    compact = f"{compact_position} · {relevant}"
    distance = "N/D" if candidate.distance_to_support_pct is None else f"{candidate.distance_to_support_pct:+.2f} %"
    sessions = "N/D" if candidate.support_last_contact_sessions is None else str(candidate.support_last_contact_sessions)
    label = candidate.support_zone_label or "soporte"
    accessible = f"Detalle técnico de {candidate.ticker}, strike ${candidate.strike:.2f}"
    return (
        f'<details class="candidate-technical"><summary aria-label="{escape(accessible)}">'
        f'<span class="technical-compact">{escape(compact)}</span></summary><dl>'
        f'<div><dt>Zona</dt><dd>{escape(label)}</dd></div>'
        f'<div><dt>Rango</dt><dd>${zone.lower:.2f}–${zone.upper:.2f}</dd></div>'
        f'<div><dt>Fuerza</dt><dd>{escape(strength)}</dd></div>'
        f'<div><dt>Contactos</dt><dd>{zone.contacts}</dd></div>'
        f'<div><dt>Último contacto</dt><dd>{sessions} sesiones</dd></div>'
        f'<div><dt>Distancia al límite de {escape(label)}</dt><dd>{escape(distance)}</dd></div></dl></details>'
    )


def _strike_context_label(candidate) -> str:
    """Return a compact, descriptive label without changing candidate ranking."""
    label = candidate.support_position_label
    if not label:
        return "Sin soporte"
    return (label.replace("Por encima de S1", "Sobre S1")
            .replace("Por debajo de ", "Bajo ")
            .replace("Dentro de ", "Dentro ")
            .replace("/", "–"))


def _strike_support_explanation(candidate) -> str:
    """Explain the stored strike context deterministically and descriptively."""
    zone = candidate.nearest_support_below
    if zone is None or not candidate.support_zone_label:
        return f"Strike ${candidate.strike:.2f} sin zona de soporte relevante disponible."
    relation = {
        StrikePosition.ABOVE_SUPPORT: "por encima de",
        StrikePosition.INSIDE_SUPPORT: "dentro de",
        StrikePosition.BELOW_SUPPORT: "por debajo de",
    }.get(candidate.support_position, "respecto a")
    strength = (candidate.support_strength or zone.strength).lower()
    contacts = f", {zone.contacts} contactos" if zone.contacts is not None else ""
    return (f"Strike ${candidate.strike:.2f} situado {relation} {candidate.support_zone_label} "
            f"(${zone.lower:.2f}–${zone.upper:.2f}). "
            f"{candidate.support_zone_label} {strength}{contacts}.")


def _rows(result: ScanResult | None) -> str:
    if result is None:
        return ""
    if not result.candidates:
        return '<tr><td colspan="21" class="empty">No hay candidatos completos para estos filtros.</td></tr>'
    rendered = []
    for c in result.candidates:
        availability = escape(str(c.market_data_availability))
        if "Frozen" in str(c.market_data_availability):
            availability = f'<span class="market-state frozen">{availability}</span><small class="market-note">Cotización congelada / última disponible</small>'
        cells = (
            _number(c.ticker), _number(c.expiration.isoformat()), _number(c.dte), _number(c.strike, 4),
            f"${c.underlying_price:,.2f}", _percent(c.safety_margin), _number(c.bid, 4),
            _number(c.ask, 4), _number(c.mid, 4), _number(c.delta, 4), _number(c.gamma, 4),
            _number(c.contract_theta, 4), _number(c.short_theta, 4), _number(c.theta_decay_pct_per_day, 2),
            _number(c.vega, 4), _percent(c.implied_volatility),
            _number(c.open_interest), availability, _percent(c.premium_yield),
            _percent(c.annualized_premium_yield),
            _candidate_technical_context(c),
        )
        rendered.append("<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>")
    return "".join(rendered)


def _summary(result: ScanResult | None) -> str:
    if result is None:
        return ""
    s = result.summary
    phase = s.phase_seconds
    items = (
        ("Considerados (evaluados con market data/filtros)", s.considered), ("Completos", s.complete),
        ("Rechazados por distancia al strike", s.rejected_margin), ("Rechazados por delta", s.rejected_delta),
        ("Tiempo total", f"{result.elapsed_seconds:.3f} s"),
    )
    technical = (
        ("Incompletos", s.incomplete),
        ("Contratos objetivo", s.target_contracts), ("Resueltos", s.resolved_contracts),
        ("Fallidos", s.failed_contracts), ("No resueltos por timeout", s.unresolved_contracts_timeout),
        ("Timeout", "Sí" if s.timed_out else "No"), ("Fase", s.timeout_phase or "—"),
        ("Resolución contractual", f"{phase.get('contract_resolution', 0):.3f} s"),
        ("Market data", f"{phase.get('market_data_snapshots', 0):.3f} s"),
        ("Strikes candidatos", s.candidate_strikes), ("Llamadas secdef/info", s.secdef_info_calls),
        ("Cache hits", s.contract_cache_hits), ("Deduplicados", s.deduplicated_contracts),
        ("Validaciones correctas", s.contract_validations_succeeded),
        ("Validaciones fallidas", s.contract_validations_failed),
        ("Latencia secdef/info media/p50/p95", f"{s.secdef_info_latency_mean_ms:.1f}/{s.secdef_info_latency_p50_ms:.1f}/{s.secdef_info_latency_p95_ms:.1f} ms"),
        ("Concurrencia máxima observada", s.max_concurrent_contract_requests),
        ("Resolución subyacente", f"{phase.get('underlying_resolution', 0):.3f} s"),
        ("Histórico", f"{phase.get('historical_data', 0):.3f} s"),
        ("Análisis técnico", f"{phase.get('technical_analysis', 0):.3f} s"),
        ("HTTP por endpoint", ", ".join(f"{name}={count}" for name, count in sorted(s.http_calls.items())) or "ninguna"),
    )
    cards = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(value))}</dd></div>" for label, value in items
    )
    details = "".join(f"<div><dt>{escape(label)}</dt><dd>{escape(str(value))}</dd></div>" for label, value in technical)
    return f'<section class="summary"><h2>Resumen del scan</h2><dl>{cards}</dl><details><summary>Detalles técnicos</summary><dl>{details}</dl></details></section>'


def _interpretation(result: ScanResult | None) -> str:
    """Turn scan counters into short, non-prescriptive user-facing messages."""
    if result is None:
        return ""

    summary = result.summary
    candidate_count = len(result.candidates)
    messages: list[tuple[str, str]] = []
    details: list[str] = []

    if summary.timed_out:
        noun = "candidato" if candidate_count == 1 else "candidatos"
        verb = "encontrado" if candidate_count == 1 else "encontrados"
        not_evaluated = max(0, summary.target_contracts - summary.considered)
        messages.append((
            "warning",
            f"{candidate_count} {noun} {verb} entre {summary.considered} contratos evaluados. "
            f"{not_evaluated} contratos objetivo no llegaron a evaluarse.",
        ))
    elif candidate_count:
        messages.extend((
            ("success", f"Se encontraron {candidate_count} candidatos que cumplen todos los filtros actuales."),
            ("neutral", "Ordenados por rentabilidad anualizada de la prima."),
        ))
        inside = [c for c in result.candidates if (c.support_position_label or "").startswith("Dentro de ")]
        if inside:
            messages.append(("neutral", f"{len(inside)} candidatos tienen strikes dentro de zonas de soporte activas."))
    else:
        messages.append(("neutral", "No se encontraron candidatos que cumplan todos los filtros."))

    if summary.timed_out:
        messages.append((
            "warning",
            "El scan terminó con resultados parciales porque no se completaron todas las fases "
            "dentro del tiempo disponible.",
        ))
    if result.market_data_status and "Frozen" in result.market_data_status:
        messages.append((
            "warning",
            "Los datos de mercado están congelados/última cotización disponible. "
            "Los resultados pueden cambiar cuando el mercado esté activo.",
        ))

    reasons = (
        (summary.rejected_delta, "contratos quedaron fuera del rango de delta configurado."),
        (summary.rejected_margin, "contratos fueron descartados por no alcanzar la distancia mínima al strike."),
        (summary.incomplete, "contratos no pudieron evaluarse completamente por falta de bid, ask o delta."),
    )
    reason_messages = [f"{count} {message}" for count, message in reasons if count]
    if not candidate_count:
        available_slots = max(0, 4 - len(messages))
        messages.extend(("neutral", message) for message in reason_messages[:available_slots])
        messages.append((
            "neutral",
            "Puedes revisar los filtros de delta, DTE o distancia al strike si quieres ampliar el universo analizado.",
        ))
    details.extend(reason_messages)
    if summary.timed_out:
        details.extend((
            f"Contratos objetivo: {summary.target_contracts}.",
            f"Contratos resueltos: {summary.resolved_contracts}.",
            f"Contratos que llegaron a market data/filtros: {summary.considered}.",
            f"Candidatos completos: {candidate_count}.",
            f"No resueltos por timeout: {summary.unresolved_contracts_timeout}.",
        ))

    visible = "".join(
        f'<p class="interpretation-message {css_class}">{escape(message)}</p>'
        for css_class, message in messages[:5]
    )
    analysis = "".join(f"<li>{escape(message)}</li>" for message in details)
    if not analysis:
        analysis = "<li>No se registraron descartes ni contratos pendientes.</li>"
    discarded = (
        ("Distancia al strike", summary.rejected_margin), ("Delta", summary.rejected_delta),
        ("Datos incompletos", summary.incomplete),
        ("Timeout", summary.unresolved_contracts_timeout),
    )
    discarded_rows = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{count}</dd></div>" for label, count in discarded
    )
    return (
        '<section class="interpretation" aria-labelledby="interpretation-title">'
        '<h2 id="interpretation-title">Interpretación del resultado</h2>'
        f'<div class="interpretation-visible">{visible}</div>'
        '<details><summary>Ver detalles del análisis</summary>'
        f'<ul>{analysis}</ul></details>'
        '<details><summary>Contratos descartados</summary>'
        f'<dl>{discarded_rows}</dl></details></section>'
    )


def _result_heading(result: ScanResult | None, ticker: str) -> str:
    if result is None:
        return ""
    price = '<span class="na">N/D</span>' if result.underlying_price is None else f"${result.underlying_price:,.2f}"
    status = ""
    if result.market_data_status:
        state = result.market_data_status
        css_state = "frozen" if "Frozen" in state else "standard"
        explanation = '<small class="market-note">Cotización congelada / última disponible</small>' if "Frozen" in state else ""
        status = f' · <span class="market-state {css_state}">{escape(state)}</span>{explanation}'
    updated = result.updated_at.strftime("%H:%M:%S UTC") if result.updated_at else "N/D"
    simulated = " · Precio simulado" if result.simulated else ""
    return f'<div class="result-head"><strong>{escape(ticker.upper())} &nbsp; {price}</strong><span>{status} · Actualizado {updated}{simulated}</span></div>'


def _technical_chart(result: ScanResult | None, *, lazy: bool = False) -> str:
    context = result.technical_context if result else None
    if result is None:
        return ""
    period = context.period.value if context else result.summary.historical_period
    period_labels = {"3m": "3M", "6m": "6M", "1y": "1A"}
    current = context.current_price if context else result.underlying_price
    supports = context.supports_below_price[:3] if context else ()
    resistances = context.resistances_above_price[:2] if context else ()

    def metric(label, zone):
        if zone is None or current is None:
            rendered = '<span class="na">N/D</span>'
        else:
            distance = (zone.center-current)/current*100
            rendered = (f'<strong>${zone.lower:.2f}–${zone.upper:.2f}</strong>'
                        f'<span class="zone-strength">{zone.strength.capitalize()}</span>'
                        f'<span>{distance:+.2f} %</span>')
        return f'<div><dt>{label}</dt><dd>{rendered}</dd></div>'

    last_session = context.bars[-1].session.isoformat() if context and context.bars else "N/D"
    identity = "technical-" + "".join(ch for ch in (context.symbol if context else "result") if ch.isalnum()).lower()
    # Keep the five canonical slots visible even when the analysis found fewer
    # zones.  This is presentation only: the stored context remains untouched.
    zone_cards = "".join(metric(f"S{i + 1}", supports[i] if i < len(supports) else None)
                         for i in range(3))
    zone_cards += "".join(metric(f"R{i + 1}", resistances[i] if i < len(resistances) else None)
                          for i in range(2))
    summary = (
        '<div class="technical-title"><h2>Contexto técnico</h2></div><dl class="technical-metrics">'
        f'<div><dt>Precio actual</dt><dd>{f"${current:.2f}" if current is not None else "N/D"}</dd></div>'
        f'{zone_cards}<div><dt>Periodo</dt><dd>{period_labels.get(period, period)}</dd></div>'
        f'<div><dt>Última sesión disponible</dt><dd>{last_session}</dd></div></dl>'
    )
    if context is None or not context.bars:
        return f'<section class="technical">{summary}<div class="history-unavailable" role="status"><strong>Histórico no disponible</strong><p>IBKR no devolvió barras históricas utilizables. El scan de opciones no se ha visto afectado.</p></div></section>'

    if lazy:
        ticker = escape(context.symbol)
        return (f'<section class="technical" data-ticker="{ticker}">{summary}'
                f'<details class="lazy-chart" data-chart-url="/scan-chart?ticker={ticker}">'
                '<summary><span aria-hidden="true">▥</span> Ver gráfico</summary>'
                '<div class="chart-panel" role="status">El gráfico se cargará al abrir.</div>'
                '</details></section>')

    bars = context.bars
    visible = tuple(supports) + tuple(resistances)
    width, height, left, right, top, bottom = 1000, 400, 62, 18, 24, 42
    prices = [value for b in bars for value in (b.low, b.high)] + [context.current_price]
    for zone in visible:
        prices.extend((zone.lower, zone.upper))
    low, high = min(prices), max(prices)
    margin = max((high-low)*.04, context.current_price*.002)
    low, high = low-margin, high+margin
    span = max(high-low, 1e-9)
    x = lambda i: left+i*(width-left-right)/max(1, len(bars)-1)
    y = lambda price: top+(high-price)*(height-top-bottom)/span

    zone_svg = []
    for i, zone in enumerate(supports, 1):
        alpha = "strong" if zone.strength == "fuerte" else "medium" if zone.strength == "media" else "weak"
        zone_svg.append(f'<rect class="zone support {alpha}" x="{left}" y="{y(zone.upper):.1f}" width="{width-left-right}" height="{max(2,y(zone.lower)-y(zone.upper)):.1f}"/><text class="zone-label support-label" x="{left+8}" y="{y(zone.center)+4:.1f}">S{i}</text>')
    for i, zone in enumerate(resistances, 1):
        alpha = "strong" if zone.strength == "fuerte" else "medium" if zone.strength == "media" else "weak"
        zone_svg.append(f'<rect class="zone resistance {alpha}" x="{left}" y="{y(zone.upper):.1f}" width="{width-left-right}" height="{max(2,y(zone.lower)-y(zone.upper)):.1f}"/><text class="zone-label resistance-label" x="{left+8}" y="{y(zone.center)+4:.1f}">R{i}</text>')
    y_ticks = []
    for i in range(5):
        price = low+(high-low)*i/4
        yy = y(price)
        y_ticks.append(f'<line class="grid" x1="{left}" x2="{width-right}" y1="{yy:.1f}" y2="{yy:.1f}"/><text class="axis-label" x="{left-7}" y="{yy+4:.1f}" text-anchor="end">${price:.2f}</text>')
    date_ticks = []
    for index in sorted({0, (len(bars)-1)//2, len(bars)-1}):
        xx=x(index)
        date_ticks.append(f'<line class="axis-tick" x1="{xx:.1f}" x2="{xx:.1f}" y1="{height-bottom}" y2="{height-bottom+5}"/><text class="axis-label" x="{xx:.1f}" y="{height-15}" text-anchor="middle">{bars[index].session.strftime("%d %b %Y")}</text>')
    path = " ".join(("M" if i == 0 else "L")+f"{x(i):.1f},{y(b.close):.1f}" for i,b in enumerate(bars))
    strike_lines = "".join(f'<line class="strike" x1="{left}" x2="{width-right}" y1="{y(item.strike):.1f}" y2="{y(item.strike):.1f}"><title>Strike ${item.strike:.2f}</title></line>' for item in context.strikes if low <= item.strike <= high)
    current_y = y(context.current_price)
    current_line = f'<line class="current" x1="{left}" x2="{width-right}" y1="{current_y:.1f}" y2="{current_y:.1f}"/><text class="current-label" x="{width-right-5}" y="{current_y-6:.1f}" text-anchor="end">Precio actual ${context.current_price:.2f}</text>'

    rows = []
    for label, zone in [(f"S{i}", z) for i,z in enumerate(supports,1)] + [(f"R{i}", z) for i,z in enumerate(resistances,1)]:
        distance=(zone.center-context.current_price)/context.current_price*100
        sessions=sum(1 for bar in bars if bar.session > zone.last_contact)
        rows.append(f'<tr><th scope="row">{label}</th><td>${zone.lower:.2f}–${zone.upper:.2f}</td><td>{distance:+.2f} %</td><td>{zone.strength.capitalize()}</td><td>{zone.contacts}</td><td>{sessions} sesiones</td></tr>')
    explanation = '<div class="zone-table-wrap"><table class="zone-table"><thead><tr><th>Zona</th><th>Rango</th><th>Distancia</th><th>Fuerza</th><th>Contactos</th><th>Último contacto</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
    strike_messages="".join(f'<li>Strike ${item.strike:.2f}: {item.position_label or "sin soporte activo"}.</li>' for item in context.strikes)
    buttons = "".join(f'<button type="button" class="period-button{" active" if value == period else ""}" data-period="{value}" aria-pressed="{"true" if value == period else "false"}">{label}</button>' for value,label in period_labels.items())
    svg = f'<svg role="img" aria-label="Gráfico histórico diario con S1 S2 S3 R1 R2, ejes, precio actual y strikes" viewBox="0 0 {width} {height}">{"".join(y_ticks)}{"".join(zone_svg)}<path class="price" d="{path}"/>{current_line}{strike_lines}{"".join(date_ticks)}</svg>'
    return f'<section class="technical" data-ticker="{escape(context.symbol)}">{summary}<details id="{identity}"><summary><span aria-hidden="true">▥</span> Ver gráfico</summary><div class="chart-panel"><div class="period-selector" aria-label="Periodo histórico">{buttons}</div>{svg}{explanation}<div class="technical-context"><ul>{strike_messages}</ul><p class="disclaimer">Las zonas se derivan del comportamiento histórico del precio y no garantizan reacciones futuras. Son contexto informativo y no constituyen una recomendación de inversión.</p></div></div></details></section>'


def _multi_screener(items: tuple[tuple[str, ScanResult | None, str | None], ...],
                    metrics: MultiScanMetrics | None = None) -> str:
    """Render comparison only; candidate ranking remains isolated per ticker."""
    rows = []
    with_candidates = errors = 0
    elapsed = 0.0
    sortable = {0: "text", 1: "number", 4: "number", 6: "number", 7: "number", 8: "number", 9: "number", 10: "number",
                11: "number", 12: "number", 13: "number", 14: "number", 15: "number", 16: "number"}
    def badge(state: str | None) -> str:
        value = state or "N/D"
        lowered = value.lower()
        kind = "frozen" if "frozen" in lowered else "delayed" if "delayed" in lowered else "realtime" if "realtime" in lowered else "na"
        short = "Frozen" if kind == "frozen" else "Delayed" if kind == "delayed" else "RealTime" if kind == "realtime" else "N/D"
        return f'<span class="status-badge {kind}" title="{escape(value)}">{short}</span>'
    for ticker, result, item_error in items:
        if result is None:
            errors += 1
            cells = ((ticker, ticker), ("N/D", ""), (badge(None), "")) + tuple(("N/D", "") for _ in range(15))
            detail = f'<div class="row-error" role="status">{escape(item_error or "No se pudo completar este ticker.")}</div>'
            row_class = "error-result"
        else:
            elapsed += result.elapsed_seconds
            context = result.technical_context
            support = context.supports_below_price[0] if context and context.supports_below_price else None
            distance_s = distance_to_zone_percent(result.underlying_price, support) if result.underlying_price else None
            best = result.candidates[0] if result.candidates else None
            with_candidates += bool(best)
            cells = (
                (ticker, ticker),
                (f"${result.underlying_price:,.2f}" if result.underlying_price is not None else "N/D", result.underlying_price),
                (badge(result.market_data_status), ""),
                (f"${support.lower:.2f}–${support.upper:.2f}" if support else "N/D", support.center if support else ""),
                ("Dentro S1" if distance_s == 0 else f"{distance_s:+.2f} %" if distance_s is not None else "N/D", distance_s),
                (support.strength.capitalize() if support else "N/D", support.strength if support else ""),
                (str(len(result.candidates)), len(result.candidates)),
                (f"${best.strike:.2f}" if best else "N/D", best.strike if best else ""),
                (str(best.dte) if best else "N/D", best.dte if best else ""),
                (_percent(best.safety_margin) if best else "N/D", best.safety_margin if best else ""),
                (f"{abs(best.delta):.4f}" if best and best.delta is not None else "N/D", abs(best.delta) if best and best.delta is not None else ""),
                (_number(best.short_theta, 4) if best else "N/D", best.short_theta if best else ""),
                (_number(best.theta_decay_pct_per_day, 2) if best else "N/D", best.theta_decay_pct_per_day if best else ""),
                (_percent(best.implied_volatility) if best else "N/D", best.implied_volatility if best else ""),
                (f"{best.premium_yield*100:.2f} %" if best and best.premium_yield is not None else "N/D", best.premium_yield if best and best.premium_yield is not None else ""),
                (f"{best.annualized_premium_yield*100:.2f} %" if best and best.annualized_premium_yield is not None else "N/D", best.annualized_premium_yield if best and best.annualized_premium_yield is not None else ""),
                (_number(best.open_interest) if best else "N/D", best.open_interest if best else ""),
                (_strike_context_label(best) if best else "N/D", _strike_context_label(best) if best else ""),
            )
            detail = (_result_heading(result, ticker) + _technical_chart(result, lazy=True) + _interpretation(result) +
                      (f'<p class="strike-explanation">{escape(_strike_support_explanation(best))}</p>' if best else '') +
                      '<h3>Candidatos PUT completos</h3><div class="scroll"><table class="candidate-table"><thead><tr>' +
                      ''.join(f'<th>{h}</th>' for h in ('Ticker','Expiration','DTE','Strike','Underlying','Distancia al strike','Bid','Ask','Mid','Delta','Gamma','Contract theta','Theta short','Theta %/día','Vega','IV','Open interest','6509','Premium yield','Annualized yield','Contexto técnico')) +
                      f'</tr></thead><tbody>{_rows(result)}</tbody></table></div>{_summary(result)}')
            relation_class = ({StrikePosition.ABOVE_SUPPORT: " strike-above",
                               StrikePosition.INSIDE_SUPPORT: " strike-inside",
                               StrikePosition.BELOW_SUPPORT: " strike-below"}
                              .get(best.support_position, "") if best else "")
            row_class = ("has-candidates" + relation_class) if best else "no-candidates"
        rendered = ''.join(f'<td data-sort-value="{escape(str(sort_value if sort_value is not None else ""))}">{value}</td>' for value, sort_value in cells)
        rows.append(f'<tr data-ticker="{escape(ticker)}" class="{row_class}">{rendered}<td><details class="ticker-detail"><summary>Ver detalle</summary><div class="detail-panel"><button type="button" class="detail-close" aria-label="Cerrar detalle">Cerrar</button>{detail}</div></details></td></tr>')
    headings = ('Ticker','Precio','Estado','S1','Distancia S1','Fuerza S1','Candidatos',
                'Strike','DTE','Distancia al strike','Delta','Theta short','Theta %/día','IV',
                'Premium yield','Annualized yield','Open interest','Contexto técnico del strike')
    headers = []
    for i, heading in enumerate(headings):
        content = heading
        if i in sortable:
            content = (f'<button type="button" class="sort-button" data-column="{i}" '
                       f'data-kind="{sortable[i]}">{heading} '
                       '<span aria-hidden="true">↕</span></button>')
        headers.append(f'<th>{content}</th>')
    rendered_headers = ''.join(headers)
    total = len(items)
    timing = (f' · total {metrics.elapsed_seconds:.1f} s · ticker p50/p95 '
              f'{metrics.ticker_seconds_p50:.1f}/{metrics.ticker_seconds_p95:.1f} s'
              if metrics else f' · {elapsed:.1f} s')
    return ('<section class="screener" aria-labelledby="screener-title"><h2 id="screener-title">Screener multi-ticker</h2>'
            f'<div class="scan-summary" role="status">{total} tickers · {with_candidates} con candidatos · {total-with_candidates-errors} sin candidatos{timing} · {errors} error</div>'
            '<div class="quick-filters" role="group" aria-label="Filtros rápidos"><button type="button" class="active" data-filter="all">Todos</button><button type="button" data-filter="has-candidates">Con candidatos</button><button type="button" data-filter="no-candidates">Sin candidatos</button><button type="button" data-filter="strong">Soporte fuerte</button><button type="button" data-filter="near">Cerca de S1</button><button type="button" data-filter="strike-above">Strike sobre soporte</button><button type="button" data-filter="strike-inside">Strike dentro soporte</button><button type="button" data-filter="strike-below">Strike bajo soporte</button></div>'
            '<div class="scroll"><table class="screener-table"><thead><tr>' + rendered_headers + '<th>Acción</th>' +
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></section>')


def render_page(values: dict[str, str] | None = None, result: ScanResult | None = None, error: str | None = None,
                multi_results: tuple[tuple[str, ScanResult | None, str | None], ...] = (),
                multi_metrics: MultiScanMetrics | None = None,
                watchlists: dict[str, Watchlist] | None = None,
                watchlist_message: str | None = None) -> bytes:
    v = {"ticker": "NVDA", "min_dte": "30", "max_dte": "45", "min_safety_margin": "20",
         "min_abs_delta": "0.15", "max_abs_delta": "0.30", "mode": "fake", "historical_period":"6m",
         "universe_source": "manual", "min_iv": "", "min_short_theta": ""}
    if values:
        v.update(values)
    checked = " checked" if v["mode"] == "fake" else ""
    source_options = [('manual', 'Entrada manual')]
    source_options.extend((f'group:{key}', label) for key, (label, _) in PREDEFINED_UNIVERSES.items())
    source_options.extend((f'watchlist:{key}', f'Watchlist: {item.name}') for key, item in (watchlists or {}).items())
    universe_options = ''.join(
        f'<option value="{escape(key)}"{" selected" if v["universe_source"] == key else ""}>{escape(label)}</option>'
        for key, label in source_options
    )
    alert = f'<div class="error" role="alert">{escape(error)}</div>' if error else ""
    notice = f'<div class="completion" role="status">{escape(watchlist_message)}</div>' if watchlist_message else ""
    watchlist_rows = "".join(
        f'''<form method="post" class="watchlist-row"><input type="hidden" name="watchlist_id" value="{escape(item.id)}">
        <label>Nombre<input name="watchlist_name" value="{escape(item.name)}" required></label>
        <label>Tickers<input name="watchlist_tickers" value="{escape(', '.join(item.symbols))}" required></label>
        <button name="action" value="watchlist_update" type="submit">Guardar</button>
        <button name="action" value="watchlist_delete" type="submit" class="danger">Eliminar</button></form>'''
        for item in (watchlists or {}).values()
    )
    table = _multi_screener(multi_results, multi_metrics) if multi_results else ("" if result is None else f'''<section>{_result_heading(result, v['ticker'])}{_technical_chart(result)}{_interpretation(result)}<h2>Candidatos completos</h2><div class="scroll"><table><thead><tr>{''.join(f'<th>{h}</th>' for h in ('Ticker','Expiration','DTE','Strike','Underlying','Distancia al strike','Bid','Ask','Mid','Delta','Gamma','Contract theta','Theta short','Theta %/día','Vega','IV','Open interest','6509','Premium yield','Annualized yield','Contexto técnico'))}</tr></thead><tbody>{_rows(result)}</tbody></table></div></section>''')
    def help_icon(identifier: str, title: str, explanation: str) -> str:
        """Return an accessible, layout-independent educational tooltip."""
        return (f'<span class="filter-help"><button class="help-trigger" type="button" '
                f'aria-label="Ayuda sobre {escape(title)}" aria-describedby="{identifier}">ⓘ</button>'
                f'<span class="help-tooltip" id="{identifier}" role="tooltip">'
                f'<strong>{escape(title)}</strong><span>{escape(explanation)}</span></span></span>')

    dte_help = "Días restantes hasta el vencimiento de la opción."
    distance_help = ("Distancia porcentual entre el precio actual del underlying y el strike de la PUT. "
                     "Mayor distancia proporciona mayor colchón, normalmente a cambio de menor prima.")
    delta_help = ("La PUT tiene Delta contractual negativo, pero el scanner filtra su valor absoluto. "
                  "Menor |Delta| suele significar un strike más OTM, menor exposición direccional y "
                  "normalmente menor prima. Para Short PUT, 0,15–0,30 es el rango base actualmente "
                  "utilizado. Puede orientar sobre el riesgo de terminar ITM, pero no es una probabilidad exacta.")
    iv_help = ("Volatilidad implícita del contrato. Una IV mayor suele implicar mayor prima, pero también "
               "mayor incertidumbre esperada. Una IV alta no significa automáticamente mejor oportunidad.")
    theta_help = ("Exposición temporal de la posición Short PUT. Se calcula invirtiendo el signo del theta "
                  "contractual, sin usar abs(). Ejemplo: contract theta −0,135 → short theta +0,135. Un valor "
                  "positivo representa deterioro temporal teóricamente favorable al vendedor, ceteris paribus; "
                  "no representa beneficio diario garantizado.")
    html = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Options Scanner</title><style>
body{{font:15px system-ui;margin:0;background:#f4f6fa;color:#182033}}main{{max-width:1500px;margin:auto;padding:2rem}}h1{{margin:0}}.note{{color:#556}}.top{{display:flex;justify-content:space-between;gap:1rem;align-items:start}}.connection{{background:white;padding:.7rem;border-radius:8px;min-width:220px}}.dot{{display:inline-block;width:.75rem;height:.75rem;border-radius:50%;background:#818895;margin-right:.4rem}}.connected .dot{{background:#198754}}.login .dot{{background:#e58a00}}.disconnected .dot{{background:#c52d36}}.demo .dot{{background:#818895}}.connection button{{font-size:.8rem;padding:.35rem .6rem;margin-top:.4rem}}.connection small{{display:block;color:#596273;margin-top:.25rem}}
form{{display:flex;flex-wrap:wrap;gap:1rem;align-items:end;background:white;padding:1.25rem;border-radius:10px;box-shadow:0 2px 8px #0001}}label{{display:grid;gap:.35rem;font-weight:600}}input{{padding:.55rem;border:1px solid #aab3c5;border-radius:5px;width:9rem}}button{{background:#2358d5;color:white;border:0;border-radius:5px;padding:.7rem 1.4rem;font-weight:700;cursor:pointer}}button:disabled{{cursor:not-allowed;opacity:.65}}button.danger{{background:#a52832}}.mode{{display:flex;align-items:center;gap:.4rem}}.mode input{{width:auto}}.watchlists{{background:white;padding:1rem;border-radius:10px}}.watchlist-row{{box-shadow:none;border-top:1px solid #dde2ea;border-radius:0;padding:.8rem 0}}.watchlist-row input[name="watchlist_tickers"]{{width:20rem}}
.interpretation{{background:white;padding:1rem 1.2rem;border-radius:8px;border-left:4px solid #60708c;margin-top:1rem}}.interpretation h2{{margin-top:0}}.interpretation-message{{margin:.45rem 0;padding:.45rem .65rem;border-radius:4px}}.interpretation-message.success{{background:#e9f7ef;border-left:3px solid #198754}}.interpretation-message.neutral{{background:#eef3fb;border-left:3px solid #60708c}}.interpretation-message.warning{{background:#fff7db;border-left:3px solid #d18a00}}.interpretation-message.error{{background:#fff0f0;border-left:3px solid #c22}}.interpretation ul{{margin-bottom:0}}.interpretation dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.6rem}}.interpretation dl div{{background:#f4f6fa;padding:.65rem;border-radius:5px}}
.technical{{background:white;padding:1rem;border-radius:8px}}.technical-title h2{{margin:0 0 .8rem}}.technical-metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.6rem;margin:0}}.technical-metrics>div{{background:#f4f6fa;padding:.65rem;border-radius:5px}}.technical details{{border-top:1px solid #dde2ea;padding-top:.7rem}}.technical details>summary{{color:#2358d5;width:max-content}}.chart-panel{{margin-top:.8rem}}.period-selector{{display:flex;gap:.35rem;margin-bottom:.6rem}}.period-button{{padding:.4rem .7rem;background:#e7ebf3;color:#263451}}.period-button.active{{background:#2358d5;color:white}}.technical svg{{width:100%;height:360px;background:#fafbfd;border:1px solid #dce2ec}}.price{{fill:none;stroke:#254fbd;stroke-width:2}}.zone.support{{fill:#2ca66f}}.zone.resistance{{fill:#db5a55}}.zone.weak{{opacity:.10}}.zone.medium{{opacity:.18}}.zone.strong{{opacity:.27}}.zone-label{{font-weight:800;font-size:14px}}.support-label{{fill:#176b48}}.resistance-label{{fill:#9b302c}}.grid{{stroke:#dfe4ec;stroke-width:1}}.axis-tick{{stroke:#778196}}.axis-label{{fill:#596273;font-size:11px}}.current-label{{fill:#182033;font-size:12px;font-weight:700}}.current{{stroke:#182033;stroke-width:1.5;stroke-dasharray:7 4}}.strike{{stroke:#8b55bb;stroke-width:1;stroke-dasharray:3 4}}.zone-strength{{display:block;font-size:.8rem;font-weight:500;text-transform:capitalize}}.zone-table-wrap{{overflow:auto;margin-top:.8rem}}.zone-table{{font-size:.9rem}}.zone-table th{{background:#eef1f6;color:#263451}}.technical-context{{background:#f6f8fb;padding:.8rem 1rem;margin-top:.7rem}}.history-unavailable{{background:#fff7db;border-left:4px solid #d18a00;padding:.75rem;margin-top:.8rem}}.history-unavailable p{{margin:.3rem 0 0}}.disclaimer{{color:#596273;font-size:.9rem}}
.candidate-technical{{margin:0;min-width:10rem;text-align:left}}.candidate-technical summary{{white-space:nowrap}}.candidate-technical dl{{display:grid;grid-template-columns:repeat(2,minmax(7rem,1fr));gap:.35rem;margin:.6rem 0 0}}.candidate-technical dl div{{white-space:normal;background:#f4f6fa;padding:.35rem}}.candidate-technical dd{{font-size:.9rem}}.technical-compact{{font-weight:650}}
.scan-status{{display:flex;align-items:center;gap:.8rem;margin:1rem 0;padding:1rem;background:#eaf1ff;border-left:4px solid #2358d5;border-radius:5px}}.scan-status[hidden]{{display:none}}.scan-status strong,.scan-status span{{display:block}}.scan-legend{{font-size:.8rem;color:#596273}}.spinner{{width:1.25rem;height:1.25rem;border:3px solid #b9c9ed;border-top-color:#2358d5;border-radius:50%;animation:spin .8s linear infinite;flex:none}}@keyframes spin{{to{{transform:rotate(360deg)}}}}.completion{{margin:1rem 0;padding:.8rem;background:#e9f7ef;border-left:4px solid #198754}}.error,.row-error{{margin:1rem 0;padding:1rem;background:#fff0f0;border-left:4px solid #c22}}.demo-label{{background:#eceff3;padding:.65rem;border-left:4px solid #818895;font-weight:700}}section{{margin-top:1.5rem}}.screener table{{font-size:.86rem}}.ticker-detail{{margin:0;text-align:left}}.detail-panel{{position:fixed;inset:5%;z-index:3;background:#f4f6fa;padding:1.2rem;box-shadow:0 8px 40px #0005;overflow:auto;white-space:normal}}.candidate-table{{font-size:.82rem}}.result-head{{background:white;padding:1rem;border-radius:8px;display:flex;gap:1rem;align-items:baseline;flex-wrap:wrap}}.result-head strong{{font-size:1.7rem}}.market-state{{font-weight:700}}.market-state.frozen{{color:#6b5200;background:#fff2bd;border-radius:4px;padding:.15rem .35rem}}.market-note{{display:block;color:#665b38;font-weight:400;white-space:normal}}.scroll{{overflow:auto}}table{{border-collapse:collapse;background:white;width:100%;white-space:nowrap}}th,td{{padding:.65rem;border-bottom:1px solid #dde2ea;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#263451;color:white}}.na,.empty{{color:#788190;font-style:italic}}.summary dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.75rem}}.summary dl div{{background:white;padding:.8rem;border-radius:7px}}dt{{color:#596273}}dd{{font-size:1.15rem;font-weight:700;margin:.25rem 0 0}}details{{margin-top:1rem}}summary{{cursor:pointer;font-weight:700}}
/* Compact screener overrides */
main{{max-width:1700px;padding:1rem}}h1{{font-size:1.45rem}}form#scan-form{{display:block;padding:.75rem}}.filter-groups{{display:grid;grid-template-columns:minmax(270px,1.25fr) minmax(230px,1fr) minmax(390px,1.7fr) minmax(130px,.55fr);gap:.65rem;align-items:stretch}}.filter-group{{min-width:0;margin:0;padding:.55rem .65rem .65rem;border:1px solid #dce2ec;border-radius:7px;background:#fafbfd}}.filter-group legend{{padding:0 .3rem;color:#33415c;font-size:.75rem;font-weight:800;letter-spacing:.055em;text-transform:uppercase}}.filter-controls{{display:flex;gap:.5rem;align-items:flex-end;height:100%}}form#scan-form label.control{{display:flex;min-width:0;flex:1;flex-direction:column;gap:.18rem;font-size:.78rem}}.control-label{{display:flex;min-height:2.1em;align-items:flex-end;gap:.25rem;line-height:1.05}}form#scan-form input,form#scan-form select{{box-sizing:border-box;width:100%;min-width:0;height:2.15rem;padding:.4rem;background:white}}.filter-help{{position:relative;display:inline-flex;align-items:center}}.help-trigger{{width:1.35rem;height:1.35rem;padding:0;border-radius:50%;background:transparent;color:#2358d5;font-size:.95rem;line-height:1}}.help-trigger:focus-visible{{outline:2px solid #2358d5;outline-offset:2px}}.help-tooltip{{position:absolute;left:0;bottom:calc(100% + .45rem);z-index:10;width:min(19rem,calc(100vw - 2rem));padding:.65rem .75rem;border-radius:6px;background:#182033;color:white;box-shadow:0 5px 18px #0004;font-size:.76rem;font-weight:400;line-height:1.35;visibility:hidden;opacity:0;pointer-events:none;transition:opacity .12s}}.help-tooltip strong,.help-tooltip span{{display:block}}.help-tooltip strong{{margin-bottom:.25rem}}.filter-help:hover .help-tooltip,.filter-help:focus-within .help-tooltip{{visibility:visible;opacity:1}}.filter-reference{{margin:.55rem 0 0}}.filter-reference>summary{{width:max-content;max-width:100%;color:#2358d5;font-size:.82rem;list-style:none}}.filter-reference>summary::-webkit-details-marker{{display:none}}.reference-card{{max-width:760px;margin-top:.45rem;padding:.7rem;background:#f8fafc;border:1px solid #dce2ec;border-radius:7px;overflow:auto}}.reference-card table{{font-size:.78rem;white-space:normal}}.reference-card th,.reference-card td{{padding:.4rem;text-align:left;vertical-align:top}}.universe-group .universe-source{{flex:.85}}.universe-group .ticker-control{{flex:1.4}}.field-help{{overflow:hidden;height:1.1em;color:#687386;font-size:.68rem;font-weight:400;line-height:1.1;white-space:nowrap;text-overflow:ellipsis}}.form-actions{{display:flex;align-items:center;gap:.6rem;margin-top:.7rem;padding-top:.7rem;border-top:1px solid #dce2ec}}.form-actions .mode{{margin-right:auto;font-size:.82rem}}.form-actions button{{padding:.55rem 1rem}}.secondary-action{{background:#566174}}.watchlists{{padding:.55rem .7rem}}.watchlists>h2,.watchlists>p{{display:none}}section{{margin-top:.8rem}}.screener h2{{font-size:1.15rem;margin:.2rem 0}}.scan-summary{{display:inline-block;background:#e8eef8;padding:.38rem .65rem;border-radius:5px;font-weight:700}}.quick-filters{{display:flex;gap:.35rem;margin:.5rem 0}}.quick-filters button{{background:#e4e9f1;color:#263451;padding:.35rem .65rem}}.quick-filters button.active{{background:#2358d5;color:white}}.sort-button{{padding:0;background:transparent;color:inherit;white-space:nowrap}}.screener th,.screener td{{padding:.45rem .5rem}}.screener th{{position:sticky;top:0}}.ticker-detail>summary{{color:#2358d5;list-style:none;white-space:nowrap}}.detail-close{{float:right;background:#566174}}.status-badge{{display:inline-block;padding:.12rem .4rem;border-radius:999px;font-size:.72rem;font-weight:800}}.status-badge.frozen{{background:#fff0b8;color:#684d00}}.status-badge.delayed{{background:#e8e0ff;color:#51359a}}.status-badge.realtime{{background:#dff4e8;color:#12643e}}.status-badge.na{{background:#e8ebef;color:#596273}}@media(max-width:1100px){{.filter-groups{{grid-template-columns:repeat(2,minmax(280px,1fr))}}}}@media(max-width:800px){{main{{padding:.5rem}}.top{{display:block}}.connection{{margin:.5rem 0}}.filter-groups{{grid-template-columns:1fr}}.form-actions{{flex-wrap:wrap}}.form-actions .mode{{flex-basis:100%;margin-right:0}}}}@media(max-width:430px){{.filter-controls{{flex-wrap:wrap}}.filter-controls .control{{flex-basis:calc(50% - .25rem)}}.universe-group .ticker-control{{flex-basis:100%}}.form-actions button{{flex:1}}.help-tooltip{{position:fixed;left:1rem;right:1rem;bottom:1rem;width:auto}}}}
</style></head><body><main><div class="top"><div><h1>PUT Options Scanner</h1><p class="strategy"><strong>Estrategia: Venta de PUT</strong></p><p class="note">Análisis local de solo lectura. No ejecuta ni ofrece operaciones de trading. <a href="/technical-check">Validación multi-ticker</a></p></div><div id="connection" class="connection"><span class="dot"></span><strong>Comprobando IBKR…</strong><small>Comprobación no bloqueante.</small><button type="button" id="refresh-status">Actualizar estado</button></div></div>{notice}<aside class="interpretation"><p>En una Short PUT, theta positivo para la posición corta representa deterioro temporal teórico favorable, manteniendo constantes las demás variables.</p><p>Una IV elevada puede aumentar la prima, pero también suele reflejar mayor incertidumbre esperada.</p><p class="note">Theta %/día es una aproximación teórica de erosión temporal relativa a la prima, no una rentabilidad diaria garantizada.</p></aside><form method="post" id="scan-form"><div class="filter-groups">
<fieldset class="filter-group universe-group"><legend>Universo</legend><div class="filter-controls"><label class="control universe-source"><span class="control-label">Universo</span><select name="universe_source">{universe_options}</select><small class="field-help">Origen del conjunto</small></label><label class="control ticker-control"><span class="control-label">Tickers / lista</span><input name="ticker" value="{escape(v['ticker'])}" placeholder="NVDA, AAPL SPY" aria-describedby="ticker-help"><small id="ticker-help" class="field-help">Separados por comas o espacios</small></label></div></fieldset>
<fieldset class="filter-group contract-group"><legend>Contrato</legend><div class="filter-controls"><label class="control"><span class="control-label">DTE: Min {help_icon('help-min-dte', 'DTE', dte_help)}</span><input type="number" name="min_dte" min="0" value="{escape(v['min_dte'])}" required><small class="field-help">Días hasta vencimiento</small></label><label class="control"><span class="control-label">DTE: Max {help_icon('help-max-dte', 'DTE', dte_help)}</span><input type="number" name="max_dte" min="0" value="{escape(v['max_dte'])}" required><small class="field-help">Días hasta vencimiento</small></label><label class="control"><span class="control-label">Distancia mínima<br>al strike (%) {help_icon('help-distance', 'Distancia al strike', distance_help)}</span><input type="number" name="min_safety_margin" min="0" max="100" step="0.01" value="{escape(v['min_safety_margin'])}" required><small class="field-help">Underlying ↔ strike</small></label></div></fieldset>
<fieldset class="filter-group short-put-group"><legend>Short PUT</legend><div class="filter-controls"><label class="control"><span class="control-label">|Delta|: Min {help_icon('help-min-delta', '|Delta|', delta_help)}</span><input type="number" name="min_abs_delta" min="0" max="1" step="0.01" value="{escape(v['min_abs_delta'])}" required><small class="field-help">Valor absoluto</small></label><label class="control"><span class="control-label">|Delta|: Max {help_icon('help-max-delta', '|Delta|', delta_help)}</span><input type="number" name="max_abs_delta" min="0" max="1" step="0.01" value="{escape(v['max_abs_delta'])}" required><small class="field-help">Valor absoluto</small></label><label class="control"><span class="control-label">IV mínima (%) {help_icon('help-iv', 'IV mínima', iv_help)}</span><input type="number" name="min_iv" min="0" step="0.01" value="{escape(v['min_iv'])}" placeholder="Desactivada"><small class="field-help">Volatilidad contractual</small></label><label class="control"><span class="control-label">Theta short<br>mínimo {help_icon('help-theta', 'Theta short mínimo', theta_help)}</span><input type="number" name="min_short_theta" min="0" step="0.0001" value="{escape(v['min_short_theta'])}" placeholder="Desactivado"><small class="field-help">Exposición favorable</small></label></div></fieldset>
<fieldset class="filter-group context-group"><legend>Contexto técnico</legend><div class="filter-controls"><label class="control"><span class="control-label">Histórico</span><select name="historical_period"><option value="3m"{' selected' if v['historical_period']=='3m' else ''}>3M</option><option value="6m"{' selected' if v['historical_period']=='6m' else ''}>6M</option><option value="1y"{' selected' if v['historical_period']=='1y' else ''}>1A</option></select><small class="field-help">Ventana de análisis</small></label></div></fieldset></div>
<details class="filter-reference"><summary>ⓘ Cómo interpretar los filtros</summary><div class="reference-card"><table><thead><tr><th scope="col">Métrica</th><th scope="col">Interpretación educativa</th></tr></thead><tbody><tr><th scope="row">Delta</th><td>Delta contractual de la PUT; el scanner usa |Delta|. Menor valor suele corresponder a un strike más OTM y menor exposición direccional.</td></tr><tr><th scope="row">Theta short</th><td>Signo inverso del theta contractual, sin abs(). Positivo indica deterioro temporal teóricamente favorable al vendedor, no beneficio garantizado.</td></tr><tr><th scope="row">Theta %/día</th><td>Erosión temporal teórica diaria respecto a la prima. Es una aproximación, no una rentabilidad diaria garantizada.</td></tr><tr><th scope="row">IV</th><td>Volatilidad implícita: una IV mayor suele acompañar prima e incertidumbre esperada mayores.</td></tr><tr><th scope="row">Vega</th><td>Sensibilidad teórica del precio del contrato a cambios en la volatilidad implícita.</td></tr></tbody></table></div></details><div class="form-actions"><label class="mode"><input id="fake-mode" type="checkbox" name="fake" value="1"{checked}> Modo demostración</label><button id="scan-button" type="submit">Scan</button><button class="secondary-action" name="action" value="watchlist_from_manual" type="submit">Guardar entrada como lista</button></div></form><p id="demo-label" class="demo-label"{' hidden' if not checked else ''}>Datos simulados — no proceden de Interactive Brokers</p>
<section class="watchlists"><h2>Watchlists</h2><p class="note">Estas listas se guardan solo en memoria y se perderán al reiniciar la aplicación.</p><button type="button" onclick="this.nextElementSibling.hidden=!this.nextElementSibling.hidden">Nueva lista</button><form method="post" class="watchlist-row" hidden><label>Nombre<input name="watchlist_name" required></label><label>Tickers separados por coma o espacios<input name="watchlist_tickers" placeholder="NVDA, AAPL SPY" required></label><button name="action" value="watchlist_create" type="submit">Crear lista</button></form>{watchlist_rows}</section>
<div id="scan-status" class="scan-status" role="status" aria-live="polite" hidden><span class="spinner" aria-hidden="true"></span><div><strong id="scan-title"></strong><span id="scan-source"></span><span>Tiempo transcurrido: <b id="scan-timer">00:00</b></span><span class="scan-legend">Estados: Pendiente / Analizando / Completado / Parcial / Error</span></div></div><div id="scan-output" aria-live="polite">{alert}{table}{_summary(result)}</div><script>
const box=document.querySelector('#connection'),fake=document.querySelector('#fake-mode'),label=document.querySelector('#demo-label'),form=document.querySelector('#scan-form'),scanButton=document.querySelector('#scan-button'),scanStatus=document.querySelector('#scan-status'),scanOutput=document.querySelector('#scan-output'),timer=document.querySelector('#scan-timer');let scanning=false,interval;
function elapsed(seconds){{const value=Math.floor(seconds);return String(Math.floor(value/60)).padStart(2,'0')+':'+String(value%60).padStart(2,'0')}}
function finishScan(){{scanning=false;clearInterval(interval);scanStatus.hidden=true;scanButton.disabled=false;scanButton.textContent='Scan'}}
form.addEventListener('submit',async event=>{{if(event.submitter&&event.submitter.value==='watchlist_from_manual')return;event.preventDefault();if(scanning)return;scanning=true;scanButton.disabled=true;scanButton.textContent='Scan en curso...';scanOutput.replaceChildren();scanStatus.hidden=false;document.querySelector('#scan-title').textContent='Analizando universo seleccionado…';document.querySelector('#scan-source').textContent=fake.checked?'Consultando datos de demostración...':'Consultando Interactive Brokers...';const started=performance.now();timer.textContent='00:00';interval=setInterval(()=>timer.textContent=elapsed((performance.now()-started)/1000),250);try{{const response=await fetch('/',{{method:'POST',body:new URLSearchParams(new FormData(form)),headers:{{'X-Requested-With':'fetch'}}}}),html=await response.text(),doc=new DOMParser().parseFromString(html,'text/html'),output=doc.querySelector('#scan-output');if(!output)throw new Error('invalid response');scanOutput.replaceChildren(...Array.from(output.childNodes).map(node=>document.importNode(node,true)));const seconds=(performance.now()-started)/1000;if(response.ok){{const done=document.createElement('p');done.className='completion';done.textContent='Scan completado en '+seconds.toFixed(1)+' s';scanOutput.prepend(done)}}else if(!scanOutput.querySelector('[role="alert"]'))throw new Error('unsafe response')}}catch(error){{scanOutput.replaceChildren();const alert=document.createElement('div');alert.className='error';alert.setAttribute('role','alert');alert.textContent='No se pudo completar el scan. Inténtalo de nuevo.';scanOutput.append(alert)}}finally{{finishScan()}}}});
function demoStatus(){{box.className='connection demo';box.querySelector('strong').textContent='Modo demostración';box.querySelector('small').textContent='La conexión IBKR no es necesaria para este scan.';}}
async function refresh(){{if(fake.checked){{demoStatus();return}} box.className='connection';box.querySelector('strong').textContent='Comprobando IBKR…';try{{const r=await fetch('/ibkr-status',{{cache:'no-store'}}),s=await r.json();box.className='connection '+s.state;box.querySelector('strong').textContent=s.text;box.querySelector('small').textContent=s.message}}catch(e){{box.className='connection disconnected';box.querySelector('strong').textContent='IBKR desconectado';box.querySelector('small').textContent='No se pudo comprobar Client Portal Gateway.'}}}}
fake.addEventListener('change',()=>{{label.hidden=!fake.checked;refresh()}});document.querySelector('#refresh-status').addEventListener('click',refresh);refresh();
document.addEventListener('click',event=>{{const button=event.target.closest('.period-button');if(!button)return;form.elements.historical_period.value=button.dataset.period;form.requestSubmit()}});
document.addEventListener('click',event=>{{
 const close=event.target.closest('.detail-close');if(close){{close.closest('.ticker-detail').open=false;return}}
 const filter=event.target.closest('[data-filter]');if(filter){{document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===filter));document.querySelectorAll('.screener-table tbody tr').forEach(row=>{{const distance=parseFloat(row.cells[4]?.dataset.sortValue),strength=row.cells[5]?.textContent.toLowerCase();row.hidden=!(filter.dataset.filter==='all'||row.classList.contains(filter.dataset.filter)||(filter.dataset.filter==='strong'&&strength.includes('fuerte'))||(filter.dataset.filter==='near'&&Number.isFinite(distance)&&Math.abs(distance)<=3))}});return}}
 const sort=event.target.closest('.sort-button');if(sort){{const table=sort.closest('table'),body=table.tBodies[0],column=Number(sort.dataset.column),ascending=sort.dataset.direction!=='asc';document.querySelectorAll('.sort-button').forEach(b=>delete b.dataset.direction);sort.dataset.direction=ascending?'asc':'desc';const rows=Array.from(body.rows);rows.sort((a,b)=>{{let x=a.cells[column].dataset.sortValue,y=b.cells[column].dataset.sortValue;if(sort.dataset.kind==='number'){{x=x===''?Number.POSITIVE_INFINITY:Number(x);y=y===''?Number.POSITIVE_INFINITY:Number(y);return (x-y)*(ascending?1:-1)}}return x.localeCompare(y,undefined,{{numeric:true}})*(ascending?1:-1)}}).forEach(row=>body.append(row));}}
}});
document.addEventListener('toggle',async event=>{{
 const details=event.target;
 if(details.matches('.ticker-detail')&&details.open)document.querySelectorAll('.ticker-detail[open]').forEach(item=>{{if(item!==details)item.open=false}});
 if(!details.matches('.lazy-chart')||!details.open||details.dataset.loaded)return;
 details.dataset.loaded='true';const panel=details.querySelector('.chart-panel');panel.textContent='Cargando gráfico…';
 try{{const response=await fetch(details.dataset.chartUrl);if(!response.ok)throw new Error();const doc=new DOMParser().parseFromString(await response.text(),'text/html'),chart=doc.querySelector('.chart-panel');if(!chart)throw new Error();panel.innerHTML=chart.innerHTML}}catch(error){{panel.textContent='Gráfico no disponible.'}}
}},true);
</script></main></body></html>'''
    return html.encode()


def create_app(service: PutScanService | None = None, *, base_url: str = "https://localhost:5000/v1/api", status_transport: object | None = None,
               technical_price_provider=None, technical_history_provider=None, ticker_workers: int = 3,
               global_http_limit: int = 8,
               watchlists: dict[str, tuple[str, ...]] | None = None,
               workspace_store: UserWorkspaceStore | None = None, user: User | None = None):
    if not 1 <= ticker_workers <= 4:
        raise ValueError("ticker_workers debe estar entre 1 y 4")
    if not 1 <= global_http_limit <= 16:
        raise ValueError("global_http_limit debe estar entre 1 y 16")
    scanner = service or PutScanService()
    transport = status_transport or __import__("options_scanner.ibkr", fromlist=["ClientPortalTransport"]).ClientPortalTransport(base_url, allow_insecure_tls=True, timeout=2.0)
    technical_cache: dict[str, TechnicalCheckResult] = {}
    scan_cache: dict[str, ScanResult] = {}
    store = workspace_store or UserWorkspaceStore()
    current_user = user or User("local", "Usuario local")
    try:
        store.watchlists_for(current_user.id)
    except KeyError:
        store.add_user(current_user)
    for key, symbols in (watchlists or {}).items():
        store.save_watchlist(Watchlist(key, current_user.id, key, tuple(symbols)))
    def application(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path == "/technical-check" and environ.get("REQUEST_METHOD") == "GET":
            provider = technical_price_provider or IbkrMarketDataProvider(ClientPortalTransport(base_url, allow_insecure_tls=True))
            results = check_tickers(DEFAULT_TICKERS, HistoricalPeriod.SIX_MONTHS, provider,
                                    technical_history_provider)
            technical_cache.update((item.symbol, item) for item in results)
            body = render_technical_screener(results)
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])
            return [body]
        if path == "/technical-check/chart" and environ.get("REQUEST_METHOD") == "GET":
            ticker = parse_qs(environ.get("QUERY_STRING", "")).get("ticker", [""])[0].upper()
            body = render_technical_chart(technical_cache.get(ticker))
            status = "200 OK" if ticker in technical_cache else "404 Not Found"
            start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])
            return [body]
        if path == "/scan-chart" and environ.get("REQUEST_METHOD") == "GET":
            ticker = parse_qs(environ.get("QUERY_STRING", "")).get("ticker", [""])[0].upper()
            cached = scan_cache.get(ticker)
            body = (_technical_chart(cached) if cached and cached.technical_context
                    else '<p role="status">Gráfico no disponible.</p>')
            status = "200 OK" if cached else "404 Not Found"
            start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])
            return [body.encode()]
        if environ.get("PATH_INFO") == "/ibkr-status" and environ.get("REQUEST_METHOD") == "GET":
            body = json.dumps(ibkr_connection_status(transport)).encode()
            start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")])
            return [body]
        if environ.get("PATH_INFO", "/") != "/" or environ.get("REQUEST_METHOD") not in ("GET", "POST"):
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Not found"]
        values: dict[str, str] = {}
        result = None
        multi_results: tuple[tuple[str, ScanResult | None, str | None], ...] = ()
        multi_metrics = None
        error = None
        watchlist_message = None
        status = "200 OK"
        if environ["REQUEST_METHOD"] == "POST":
            try:
                size = min(int(environ.get("CONTENT_LENGTH") or 0), 8192)
                data = parse_qs(environ["wsgi.input"].read(size).decode("utf-8"), keep_blank_values=True)
                values = {key: entries[0] for key, entries in data.items()}
                values["mode"] = "fake" if values.get("fake") == "1" else "live"
                action = values.get("action", "scan")
                if action.startswith("watchlist_"):
                    if action == "watchlist_delete":
                        store.delete_watchlist(current_user.id, values.get("watchlist_id", ""))
                        watchlist_message = "Watchlist eliminada."
                    else:
                        raw = values.get("ticker", "") if action == "watchlist_from_manual" else values.get("watchlist_tickers", "")
                        symbols = parse_tickers(raw)
                        name = values.get("watchlist_name", "").strip()
                        if action == "watchlist_from_manual" and not name:
                            name = "Lista manual"
                        if not name:
                            raise ValueError("nombre vacío")
                        item_id = values.get("watchlist_id", "") if action == "watchlist_update" else uuid4().hex
                        if action == "watchlist_update" and item_id not in {item.id for item in store.watchlists_for(current_user.id)}:
                            raise KeyError("watchlist desconocida")
                        store.save_watchlist(Watchlist(item_id, current_user.id, name, symbols))
                        watchlist_message = "Watchlist guardada."
                    values = {}
                    raise StopIteration
                current_watchlists = {item.id: item.symbols for item in store.watchlists_for(current_user.id)}
                tickers = resolve_universe(values.get("universe_source", "manual"),
                                           values.get("ticker", ""), current_watchlists)
                values["ticker"] = ", ".join(tickers)
                request_options = dict(
                    min_dte=int(values.get("min_dte", "")),
                    max_dte=int(values.get("max_dte", "")),
                    min_safety_margin=float(values.get("min_safety_margin", "")) / 100,
                    min_abs_delta=float(values.get("min_abs_delta", "")),
                    max_abs_delta=float(values.get("max_abs_delta", "")), fake=values["mode"] == "fake",
                    min_iv=(float(values["min_iv"]) / 100 if values.get("min_iv", "").strip() else None),
                    min_short_theta=(float(values["min_short_theta"]) if values.get("min_short_theta", "").strip() else None),
                    historical_period=HistoricalPeriod(values.get("historical_period", "6m")),
                )
                if len(tickers) == 1:
                    result = scanner.run(ScanRequest(ticker=tickers[0], **request_options),
                                         base_url=base_url, allow_insecure_tls=True)
                else:
                    def scan_one(ticker, limiter):
                        item = scanner.run(ScanRequest(ticker=ticker, **request_options),
                                           base_url=base_url, allow_insecure_tls=True,
                                           work_limiter=limiter)
                        scan_cache[ticker] = item
                        return item
                    raw_results, multi_metrics = run_multi_ticker(
                        tickers, scan_one, ticker_workers=ticker_workers,
                        global_http_limit=global_http_limit)
                    def safe_error(ticker, exc):
                        if isinstance(exc, NotAuthenticatedError): return "Sesión de IBKR no autenticada."
                        if isinstance(exc, GatewayUnavailableError): return "Client Portal Gateway no está disponible."
                        if isinstance(exc, IbkrError): return "IBKR no pudo completar este ticker."
                        logger.error("Unexpected multi-ticker scan failure for %s (%s)", ticker, type(exc).__name__)
                        return "No se pudo completar este ticker."
                    multi_results = tuple((ticker, item, None if exc is None else safe_error(ticker, exc))
                                          for ticker, item, exc in raw_results)
            except StopIteration:
                pass
            except (ValueError, KeyError):
                error, status = "Revisa los parámetros del formulario e inténtalo de nuevo.", "400 Bad Request"
            except NotAuthenticatedError:
                error, status = "Client Portal Gateway está disponible, pero la sesión de IBKR no está autenticada.", "503 Service Unavailable"
            except GatewayUnavailableError:
                error, status = "No se pudo conectar con Client Portal Gateway. Inícialo y comprueba la sesión.", "503 Service Unavailable"
            except IbkrError:
                error, status = "IBKR no pudo completar el scan. Comprueba Gateway y vuelve a intentarlo.", "502 Bad Gateway"
            except Exception:
                logger.exception("Unexpected web scan failure")
                error, status = "No se pudo completar el scan. Inténtalo de nuevo.", "500 Internal Server Error"
        current_watchlists = {item.id: item for item in store.watchlists_for(current_user.id)}
        body = render_page(values, result, error, multi_results, multi_metrics,
                           current_watchlists, watchlist_message)
        start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                                ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")])
        return [body]
    return application


def main() -> None:
    host, port = "127.0.0.1", 8000
    print(f"Options Scanner local: http://{host}:{port}")
    with make_server(host, port, create_app()) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
