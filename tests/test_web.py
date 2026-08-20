from io import BytesIO
from unittest import TestCase

from options_scanner.ibkr import GatewayUnavailableError, NotAuthenticatedError
from options_scanner.scan_service import ScanMetrics, ScanResult
from options_scanner.scanner import PutScanCandidate
from options_scanner.web import create_app
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


class WebTest(TestCase):
    def test_get_renders_form_defaults_and_demo_mode(self):
        status, page = request(create_app(StubService()))
        self.assertEqual(status, "200 OK")
        for value in ('value="NVDA"', 'value="30"', 'value="45"', 'value="20"',
                      'value="0.15"', 'value="0.30"', "Modo demostración", "Scan"):
            self.assertIn(value, page)

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
        self.assertNotIn("<script>", page)
