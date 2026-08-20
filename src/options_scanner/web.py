"""Small dependency-free local WSGI interface for the read-only scanner."""

from __future__ import annotations

from html import escape
import logging
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from options_scanner.ibkr import GatewayUnavailableError, IbkrError, NotAuthenticatedError
from options_scanner.scan_service import PutScanService, ScanRequest, ScanResult

logger = logging.getLogger(__name__)


def _number(value: object, digits: int = 2) -> str:
    if value is None:
        return '<span class="na">N/D</span>'
    return escape(f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def _percent(value: float | None) -> str:
    return '<span class="na">N/D</span>' if value is None else f"{value * 100:.2f} %"


def _rows(result: ScanResult | None) -> str:
    if result is None:
        return ""
    if not result.candidates:
        return '<tr><td colspan="18" class="empty">No hay candidatos completos para estos filtros.</td></tr>'
    rendered = []
    for c in result.candidates:
        cells = (
            _number(c.ticker), _number(c.expiration.isoformat()), _number(c.dte), _number(c.strike, 4),
            _number(c.underlying_price, 4), _percent(c.safety_margin), _number(c.bid, 4),
            _number(c.ask, 4), _number(c.mid, 4), _number(c.delta, 4), _number(c.gamma, 4),
            _number(c.theta, 4), _number(c.vega, 4), _percent(c.implied_volatility),
            _number(c.open_interest), _number(c.market_data_availability), _percent(c.premium_yield),
            _percent(c.annualized_premium_yield),
        )
        rendered.append("<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>")
    return "".join(rendered)


def _summary(result: ScanResult | None) -> str:
    if result is None:
        return ""
    s = result.summary
    phase = s.phase_seconds
    items = (
        ("Considerados", s.considered), ("Completos", s.complete), ("Incompletos", s.incomplete),
        ("Rechazados por margen", s.rejected_margin), ("Rechazados por delta", s.rejected_delta),
        ("Contratos objetivo", s.target_contracts), ("Resueltos", s.resolved_contracts),
        ("Fallidos", s.failed_contracts), ("No resueltos por timeout", s.unresolved_contracts_timeout),
        ("Timeout", "Sí" if s.timed_out else "No"), ("Fase", s.timeout_phase or "—"),
        ("Tiempo total", f"{result.elapsed_seconds:.3f} s"),
        ("Resolución contractual", f"{phase.get('contract_resolution', 0):.3f} s"),
        ("Market data", f"{phase.get('market_data_snapshots', 0):.3f} s"),
    )
    return '<section class="summary"><h2>Resumen del scan</h2><dl>' + "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(value))}</dd></div>" for label, value in items
    ) + "</dl></section>"


def render_page(values: dict[str, str] | None = None, result: ScanResult | None = None, error: str | None = None) -> bytes:
    v = {"ticker": "NVDA", "min_dte": "30", "max_dte": "45", "min_safety_margin": "20",
         "min_abs_delta": "0.15", "max_abs_delta": "0.30", "mode": "fake"}
    if values:
        v.update(values)
    checked = " checked" if v["mode"] == "fake" else ""
    alert = f'<div class="error" role="alert">{escape(error)}</div>' if error else ""
    table = "" if result is None else f'''<section><h2>Candidatos completos</h2><div class="scroll"><table><thead><tr>{''.join(f'<th>{h}</th>' for h in ('Ticker','Expiration','DTE','Strike','Underlying','Safety margin','Bid','Ask','Mid','Delta','Gamma','Theta','Vega','IV','Open interest','6509','Premium yield','Annualized yield'))}</tr></thead><tbody>{_rows(result)}</tbody></table></div></section>'''
    html = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Options Scanner</title><style>
body{{font:15px system-ui;margin:0;background:#f4f6fa;color:#182033}}main{{max-width:1500px;margin:auto;padding:2rem}}h1{{margin-bottom:.25rem}}.note{{color:#556}}
form{{display:flex;flex-wrap:wrap;gap:1rem;align-items:end;background:white;padding:1.25rem;border-radius:10px;box-shadow:0 2px 8px #0001}}label{{display:grid;gap:.35rem;font-weight:600}}input{{padding:.55rem;border:1px solid #aab3c5;border-radius:5px;width:9rem}}button{{background:#2358d5;color:white;border:0;border-radius:5px;padding:.7rem 1.4rem;font-weight:700;cursor:pointer}}.mode{{display:flex;align-items:center;gap:.4rem}}.mode input{{width:auto}}
.error{{margin:1rem 0;padding:1rem;background:#fff0f0;border-left:4px solid #c22}}section{{margin-top:1.5rem}}.scroll{{overflow:auto}}table{{border-collapse:collapse;background:white;width:100%;white-space:nowrap}}th,td{{padding:.65rem;border-bottom:1px solid #dde2ea;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#263451;color:white}}.na,.empty{{color:#788190;font-style:italic}}.summary dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.75rem}}.summary dl div{{background:white;padding:.8rem;border-radius:7px}}dt{{color:#596273}}dd{{font-size:1.15rem;font-weight:700;margin:.25rem 0 0}}
</style></head><body><main><h1>PUT Options Scanner</h1><p class="note">Análisis local de solo lectura. No ejecuta ni ofrece operaciones de trading.</p>{alert}<form method="post">
<label>Ticker<input name="ticker" value="{escape(v['ticker'])}" required></label><label>Min DTE<input type="number" name="min_dte" min="0" value="{escape(v['min_dte'])}" required></label><label>Max DTE<input type="number" name="max_dte" min="0" value="{escape(v['max_dte'])}" required></label>
<label>Margen mínimo (%)<input type="number" name="min_safety_margin" min="0" max="100" step="0.01" value="{escape(v['min_safety_margin'])}" required></label><label>|Delta| mínima<input type="number" name="min_abs_delta" min="0" max="1" step="0.01" value="{escape(v['min_abs_delta'])}" required></label><label>|Delta| máxima<input type="number" name="max_abs_delta" min="0" max="1" step="0.01" value="{escape(v['max_abs_delta'])}" required></label>
<label class="mode"><input type="checkbox" name="fake" value="1"{checked}> Modo demostración</label><button type="submit">Scan</button></form>{table}{_summary(result)}</main></body></html>'''
    return html.encode()


def create_app(service: PutScanService | None = None):
    scanner = service or PutScanService()
    def application(environ, start_response):
        if environ.get("PATH_INFO", "/") != "/" or environ.get("REQUEST_METHOD") not in ("GET", "POST"):
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Not found"]
        values: dict[str, str] = {}
        result = None
        error = None
        status = "200 OK"
        if environ["REQUEST_METHOD"] == "POST":
            try:
                size = min(int(environ.get("CONTENT_LENGTH") or 0), 8192)
                data = parse_qs(environ["wsgi.input"].read(size).decode("utf-8"), keep_blank_values=True)
                values = {key: entries[0] for key, entries in data.items()}
                values["mode"] = "fake" if values.get("fake") == "1" else "live"
                request = ScanRequest(
                    ticker=values.get("ticker", ""), min_dte=int(values.get("min_dte", "")),
                    max_dte=int(values.get("max_dte", "")),
                    min_safety_margin=float(values.get("min_safety_margin", "")) / 100,
                    min_abs_delta=float(values.get("min_abs_delta", "")),
                    max_abs_delta=float(values.get("max_abs_delta", "")), fake=values["mode"] == "fake",
                )
                result = scanner.run(request, allow_insecure_tls=True)
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
        body = render_page(values, result, error)
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
