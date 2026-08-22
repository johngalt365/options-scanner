from io import BytesIO
from dataclasses import replace
from unittest import TestCase

from options_scanner.ibkr import (GatewayUnavailableError, IncompleteDataError,
                                  NotAuthenticatedError, TickerNotFoundError)
from options_scanner.scan_service import (DiscardedContract, PutScanService, ScanMetrics,
                                          ScanRequest, ScanResult)
from options_scanner.scanner import PutScanCandidate
from options_scanner.web import (_directional_distance, _evaluation_section, _interpretation, _multi_screener, _rows, create_app, ibkr_connection_status, parse_tickers,
                                 render_page, render_technical_screener, resolve_universe)
from options_scanner.historical import HistoricalBar
from datetime import timedelta
from options_scanner.scanner import rank_candidates
from options_scanner.technical_analysis import PriceZone, ZoneType
from options_scanner.technical_context import (ConfluenceOrigin, TechnicalConfluence, TechnicalContext,
                                               build_multi_technical_context)
from options_scanner.technical_check import TechnicalCheckResult
from options_scanner.historical import HistoricalPeriod
from datetime import date
from options_scanner.models import User
from options_scanner.workspace import UserWorkspaceStore
from options_scanner.short_put_ranking import rank_by_score


def test_ranked_evaluation_reaches_single_and_multi_renderers_end_to_end():
    result = PutScanService(today=lambda: date(2026, 8, 20)).run(
        ScanRequest(ticker="NVDA", fake=True, historical_period=HistoricalPeriod.MULTI)
    )
    best = result.candidates[0]
    evaluation = best.evaluation

    assert evaluation is not None
    single = render_page(values={"ticker": "AEHR"}, result=result).decode()
    multi = render_page(multi_results=(("AEHR", result, None),)).decode()

    # The service-owned evaluation, rather than a renderer calculation, feeds
    # the row, contract table, and selected-candidate explanation.
    for page in (single, multi):
        assert "Evaluación Short PUT" in page
        assert f"{evaluation.total_score:.2f}/100 · {evaluation.label}" in page
        assert f"{evaluation.risk_score:.2f}/30" in page
        assert f"{evaluation.technical_score:.2f}/25" in page
        assert f"{evaluation.premium_score:.2f}/20" in page
        assert f"{evaluation.theta_score:.2f}/15" in page
        assert f"{evaluation.liquidity_score:.2f}/10" in page
        assert "Spread relativo:" in page
        assert "Fortalezas:" in page
        assert "Debilidades:" in page
        assert "Datos ausentes que reducen la confianza:" in page
    assert '<th class="detail-launch"><span class="sr-only">Abrir detalle</span></th><th><button type="button" class="sort-button" data-column="0" data-kind="text">Ticker' in multi
    assert 'class="sort-button" data-column="1" data-kind="number">Score' in multi


def test_evaluation_identifies_winner_and_complete_candidate_count():
    result = PutScanService(today=lambda: date(2026, 8, 20)).run(
        ScanRequest(ticker="NVDA", fake=True, historical_period=HistoricalPeriod.MULTI)
    )
    best = result.candidates[0]
    page = render_page(values={"ticker": "NVDA"}, result=result).decode()

    assert (f"Evaluación Short PUT — {best.ticker} · PUT ${best.strike:g} · "
            f"{best.expiration.day} Sep {best.expiration.year} · {best.dte} DTE") in page
    assert f"Mejor candidato de {len(result.candidates)} contratos completos" in page
    assert "<th>Ticker</th><th>Score</th><th>Evaluación</th><th>Expiration</th>" in page


def test_every_complete_candidate_uses_attached_evaluation_in_score_order():
    result = PutScanService(today=lambda: date(2026, 8, 20)).run(
        ScanRequest(ticker="NVDA", fake=True, historical_period=HistoricalPeriod.MULTI)
    )
    rows = _rows(result)
    scores = [candidate.evaluation.total_score for candidate in result.candidates]

    assert scores == sorted(scores, reverse=True)
    for candidate in result.candidates:
        assert candidate.evaluation is not None
        assert f"{candidate.evaluation.total_score:.2f}" in rows
        assert candidate.evaluation.label in rows
    assert rows.index(f"{scores[0]:.2f}") < rows.index(f"{scores[-1]:.2f}")


def test_winner_score_is_identical_in_multi_detail_evaluation_and_first_row():
    result = PutScanService(today=lambda: date(2026, 8, 20)).run(
        ScanRequest(ticker="NVDA", fake=True, historical_period=HistoricalPeriod.MULTI)
    )
    best = result.candidates[0]
    score = f"{best.evaluation.total_score:.2f}"
    multi = render_page(multi_results=((best.ticker, result, None),)).decode()

    assert f'<td data-sort-value="{score}">{score}</td>' in multi
    assert f"<summary>{score}/100 · {best.evaluation.label}</summary>" in multi
    assert f"<dd>{score}/100 · {best.evaluation.label}</dd>" in multi
    assert _rows(result).split("</tr>", 1)[0].count(score) >= 2


def test_single_candidate_and_missing_optional_data_remain_explicit():
    result = PutScanService(today=lambda: date(2026, 8, 20)).run(
        ScanRequest(ticker="NVDA", fake=True, historical_period=HistoricalPeriod.MULTI)
    )
    candidate = replace(result.candidates[0], implied_volatility=None, theta=None,
                        open_interest=None)
    winner = rank_by_score([candidate], result.technical_context)[0]
    section = _evaluation_section(winner, 1)
    rows = _rows(replace(result, candidates=(winner,)))

    assert "Mejor candidato de 1 contrato completo" in section
    assert "IV" in section and "theta relativo" in section and "open interest" in section
    assert f"{winner.evaluation.total_score:.2f}" in rows


def request(app, method="GET", body=""):
    encoded = body.encode()
    captured = {}
    environ = {"PATH_INFO": "/", "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(encoded)),
               "wsgi.input": BytesIO(encoded)}
    def start_response(status, headers):
        captured["status"], captured["headers"] = status, headers
    output = b"".join(app(environ, start_response)).decode()
    return captured["status"], output


def request_path(app, path, query=""):
    captured = {}
    environ = {"PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": "GET",
               "wsgi.input": BytesIO()}
    output = b"".join(app(environ, lambda status, headers: captured.update(status=status))).decode()
    return captured["status"], output


FORM = "ticker=NVDA&min_dte=30&max_dte=45&min_safety_margin=20&min_abs_delta=0.15&max_abs_delta=0.30"


class StubService:
    def __init__(self, result=None, error=None):
        self.result = result or ScanResult((), ScanMetrics(), .01)
        self.error = error
        self.requests = []

    def run(self, scan_request, **kwargs):
        self.requests.append(scan_request)
        if self.error:
            raise self.error
        return self.result


class StatusTransport:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def get(self, path, params):
        self.calls.append((path, params))
        if self.error:
            raise self.error
        return self.payload


class WebTest(TestCase):
    def test_multi_detail_uses_each_stored_context_and_lazy_chart_without_rescan(self):
        bar = HistoricalBar(date(2026, 1, 2), 99, 102, 98, 101)

        class ContextService(StubService):
            def run(self, scan_request, **kwargs):
                self.requests.append(scan_request)
                offset = 0 if scan_request.ticker == "NVDA" else 50
                support = PriceZone(90 + offset, 92 + offset, 91 + offset,
                                    ZoneType.SUPPORT, 3, date(2026, 1, 1), 2, "fuerte")
                resistance = PriceZone(108 + offset, 110 + offset, 109 + offset,
                                       ZoneType.RESISTANCE, 2, date(2026, 1, 1), 3, "media")
                price = 100 + offset
                context = TechnicalContext(scan_request.ticker, HistoricalPeriod.SIX_MONTHS,
                                           (bar,), price, (support, resistance), (support,),
                                           (resistance,), support, resistance, None, None, ())
                return ScanResult((), ScanMetrics(historical_status="ok"), .01,
                                  underlying_price=price, market_data_status="RealTime",
                                  technical_context=context)

        service = ContextService()
        app = create_app(service, ticker_workers=1)
        status, page = request(app, "POST", FORM.replace("NVDA", "NVDA%2CSPY"))

        self.assertEqual(status, "200 OK")
        self.assertIn('data-ticker="NVDA"', page)
        self.assertIn('data-ticker="SPY"', page)
        self.assertIn("$90.00–$92.00", page)
        self.assertIn("$140.00–$142.00", page)
        for level in ("S1", "S2", "S3", "R1", "R2"):
            self.assertIn(f"<dt>{level}</dt>", page)
        self.assertEqual(page.count('<details class="ticker-detail" open'), 0)
        self.assertEqual(page.count('<details class="lazy-chart"'), 2)
        self.assertNotIn('<svg role="img"', page)
        self.assertIn(".ticker-detail[open]", page)

        calls_after_scan = len(service.requests)
        chart_status, nvda_chart = request_path(app, "/scan-chart", "ticker=NVDA")
        self.assertEqual(chart_status, "200 OK")
        self.assertIn('<svg role="img"', nvda_chart)
        self.assertIn('data-ticker="NVDA"', nvda_chart)
        self.assertNotIn('data-ticker="SPY"', nvda_chart)
        self.assertEqual(len(service.requests), calls_after_scan)

    def test_multi_detail_reports_missing_history_and_single_ticker_keeps_chart(self):
        empty = ScanResult((), ScanMetrics(historical_status="empty", historical_period="6m"),
                           .01, underlying_price=100, market_data_status="Delayed",
                           technical_context=None)
        multi = render_page(multi_results=(("EMPTY", empty, None), ("OTHER", empty, None))).decode()
        self.assertEqual(multi.count("Histórico no disponible"), 2)

        bar = HistoricalBar(date(2026, 1, 2), 99, 102, 98, 101)
        context = TechnicalContext("ONE", HistoricalPeriod.SIX_MONTHS, (bar,), 101,
                                   (), (), (), None, None, None, None, ())
        single = render_page(result=ScanResult((), ScanMetrics(), .01,
                                               underlying_price=101,
                                               technical_context=context),
                             values={"ticker": "ONE"}).decode()
        self.assertIn('<svg role="img"', single)
        self.assertIn("Ver gráfico", single)

    def test_multi_detail_shows_status_for_each_horizon_and_partial_chart(self):
        bar = HistoricalBar(date(2026, 1, 2), 99, 102, 98, 101)
        context = build_multi_technical_context("AEHR", {
            HistoricalPeriod.THREE_MONTHS: (bar,),
            HistoricalPeriod.SIX_MONTHS: (bar,),
            HistoricalPeriod.ONE_YEAR: (),
        }, 101)
        page = render_page(result=ScanResult(
            (), ScanMetrics(historical_status="ok", historical_period="multi"), .01,
            underlying_price=101, technical_context=context,
        ), values={"ticker": "AEHR", "historical_period": "multi"}).decode()
        self.assertIn("3M ✓ · 6M ✓ · 1A sin datos", page)
        self.assertNotIn("Histórico no disponible", page)
        self.assertIn('<svg role="img"', page)

        one_context = build_multi_technical_context("AEHR", {
            HistoricalPeriod.THREE_MONTHS: (bar,),
        }, 101)
        one_page = render_page(result=ScanResult(
            (), ScanMetrics(historical_status="ok", historical_period="multi"), .01,
            underlying_price=101, technical_context=one_context,
        ), values={"ticker": "AEHR", "historical_period": "multi"}).decode()
        self.assertIn("No hay suficientes horizontes para determinar confluencia", one_page)
        self.assertNotIn("Histórico no disponible", one_page)

    def test_explainable_strike_columns_quick_filters_and_sorting_controls(self):
        zone = PriceZone(70.73, 91.04, 80, ZoneType.SUPPORT, 3, date(2026, 8, 1), 75, "fuerte")
        base = dict(expiration=date(2026, 9, 24), dte=35, underlying_price=100,
                    safety_margin=.20, bid=1, ask=1.2, delta=-.2, gamma=None, theta=None,
                    vega=None, implied_volatility=.3, open_interest=100,
                    market_data_availability="RealTime", nearest_support_below=zone,
                    support_strength="fuerte", support_zone_label="S1")
        cases = (
            ("ABOVE", 95, "ABOVE_SUPPORT", 4.35, "Por encima de S1", "strike-above", "Sobre S1"),
            ("INSIDE", 80, "INSIDE_SUPPORT", 0, "Dentro de S1", "strike-inside", "Dentro S1"),
            ("BELOW", 65, "BELOW_SUPPORT", -8.10, "Por debajo de S1", "strike-below", "Bajo S1"),
        )
        items = []
        for ticker, strike, position, distance, label, _, _ in cases:
            candidate = PutScanCandidate(ticker=ticker, strike=strike, support_position=position,
                                         distance_to_support_pct=distance,
                                         support_position_label=label, **base)
            items.append((ticker, ScanResult((candidate,), ScanMetrics(), .1,
                                             underlying_price=100), None))
        page = _multi_screener(tuple(items))

        for _, _, _, _, _, css_class, label in cases:
            self.assertIn(css_class, page)
            self.assertIn(label, page)
        for label in ("Strike sobre soporte", "Strike dentro soporte", "Strike bajo soporte"):
            self.assertIn(label, page)
        for heading in ("Distancia al strike", "Delta", "Theta short", "Theta %/día", "IV",
                        "Premium yield", "Annualized yield", "Open interest"):
            self.assertRegex(page, rf'class="sort-button"[^>]*>{heading}')
        self.assertIn("Strike $95.00 situado por encima de S1 ($70.73–$91.04). S1 fuerte, 3 contactos.", page)
        self.assertNotIn("recomend", page.lower())

    def test_multi_table_detail_and_candidate_summary_share_strike_classification(self):
        zone = PriceZone(70.73, 91.04, 80, ZoneType.SUPPORT, 3, date(2026, 8, 1), 75, "fuerte")
        periods = (HistoricalPeriod.THREE_MONTHS, HistoricalPeriod.SIX_MONTHS,
                   HistoricalPeriod.ONE_YEAR)
        confluence = TechnicalConfluence(
            70.73, 91.04, ZoneType.SUPPORT,
            tuple(ConfluenceOrigin(period, zone) for period in periods[:2]), 0,
        )
        context = TechnicalContext("AEHR", HistoricalPeriod.MULTI, (), 100, (), (), (),
                                   None, None, None, None, (), (), (confluence,),
                                   periods, periods[:2])
        candidate = PutScanCandidate(
            ticker="AEHR", expiration=date(2026, 9, 24), dte=34, strike=80,
            underlying_price=100, safety_margin=.20, bid=1, ask=1.2, delta=-.2,
            gamma=None, theta=None, vega=None, implied_volatility=.3,
            open_interest=100, market_data_availability="RealTime",
        )
        result = ScanResult((candidate,), ScanMetrics(historical_period="multi"), .1,
                            underlying_price=100, technical_context=context)

        page = _multi_screener((("AEHR", result, None),))

        # Table relationship, candidate textual summary, and detail explanation all
        # originate in TechnicalContext.classify_strike_against_confluence.
        self.assertGreaterEqual(page.count("Dentro de confluencia"), 3)
        self.assertIn('class="has-candidates strike-inside"', page)
        self.assertIn("$70.73–$91.04", page)
        self.assertIn("2/3", page)
        self.assertIn(
            "Strike $80.00 dentro de confluencia de soporte $70.73–$91.04 · 2/3 horizontes.",
            page,
        )
        self.assertNotIn("sin zona de soporte relevante disponible", page)

    def test_multi_screener_controls_badges_summary_and_fourteen_rows(self):
        items = tuple(
            (f"T{i:02d}", ScanResult((), ScanMetrics(), .1, underlying_price=100 + i,
                                      market_data_status=("Frozen" if i == 0 else "Delayed")), None)
            for i in range(14)
        )
        page = render_page(multi_results=items).decode()
        self.assertEqual(page.count('class="ticker-detail"'), 14)
        self.assertEqual(page.count('<details class="ticker-detail" open'), 0)
        self.assertIn("0 completados · 0 parciales · 14 sin candidatos · 0 error · 1.4 s", page)
        for name in ("Todos", "Con candidatos", "Sin candidatos", "Soporte fuerte", "Cerca de S1"):
            self.assertIn(name, page)
        for heading in ("Ticker", "Precio", "Distancia S1", "Candidatos", "Delta",
                        "Premium yield", "Annualized yield"):
            self.assertRegex(page, rf'class="sort-button"[^>]*>{heading}')
        self.assertIn('class="status-badge frozen"', page)
        self.assertIn('class="status-badge delayed"', page)
        self.assertIn('class="screener-table"', page)
        self.assertIn(".scroll{overflow:auto}", page)

    def test_detail_actions_are_delegated_and_charts_lazy(self):
        result = ScanResult((), ScanMetrics(), .1, underlying_price=100,
                            market_data_status="RealTime")
        page = render_page(multi_results=(("NVDA", result, None), ("SPY", result, None))).decode()
        self.assertEqual(page.count("<summary>Ver detalle</summary>"), 2)
        self.assertIn(".detail-close", page)
        self.assertIn(".lazy-chart", page)
        self.assertNotIn('<svg role="img"', page)
        self.assertIn("addEventListener('toggle'", page)
        self.assertEqual(page.count('class="detail-trigger chevron"'), 2)
        self.assertIn('aria-label="Ver detalle de NVDA"', page)

    def test_zone_distances_are_directional_and_legacy_modes_keep_columns(self):
        support = PriceZone(70, 80, 75, ZoneType.SUPPORT, 2, date(2026, 8, 1), 70, "media")
        resistance = PriceZone(130, 150, 140, ZoneType.RESISTANCE, 2, date(2026, 8, 1), 130, "media")
        context = TechnicalContext("XYZ", HistoricalPeriod.SIX_MONTHS, (), 100,
            (support, resistance), (support,), (resistance,), support, resistance, 25, 40, ())
        result = ScanResult((), ScanMetrics(historical_period="6m"), .1,
                            underlying_price=100, technical_context=context)
        self.assertEqual(_directional_distance(-25, "support"), "25.00% por debajo del precio")
        self.assertEqual(_directional_distance(40, "resistance"), "40.00% por encima del precio")
        for heading in ("S1", "Distancia S1", "Fuerza S1"):
            self.assertIn(heading, _multi_screener((("XYZ", result, None),)))

    def test_compact_header_keeps_watchlist_selector_and_financial_filters(self):
        page = render_page(watchlists={
            "core": __import__("options_scanner.models", fromlist=["Watchlist"]).Watchlist(
                "core", "local", "Core", ("NVDA", "SPY"))
        }).decode()
        self.assertIn('name="universe_source"', page)
        self.assertIn("Watchlist: Core", page)
        for name in ("min_dte", "max_dte", "min_safety_margin", "min_abs_delta",
                     "max_abs_delta", "historical_period", "fake"):
            self.assertIn(f'name="{name}"', page)

    def test_filter_panel_groups_controls_aligns_labels_and_exposes_help(self):
        page = render_page().decode()

        for group in ("Universo", "Contrato", "Short PUT", "Contexto técnico"):
            self.assertIn(f"<legend>{group}</legend>", page)
        self.assertIn('class="control-label">Distancia mínima<br>al strike (%)', page)
        self.assertIn('class="control-label">Theta short<br>mínimo', page)
        self.assertEqual(page.count('class="help-trigger"'), 9)
        self.assertEqual(page.count('role="tooltip"'), 9)
        for help_id in ("help-universe", "help-min-dte", "help-max-dte", "help-distance", "help-min-delta",
                        "help-max-delta", "help-iv", "help-theta"):
            self.assertIn(f'aria-describedby="{help_id}"', page)
            self.assertIn(f'id="{help_id}" role="tooltip"', page)
        self.assertIn('id="help-history" role="tooltip"', page)
        self.assertIn("Universo soportado: acciones de EE. UU. con opciones negociables en IBKR.", page)
        for universe_help_text in (
            "Esta versión está diseñada para acciones de EE. UU.",
            "opciones accesibles mediante Interactive Brokers.",
            "Otros mercados, ETFs, índices y otros tipos de activo no forman parte actualmente",
        ):
            self.assertIn(universe_help_text, page)
        for history_help_text in (
            "3M / 6M / 1A: analizan soportes y resistencias utilizando únicamente ese horizonte histórico.",
            "Multi: analiza independientemente 3M, 6M y 1A y busca confluencias",
            "Una confluencia 3/3 significa que los tres horizontes presentan una zona solapada; "
            "2/3, que coinciden dos de los tres.",
            "El horizonte histórico afecta únicamente al contexto técnico del underlying.",
            "No modifica DTE, Delta, Theta, IV ni los demás criterios del screening de opciones.",
        ):
            self.assertIn(history_help_text, page)
        self.assertNotIn("En una Short PUT, theta positivo", page)
        for help_text in (
            "Días restantes hasta el vencimiento de la opción.",
            "Mayor distancia proporciona mayor colchón, normalmente a cambio de menor prima.",
            "0,15–0,30 es el rango base actualmente utilizado.",
            "no es una probabilidad exacta.",
            "Una IV alta no significa automáticamente mejor oportunidad.",
            "sin usar abs().",
            "no representa beneficio diario garantizado.",
        ):
            self.assertIn(help_text, page)
        self.assertIn('.filter-help:hover .help-tooltip,.filter-help:focus-within .help-tooltip', page)
        self.assertIn('position:absolute', page)
        self.assertIn('class="form-actions"', page)
        self.assertIn('.control-label{display:flex;min-height:2.1em;align-items:flex-end', page)
        self.assertIn('@media(max-width:1100px)', page)
        self.assertIn('@media(max-width:430px)', page)

    def test_filter_reference_is_keyboard_native_and_exclusively_educational(self):
        page = render_page().decode()

        self.assertIn('<details class="filter-reference"><summary>ⓘ Cómo interpretar los filtros</summary>', page)
        for metric in ("Delta", "Theta short", "Theta %/día", "IV", "Vega"):
            self.assertIn(f'<th scope="row">{metric}</th>', page)
        self.assertIn("Es una aproximación, no una rentabilidad diaria garantizada.", page)
        self.assertNotIn('data-filter="vega"', page)

    def test_watchlist_crud_selection_validation_and_user_isolation(self):
        store = UserWorkspaceStore()
        service = StubService()
        ana = create_app(service, workspace_store=store, user=User("ana", "Ana"))
        create = "action=watchlist_create&watchlist_name=Core&watchlist_tickers=nvda%2C+SPY+nvda"
        status, page = request(ana, "POST", create)
        self.assertEqual(status, "200 OK")
        item = store.watchlists_for("ana")[0]
        self.assertEqual(item.symbols, ("NVDA", "SPY"))
        self.assertIn("Watchlist: Core", page)
        self.assertIn("exclusivamente al usuario autenticado", page)

        update = (f"action=watchlist_update&watchlist_id={item.id}&watchlist_name=Growth"
                  "&watchlist_tickers=qqq+MSFT+qqq")
        self.assertEqual(request(ana, "POST", update)[0], "200 OK")
        self.assertEqual(store.watchlists_for("ana")[0].symbols, ("QQQ", "MSFT"))
        scan = FORM.replace("ticker=NVDA", "ticker=") + f"&universe_source=watchlist%3A{item.id}"
        request(ana, "POST", scan)
        self.assertEqual([r.ticker for r in service.requests], ["QQQ", "MSFT"])

        for tickers in ("", "%24BAD"):
            status, _ = request(ana, "POST", "action=watchlist_create&watchlist_name=Bad&watchlist_tickers=" + tickers)
            self.assertEqual(status, "400 Bad Request")
        bruno = create_app(StubService(), workspace_store=store, user=User("bruno", "Bruno"))
        self.assertNotIn("Growth", request(bruno)[1])
        self.assertEqual(request(bruno, "POST", f"action=watchlist_delete&watchlist_id={item.id}")[0],
                         "400 Bad Request")
        self.assertEqual(request(ana, "POST", f"action=watchlist_delete&watchlist_id={item.id}")[0], "200 OK")
        self.assertEqual(store.watchlists_for("ana"), ())

    def test_create_watchlist_from_current_manual_input(self):
        store = UserWorkspaceStore()
        app = create_app(StubService(), workspace_store=store)
        status, page = request(app, "POST", "action=watchlist_from_manual&ticker=aapl%2C+MSFT+aapl")
        self.assertEqual(status, "200 OK")
        self.assertEqual(store.watchlists_for("local")[0].symbols, ("AAPL", "MSFT"))
        self.assertIn("Lista manual", page)

    def test_ticker_list_normalizes_separators_case_and_duplicates(self):
        self.assertEqual(parse_tickers(" aaoi, NVDA  aaoi\tspy,QQQ "),
                         ("AAOI", "NVDA", "SPY", "QQQ"))
        self.assertEqual(parse_tickers("asx"), ("ASX",))
        for invalid in ("", "NVDA,$BAD", "TOO-LONG-SYMBOL"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_tickers(invalid)

    def test_manual_and_watchlist_universes_reach_the_same_multi_ticker_pipeline(self):
        manual_service = StubService()
        watchlist_service = StubService()
        manual = FORM.replace("NVDA", "nvda%2C+spy+NVDA+qqq") + "&universe_source=manual"
        watchlist = FORM.replace("ticker=NVDA", "ticker=") + "&universe_source=watchlist%3Acore"

        # A single worker makes invocation order observable; concurrent result
        # ordering is covered independently by the multi-scan tests.
        manual_status, manual_page = request(create_app(manual_service, ticker_workers=1), "POST", manual)
        watchlist_status, watchlist_page = request(
            create_app(watchlist_service, ticker_workers=1,
                       watchlists={"core": ("nvda", "SPY", "nvda", "qqq")}),
            "POST", watchlist,
        )

        self.assertEqual((manual_status, watchlist_status), ("200 OK", "200 OK"))
        expected = ["NVDA", "SPY", "QQQ"]
        self.assertEqual([item.ticker for item in manual_service.requests], expected)
        self.assertEqual([item.ticker for item in watchlist_service.requests], expected)
        self.assertEqual(manual_page.count('class="ticker-detail"'), 3)
        self.assertEqual(watchlist_page.count('class="ticker-detail"'), 3)

    def test_all_universe_sources_use_the_canonical_normalizer(self):
        self.assertEqual(resolve_universe("manual", "spy, qqq SPY"), ("SPY", "QQQ"))
        self.assertEqual(resolve_universe("watchlist:mine", "ignored",
                                          {"mine": ("spy", "QQQ", "spy")}), ("SPY", "QQQ"))
        self.assertEqual(resolve_universe("group:indices", "ignored"), ("SPY", "QQQ", "IWM"))

    def test_multi_ticker_is_compact_and_failure_does_not_abort_other_rows(self):
        class MixedService(StubService):
            def run(self, scan_request, **kwargs):
                self.requests.append(scan_request)
                if scan_request.ticker == "BAD":
                    raise RuntimeError("secret payload cookie")
                return ScanResult((), ScanMetrics(historical_status="empty"), .01,
                                  underlying_price=100, market_data_status="Delayed")
        service = MixedService()
        status, page = request(create_app(service), "POST", FORM.replace("NVDA", "nvda%2C+BAD+spy"))
        self.assertEqual(status, "200 OK")
        # Workers may enter the stub in any order; output remains in input order.
        self.assertCountEqual([item.ticker for item in service.requests], ["NVDA", "BAD", "SPY"])
        self.assertEqual(page.count('class="ticker-detail"'), 3)
        self.assertIn("Screener multi-ticker", page)
        self.assertIn("Delayed", page)
        self.assertIn("No se pudo completar este ticker.", page)
        self.assertNotIn("secret payload", page)
        self.assertNotIn('<svg role="img"', page)
        self.assertNotIn('<details class="ticker-detail" open', page)

    def test_multi_ticker_concurrency_is_configurable_and_capped_at_four(self):
        import threading
        import time
        class MeasuringService(StubService):
            def __init__(self):
                super().__init__(); self.active = self.maximum = 0; self.lock = threading.Lock()
            def run(self, scan_request, **kwargs):
                with self.lock:
                    self.active += 1; self.maximum = max(self.maximum, self.active)
                time.sleep(.02)
                with self.lock: self.active -= 1
                return ScanResult((), ScanMetrics(), .01)
        service = MeasuringService()
        request(create_app(service, ticker_workers=4), "POST", FORM.replace("NVDA", "AAOI+AEHR+COHR+LITE"))
        self.assertEqual(service.maximum, 4)
        with self.assertRaises(ValueError):
            create_app(service, ticker_workers=5)

    def test_compact_rows_cover_zone_absence_history_failure_and_feed_states(self):
        bar = HistoricalBar(date(2026, 1, 1), 100, 101, 99, 100)
        support = PriceZone(98, 100, 99, ZoneType.SUPPORT, 4, date(2026, 1, 1), 2, "Fuerte")
        support2 = PriceZone(90, 92, 91, ZoneType.SUPPORT, 2, date(2025, 12, 1), 3, "Media")
        resistance = PriceZone(108, 110, 109, ZoneType.RESISTANCE, 3, date(2026, 1, 1), 2, "Media")
        def result(symbol, supports=(), resistances=(), status="RealTime"):
            zones = supports + resistances
            context = TechnicalContext(symbol, HistoricalPeriod.SIX_MONTHS, (bar,), 101, zones,
                supports, resistances, supports[0] if supports else None,
                resistances[0] if resistances else None, None, None, ())
            return TechnicalCheckResult(symbol, HistoricalPeriod.SIX_MONTHS, 101, context, "ok",
                                        market_data_status=status)
        failed = TechnicalCheckResult("FAIL", HistoricalPeriod.SIX_MONTHS, 101, None, "error",
                                      "ValueError: history", "Delayed")
        page = render_technical_screener((
            result("MULTI", (support, support2), (resistance,), "Frozen"),
            result("ONLYS1", (support,), (), "Delayed"),
            result("NORES", (support, support2), (), "RealTime"),
            result("NONE"), failed,
        )).decode()
        for state in ("Frozen", "Delayed", "RealTime"):
            self.assertIn(state, page)
        self.assertIn("Muy cerca", page)
        self.assertIn("Estado histórico", page)
        self.assertIn(">error<", page)
        self.assertGreaterEqual(page.count("N/D"), 8)
        self.assertEqual(page.count('class="chart-button"'), 5)
        self.assertNotIn('<svg role="img"', page)
        self.assertIn("querySelectorAll('.chart-drawer')", page)

    def test_technical_screener_is_separate_and_charts_are_lazy_and_independent(self):
        symbols = ("NVDA", "AAPL", "MSFT", "AMZN", "TSLA")
        class Provider:
            def get_underlying(self, symbol):
                from options_scanner.models import Underlying
                return Underlying(symbol, 100 + symbols.index(symbol))
            def get_historical_bars(self, symbol, period):
                start = date(2026, 1, 1)
                return tuple(HistoricalBar(start + timedelta(days=i), 100, 102, 98, 100 + i % 3)
                             for i in range(40))
        app = create_app(StubService(), technical_price_provider=Provider())
        captured = {}
        environ = {"PATH_INFO": "/technical-check", "REQUEST_METHOD": "GET", "wsgi.input": BytesIO()}
        page = b"".join(app(environ, lambda status, headers: captured.update(status=status))).decode()
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual([page.index(f'data-ticker="{symbol}"') for symbol in symbols],
                         sorted(page.index(f'data-ticker="{symbol}"') for symbol in symbols))
        self.assertEqual(page.count('class="chart-button"'), 5)
        self.assertNotIn('<svg role="img"', page)
        self.assertIn("drawer.hidden=false", page)
        charts = []
        for symbol in ("NVDA", "AAPL"):
            chart_env = {"PATH_INFO": "/technical-check/chart", "QUERY_STRING": f"ticker={symbol}",
                         "REQUEST_METHOD": "GET", "wsgi.input": BytesIO()}
            charts.append(b"".join(app(chart_env, lambda status, headers: None)).decode())
        self.assertIn("NVDA", charts[0])
        self.assertNotIn("AAPL", charts[0])
        self.assertIn("AAPL", charts[1])

    def interpretation(self, **metrics):
        market_data_status = metrics.pop("market_data_status", None)
        candidates = metrics.pop("candidates", ())
        return _interpretation(ScanResult(
            candidates, ScanMetrics(**metrics), .01,
            market_data_status=market_data_status,
        ))

    def test_get_renders_form_defaults_and_demo_mode(self):
        status, page = request(create_app(StubService()))
        self.assertEqual(status, "200 OK")
        for value in ('value="NVDA"', 'value="30"', 'value="45"', 'value="20"',
                      'value="0.15"', 'value="0.30"', "Modo demostración", "Scan"):
            self.assertIn(value, page)
        self.assertIn("Actualizar estado", page)
        self.assertIn("Datos simulados — no proceden de Interactive Brokers", page)
        self.assertIn("Modo demostración", page)

    def test_scan_loading_state_disables_button_and_tracks_elapsed_time(self):
        _, page = request(create_app(StubService()))
        self.assertIn('id="scan-status"', page)
        self.assertIn('class="spinner"', page)
        self.assertIn("Analizando universo seleccionado", page)
        self.assertIn("Tiempo transcurrido:", page)
        self.assertIn("setInterval", page)
        self.assertIn("scanButton.disabled=true", page)
        self.assertIn("Scan en curso...", page)
        self.assertIn("if(scanning)return", page)

    def test_scan_uses_fetch_and_restores_ui_on_completion(self):
        _, page = request(create_app(StubService()))
        self.assertIn("await fetch('/',", page)
        self.assertIn("new URLSearchParams(new FormData(form))", page)
        self.assertIn("Scan completado en ", page)
        self.assertIn("clearInterval(interval)", page)
        self.assertIn("scanButton.disabled=false", page)
        self.assertIn("finally{finishScan()}", page)

    def test_scan_has_safe_client_error_and_live_and_demo_messages(self):
        _, page = request(create_app(StubService()))
        self.assertIn("Consultando Interactive Brokers...", page)
        self.assertIn("Consultando datos de demostración...", page)
        self.assertIn("No se pudo completar el scan. Inténtalo de nuevo.", page)
        self.assertNotIn("error.stack", page)

    def test_four_connection_states_are_safe_and_accessible(self):
        cases = (
            ({"authenticated": True, "connected": True}, None, "connected", "Realtime"),
            ({"authenticated": False, "connected": True}, None, "login", "Login requerido"),
            (None, RuntimeError("account=SECRET cookie=SECRET"), "disconnected", "Desconectado"),
        )
        for payload, error, state, text in cases:
            with self.subTest(state=state):
                result = ibkr_connection_status(StatusTransport(payload, error))
                self.assertEqual((result["state"], result["text"]), (state, text))
                self.assertNotIn("SECRET", str(result))
        _, page = request(create_app(StubService()))
        self.assertIn("connection demo", page)
        self.assertIn("La conexión IBKR no es necesaria", page)

    def test_status_endpoint_uses_read_only_auth_check(self):
        transport = StatusTransport({"authenticated": True, "connected": True, "accountId": "SECRET"})
        app = create_app(StubService(), status_transport=transport)
        captured = {}
        environ = {"PATH_INFO": "/ibkr-status", "REQUEST_METHOD": "GET", "wsgi.input": BytesIO()}
        body = b"".join(app(environ, lambda status, headers: captured.update(status=status))).decode()
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(transport.calls, [("/iserver/auth/status", {})])
        self.assertIn("Realtime", body)
        self.assertNotIn("SECRET", body)

    def test_demo_post_and_empty_result_are_rendered(self):
        service = StubService()
        status, page = request(create_app(service), "POST", FORM + "&fake=1")
        self.assertEqual(status, "200 OK")
        self.assertTrue(service.requests[0].fake)
        self.assertIn("No hay candidatos completos", page)
        self.assertIn("Resumen del scan", page)

    def test_complete_candidate_and_unavailable_values_are_rendered(self):
        candidate = PutScanCandidate(
            "NVDA", date(2026, 9, 24), 35, 80, 100, .20, 1, 1.2, -.2,
            None, -.04, .08, .30, 100, "RpB (RealTime)",
        )
        result = ScanResult((candidate,), ScanMetrics(considered=1, complete=1), .02)
        status, page = request(create_app(StubService(result=result)), "POST", FORM + "&fake=1")
        self.assertEqual(status, "200 OK")
        self.assertIn("2026-09-24", page)
        self.assertIn("20.00 %", page)
        self.assertIn("RpB (RealTime)", page)
        self.assertIn("N/D", page)
        self.assertIn("$100.00", page)
        self.assertIn("Detalles técnicos", page)

    def test_candidate_context_is_compact_accessible_and_does_not_change_ranking(self):
        zone=PriceZone(79,81,80,ZoneType.SUPPORT,4,date(2026,8,1),75,"fuerte")
        base=dict(ticker="NVDA",expiration=date(2026,9,24),dte=35,strike=80,
                  underlying_price=100,safety_margin=.2,bid=1,ask=1.2,delta=-.2,
                  gamma=-.01,theta=-.04,vega=.08,implied_volatility=.3,
                  open_interest=100,market_data_availability="RealTime")
        contextual=PutScanCandidate(**base,nearest_support_below=zone,
            support_position="INSIDE_SUPPORT",distance_to_support_pct=0,
            support_strength="fuerte",support_zone_label="S1",
            support_position_label="Dentro de S1",support_last_contact_sessions=12)
        other=PutScanCandidate(**{**base,"ticker":"MSFT","strike":70,"bid":.5,"ask":.7})
        before=[c.ticker for c in rank_candidates((contextual,other))]
        html=_rows(ScanResult((contextual,other),ScanMetrics(),.1))
        self.assertEqual(before,[c.ticker for c in rank_candidates((contextual,other))])
        self.assertIn("Dentro S1 · en S1 fuerte",html)
        self.assertIn("Distancia al límite de S1",html)
        self.assertIn('aria-label="Detalle técnico de NVDA, strike $80.00"',html)
        for value in ("$79.00–$81.00","4","12 sesiones","+0.00 %"):
            self.assertIn(value,html)
        self.assertIn("Sin contexto técnico",html)
        self.assertEqual(html.count("Dentro S1 · en S1 fuerte"),1)

    def test_frozen_market_data_is_explained_without_error_styling(self):
        candidate = PutScanCandidate(
            "NVDA", date(2026, 9, 24), 35, 80, 100, .20, 1, 1.2, -.2,
            -.01, -.04, .08, .30, 100, "ZBd (Frozen)",
        )
        result = ScanResult(
            (candidate,), ScanMetrics(considered=1, complete=1, market_data_frozen=1),
            .02, underlying_price=100, market_data_status="Frozen",
        )
        _, page = request(create_app(StubService(result=result)), "POST", FORM)
        self.assertIn('<span class="market-state frozen">Frozen</span>', page)
        self.assertIn("ZBd (Frozen)", page)
        self.assertGreaterEqual(page.count("Cotización congelada / última disponible"), 2)
        self.assertNotIn('<div class="error" role="alert">Frozen', page)

    def test_interpretation_explains_found_candidates_and_ranking(self):
        candidate = PutScanCandidate(
            "NVDA", date(2026, 9, 24), 35, 80, 100, .20, 1, 1.2, -.2,
            -.01, -.04, .08, .30, 100, "RealTime",
        )
        block = self.interpretation(candidates=(candidate,), complete=1)
        self.assertIn("Se encontraron 1 candidatos", block)
        self.assertIn("Ordenados por rentabilidad anualizada de la prima.", block)
        self.assertIn('class="interpretation-message success"', block)

    def test_interpretation_explains_zero_candidates_rejected_by_delta(self):
        block = self.interpretation(rejected_delta=29)
        self.assertIn("29 contratos quedaron fuera del rango de delta configurado.", block)
        self.assertIn("Puedes revisar los filtros de delta, DTE o distancia al strike", block)

    def test_interpretation_explains_zero_candidates_rejected_by_margin(self):
        block = self.interpretation(rejected_margin=202)
        self.assertIn("202 contratos fueron descartados por no alcanzar la distancia mínima al strike", block)

    def test_interpretation_explains_incomplete_contracts(self):
        block = self.interpretation(incomplete=7)
        self.assertIn("7 contratos no pudieron evaluarse completamente por market data incompleta/no disponible.", block)

    def test_interpretation_explains_partial_timeout_and_pending_count(self):
        block = self.interpretation(timed_out=True, unresolved_contracts_timeout=92,
                                    target_contracts=142, considered=50,
                                    candidates=(PutScanCandidate(
                                        "NVDA", date(2026, 9, 24), 35, 80, 100, .20, 1, 1.2, -.2,
                                        -.01, -.04, .08, .30, 100, "RealTime"),))
        self.assertIn("El scan terminó con resultados parciales", block)
        self.assertIn("1 candidato encontrado entre 50 contratos evaluados. 92 contratos objetivo no llegaron a evaluarse.", block)
        self.assertNotIn("Se encontraron 1 candidatos", block)
        for detail in ("Contratos objetivo: 142.", "Contratos resueltos: 0.",
                       "Contratos que llegaron a market data/filtros: 50.",
                       "Candidatos completos: 1.", "No resueltos por timeout: 92."):
            self.assertIn(detail, block)
        self.assertIn('class="interpretation-message warning"', block)

    def test_interpretation_warns_about_frozen_market_data(self):
        block = self.interpretation(market_data_status="Frozen")
        self.assertIn("Los datos de mercado están congelados/última cotización disponible.", block)
        self.assertIn("Los resultados pueden cambiar cuando el mercado esté activo.", block)

    def test_interpretation_has_no_realtime_warning(self):
        block = self.interpretation(market_data_status="RealTime")
        self.assertNotIn("congelados", block)
        self.assertNotIn('class="interpretation-message warning"', block)

    def test_interpretation_combines_reasons_and_discarded_summary(self):
        block = self.interpretation(
            rejected_margin=202, rejected_delta=29, incomplete=7,
            timed_out=True, unresolved_contracts_timeout=11,
        )
        self.assertIn("Ver detalles del análisis", block)
        self.assertIn("Contratos descartados", block)
        for label, count in (("Distancia mínima al strike", 202), ("|Delta| mínimo/máximo", 29),
                             ("Market data incompleta/no disponible", 7), ("No evaluados", 11)):
            self.assertIn(f"<dt>{label}</dt><dd>{count}</dd>", block)

    def test_interpretation_lists_precise_rejection_reasons_and_all_counters(self):
        discarded = DiscardedContract(
            "AEHR", date(2026, 9, 25), 80,
            ("IV 111.9600% < mínimo 112.0000%",
             "Theta short 0.121000 < mínimo 0.130000"),
        )
        block = self.interpretation(
            rejected_dte=1, rejected_margin=2, rejected_delta=3, rejected_iv=4,
            rejected_theta=5, incomplete=6, discarded_contracts=[discarded],
        )
        for label, count in (("DTE", 1), ("Distancia mínima al strike", 2),
                             ("|Delta| mínimo/máximo", 3), ("IV mínima", 4),
                             ("Theta short mínimo", 5),
                             ("Market data incompleta/no disponible", 6)):
            self.assertIn(f"<dt>{label}</dt><dd>{count}</dd>", block)
        self.assertIn(
            "AEHR · 2026-09-25 · PUT $80 · IV 111.9600% &lt; mínimo 112.0000%; "
            "Theta short 0.121000 &lt; mínimo 0.130000", block,
        )

    def test_interpretation_and_heading_sanitize_market_status(self):
        malicious = 'Frozen<script>alert("x")</script>'
        result = ScanResult((), ScanMetrics(), .01, market_data_status=malicious)
        page = create_app(StubService(result=result))
        _, rendered = request(page, "POST", FORM)
        self.assertNotIn(malicious, rendered)
        self.assertNotIn("<script>alert", rendered)
        self.assertIn("Frozen&lt;script&gt;", rendered)

    def test_invalid_parameters_are_safe(self):
        status, page = request(create_app(StubService()), "POST", FORM.replace("min_dte=30", "min_dte=x"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Revisa los parámetros", page)
        self.assertNotIn("Traceback", page)

    def test_cross_field_and_ticker_validation_messages_are_specific(self):
        cases = (
            (FORM.replace("min_dte=30", "min_dte=46"),
             "DTE mínimo no puede ser mayor que DTE máximo."),
            (FORM.replace("min_abs_delta=0.15", "min_abs_delta=0.40"),
             "|Delta| mínimo no puede ser mayor que |Delta| máximo."),
            (FORM.replace("ticker=NVDA", "ticker="),
             "Introduce al menos un ticker válido."),
            (FORM.replace("ticker=NVDA", "ticker=%24BAD"),
             "El ticker debe ser un símbolo válido."),
        )
        for body, message in cases:
            with self.subTest(message=message):
                status, page = request(create_app(StubService()), "POST", body)
                self.assertEqual(status, "400 Bad Request")
                self.assertIn(message, page)
                self.assertNotIn("Traceback", page)

    def test_empty_result_reasons_and_degraded_multi_summary_are_distinct(self):
        filtered = ScanResult((), ScanMetrics(considered=4, rejected_delta=4), .1)
        incomplete = ScanResult((), ScanMetrics(considered=4, incomplete=4), .1)
        timeout = ScanResult((), ScanMetrics(considered=2, timed_out=True,
                                             target_contracts=5,
                                             unresolved_contracts_timeout=3), .1)
        failed = "token=SECRET cookie=SECRET /private/path"
        page = render_page(multi_results=(
            ("FILTER", filtered, None), ("DATA", incomplete, None),
            ("SLOW", timeout, None), ("BAD", None, failed),
        )).decode()
        self.assertIn("0 candidatos porque ningún contrato cumplió los filtros", page)
        self.assertIn("0 candidatos porque no hubo datos suficientes", page)
        self.assertIn("Resultado parcial por timeout", page)
        self.assertIn("0 completados · 1 parciales · 2 sin candidatos · 1 error", page)
        self.assertNotIn("SECRET", page)
        self.assertNotIn("/private/path", page)

        class AllFail(StubService):
            def run(self, scan_request, **kwargs):
                raise RuntimeError(f"{scan_request.ticker} {failed}")
        _, safe_page = request(create_app(AllFail()), "POST", FORM.replace("NVDA", "BAD%2CFAIL"))
        self.assertIn("0 completados · 0 parciales · 0 sin candidatos · 2 error", safe_page)
        self.assertNotIn("SECRET", safe_page)
        self.assertNotIn("/private/path", safe_page)

    def test_gateway_and_session_errors_are_safe(self):
        cases = (
            (GatewayUnavailableError("secret payload"), "No se pudo conectar"),
            (NotAuthenticatedError("secret cookie"), "no está autenticada"),
        )
        for error, message in cases:
            with self.subTest(error=error):
                status, page = request(create_app(StubService(error=error)), "POST", FORM)
                self.assertEqual(status, "503 Service Unavailable")
                self.assertIn(message, page)
                self.assertNotIn(str(error), page)

    def test_unresolved_ticker_and_unusable_chain_are_distinct_and_safe(self):
        cases = (
            (TickerNotFoundError("ticker=SECRET"), "Ticker no resuelto."),
            (IncompleteDataError("payload=SECRET"), "Ticker sin cadena de opciones utilizable"),
        )
        for error, message in cases:
            with self.subTest(error=type(error).__name__):
                status, page = request(create_app(StubService(error=error)), "POST", FORM)
                self.assertEqual(status, "422 Unprocessable Entity")
                self.assertIn(message, page)
                self.assertNotIn("SECRET", page)

    def test_post_preserves_escaped_input(self):
        status, page = request(create_app(StubService()), "POST", FORM.replace("NVDA", "%3Cscript%3E"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn('value="&lt;script&gt;"', page)
        self.assertNotIn('value="<script>"', page)
