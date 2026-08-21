from io import BytesIO
from unittest import TestCase

from options_scanner.ibkr import GatewayUnavailableError, NotAuthenticatedError
from options_scanner.scan_service import ScanMetrics, ScanResult
from options_scanner.scanner import PutScanCandidate
from options_scanner.web import (_interpretation, _rows, create_app, ibkr_connection_status, parse_tickers,
                                 render_technical_screener, resolve_universe)
from options_scanner.historical import HistoricalBar
from datetime import timedelta
from options_scanner.scanner import rank_candidates
from options_scanner.technical_analysis import PriceZone, ZoneType
from options_scanner.technical_context import TechnicalContext
from options_scanner.technical_check import TechnicalCheckResult
from options_scanner.historical import HistoricalPeriod
from datetime import date
from options_scanner.models import User
from options_scanner.workspace import UserWorkspaceStore


def request(app, method="GET", body=""):
    encoded = body.encode()
    captured = {}
    environ = {"PATH_INFO": "/", "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(encoded)),
               "wsgi.input": BytesIO(encoded)}
    def start_response(status, headers):
        captured["status"], captured["headers"] = status, headers
    output = b"".join(app(environ, start_response)).decode()
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
        self.assertIn("solo en memoria", page)

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

        manual_status, manual_page = request(create_app(manual_service), "POST", manual)
        watchlist_status, watchlist_page = request(
            create_app(watchlist_service, watchlists={"core": ("nvda", "SPY", "nvda", "qqq")}),
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
        self.assertEqual([item.ticker for item in service.requests], ["NVDA", "BAD", "SPY"])
        self.assertEqual(page.count('class="ticker-detail"'), 3)
        self.assertIn("Screener multi-ticker", page)
        self.assertIn("Delayed", page)
        self.assertIn("No se pudo completar este ticker.", page)
        self.assertNotIn("secret payload", page)
        self.assertNotIn('<svg role="img"', page)
        self.assertNotIn('<details class="ticker-detail" open', page)

    def test_multi_ticker_concurrency_is_configurable_and_capped_at_two(self):
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
        request(create_app(service, ticker_workers=2), "POST", FORM.replace("NVDA", "AAOI+AEHR+COHR+LITE"))
        self.assertEqual(service.maximum, 2)
        with self.assertRaises(ValueError):
            create_app(service, ticker_workers=3)

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
            ({"authenticated": True, "connected": True}, None, "connected", "IBKR conectado"),
            ({"authenticated": False, "connected": True}, None, "login", "IBKR requiere login"),
            (None, RuntimeError("account=SECRET cookie=SECRET"), "disconnected", "IBKR desconectado"),
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
        self.assertIn("IBKR conectado", body)
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
        self.assertIn("Puedes revisar los filtros de delta, DTE o margen", block)

    def test_interpretation_explains_zero_candidates_rejected_by_margin(self):
        block = self.interpretation(rejected_margin=202)
        self.assertIn("202 contratos fueron descartados por no alcanzar el margen", block)

    def test_interpretation_explains_incomplete_contracts(self):
        block = self.interpretation(incomplete=7)
        self.assertIn("7 contratos no pudieron evaluarse completamente por falta de bid, ask o delta.", block)

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
        for label, count in (("Margen", 202), ("Delta", 29), ("Datos incompletos", 7), ("Timeout", 11)):
            self.assertIn(f"<dt>{label}</dt><dd>{count}</dd>", block)

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

    def test_post_preserves_escaped_input(self):
        status, page = request(create_app(StubService()), "POST", FORM.replace("NVDA", "%3Cscript%3E"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn('value="&lt;script&gt;"', page)
        self.assertNotIn('value="<script>"', page)
