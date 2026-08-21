from io import BytesIO
from unittest import TestCase

from options_scanner.ibkr import GatewayUnavailableError, NotAuthenticatedError
from options_scanner.scan_service import ScanMetrics, ScanResult
from options_scanner.scanner import PutScanCandidate
from options_scanner.web import _interpretation, create_app, ibkr_connection_status
from datetime import date


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
        self.assertIn("Escaneando '+form.elements.ticker.value", page)
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
        block = self.interpretation(timed_out=True, unresolved_contracts_timeout=11)
        self.assertIn("El scan terminó con resultados parciales", block)
        self.assertIn("11 contratos quedaron pendientes.", block)
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
