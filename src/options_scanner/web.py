"""Small dependency-free local WSGI interface for the read-only scanner."""

from __future__ import annotations

from html import escape
import json
import logging
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from options_scanner.ibkr import GatewayUnavailableError, IbkrError, NotAuthenticatedError
from options_scanner.scan_service import PutScanService, ScanRequest, ScanResult

logger = logging.getLogger(__name__)


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


def _rows(result: ScanResult | None) -> str:
    if result is None:
        return ""
    if not result.candidates:
        return '<tr><td colspan="18" class="empty">No hay candidatos completos para estos filtros.</td></tr>'
    rendered = []
    for c in result.candidates:
        availability = escape(str(c.market_data_availability))
        if "Frozen" in str(c.market_data_availability):
            availability = f'<span class="market-state frozen">{availability}</span><small class="market-note">Cotización congelada / última disponible</small>'
        cells = (
            _number(c.ticker), _number(c.expiration.isoformat()), _number(c.dte), _number(c.strike, 4),
            f"${c.underlying_price:,.2f}", _percent(c.safety_margin), _number(c.bid, 4),
            _number(c.ask, 4), _number(c.mid, 4), _number(c.delta, 4), _number(c.gamma, 4),
            _number(c.theta, 4), _number(c.vega, 4), _percent(c.implied_volatility),
            _number(c.open_interest), availability, _percent(c.premium_yield),
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
        ("Considerados", s.considered), ("Completos", s.complete),
        ("Rechazados por margen", s.rejected_margin), ("Rechazados por delta", s.rejected_delta),
        ("Tiempo total", f"{result.elapsed_seconds:.3f} s"),
    )
    technical = (
        ("Incompletos", s.incomplete),
        ("Contratos objetivo", s.target_contracts), ("Resueltos", s.resolved_contracts),
        ("Fallidos", s.failed_contracts), ("No resueltos por timeout", s.unresolved_contracts_timeout),
        ("Timeout", "Sí" if s.timed_out else "No"), ("Fase", s.timeout_phase or "—"),
        ("Resolución contractual", f"{phase.get('contract_resolution', 0):.3f} s"),
        ("Market data", f"{phase.get('market_data_snapshots', 0):.3f} s"),
    )
    cards = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(value))}</dd></div>" for label, value in items
    )
    details = "".join(f"<div><dt>{escape(label)}</dt><dd>{escape(str(value))}</dd></div>" for label, value in technical)
    return f'<section class="summary"><h2>Resumen del scan</h2><dl>{cards}</dl><details><summary>Detalles técnicos</summary><dl>{details}</dl></details></section>'


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


def render_page(values: dict[str, str] | None = None, result: ScanResult | None = None, error: str | None = None) -> bytes:
    v = {"ticker": "NVDA", "min_dte": "30", "max_dte": "45", "min_safety_margin": "20",
         "min_abs_delta": "0.15", "max_abs_delta": "0.30", "mode": "fake"}
    if values:
        v.update(values)
    checked = " checked" if v["mode"] == "fake" else ""
    alert = f'<div class="error" role="alert">{escape(error)}</div>' if error else ""
    table = "" if result is None else f'''<section>{_result_heading(result, v['ticker'])}<h2>Candidatos completos</h2><div class="scroll"><table><thead><tr>{''.join(f'<th>{h}</th>' for h in ('Ticker','Expiration','DTE','Strike','Underlying','Safety margin','Bid','Ask','Mid','Delta','Gamma','Theta','Vega','IV','Open interest','6509','Premium yield','Annualized yield'))}</tr></thead><tbody>{_rows(result)}</tbody></table></div></section>'''
    html = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Options Scanner</title><style>
body{{font:15px system-ui;margin:0;background:#f4f6fa;color:#182033}}main{{max-width:1500px;margin:auto;padding:2rem}}h1{{margin:0}}.note{{color:#556}}.top{{display:flex;justify-content:space-between;gap:1rem;align-items:start}}.connection{{background:white;padding:.7rem;border-radius:8px;min-width:220px}}.dot{{display:inline-block;width:.75rem;height:.75rem;border-radius:50%;background:#818895;margin-right:.4rem}}.connected .dot{{background:#198754}}.login .dot{{background:#e58a00}}.disconnected .dot{{background:#c52d36}}.demo .dot{{background:#818895}}.connection button{{font-size:.8rem;padding:.35rem .6rem;margin-top:.4rem}}.connection small{{display:block;color:#596273;margin-top:.25rem}}
form{{display:flex;flex-wrap:wrap;gap:1rem;align-items:end;background:white;padding:1.25rem;border-radius:10px;box-shadow:0 2px 8px #0001}}label{{display:grid;gap:.35rem;font-weight:600}}input{{padding:.55rem;border:1px solid #aab3c5;border-radius:5px;width:9rem}}button{{background:#2358d5;color:white;border:0;border-radius:5px;padding:.7rem 1.4rem;font-weight:700;cursor:pointer}}button:disabled{{cursor:not-allowed;opacity:.65}}.mode{{display:flex;align-items:center;gap:.4rem}}.mode input{{width:auto}}
.scan-status{{display:flex;align-items:center;gap:.8rem;margin:1rem 0;padding:1rem;background:#eaf1ff;border-left:4px solid #2358d5;border-radius:5px}}.scan-status[hidden]{{display:none}}.scan-status strong,.scan-status span{{display:block}}.spinner{{width:1.25rem;height:1.25rem;border:3px solid #b9c9ed;border-top-color:#2358d5;border-radius:50%;animation:spin .8s linear infinite;flex:none}}@keyframes spin{{to{{transform:rotate(360deg)}}}}.completion{{margin:1rem 0;padding:.8rem;background:#e9f7ef;border-left:4px solid #198754}}.error{{margin:1rem 0;padding:1rem;background:#fff0f0;border-left:4px solid #c22}}.demo-label{{background:#eceff3;padding:.65rem;border-left:4px solid #818895;font-weight:700}}section{{margin-top:1.5rem}}.result-head{{background:white;padding:1rem;border-radius:8px;display:flex;gap:1rem;align-items:baseline;flex-wrap:wrap}}.result-head strong{{font-size:1.7rem}}.market-state{{font-weight:700}}.market-state.frozen{{color:#6b5200;background:#fff2bd;border-radius:4px;padding:.15rem .35rem}}.market-note{{display:block;color:#665b38;font-weight:400;white-space:normal}}.scroll{{overflow:auto}}table{{border-collapse:collapse;background:white;width:100%;white-space:nowrap}}th,td{{padding:.65rem;border-bottom:1px solid #dde2ea;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#263451;color:white}}.na,.empty{{color:#788190;font-style:italic}}.summary dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.75rem}}.summary dl div{{background:white;padding:.8rem;border-radius:7px}}dt{{color:#596273}}dd{{font-size:1.15rem;font-weight:700;margin:.25rem 0 0}}details{{margin-top:1rem}}summary{{cursor:pointer;font-weight:700}}
</style></head><body><main><div class="top"><div><h1>PUT Options Scanner</h1><p class="note">Análisis local de solo lectura. No ejecuta ni ofrece operaciones de trading.</p></div><div id="connection" class="connection"><span class="dot"></span><strong>Comprobando IBKR…</strong><small>Comprobación no bloqueante.</small><button type="button" id="refresh-status">Actualizar estado</button></div></div><form method="post">
<label>Ticker<input name="ticker" value="{escape(v['ticker'])}" required></label><label>Min DTE<input type="number" name="min_dte" min="0" value="{escape(v['min_dte'])}" required></label><label>Max DTE<input type="number" name="max_dte" min="0" value="{escape(v['max_dte'])}" required></label>
<label>Margen mínimo (%)<input type="number" name="min_safety_margin" min="0" max="100" step="0.01" value="{escape(v['min_safety_margin'])}" required></label><label>|Delta| mínima<input type="number" name="min_abs_delta" min="0" max="1" step="0.01" value="{escape(v['min_abs_delta'])}" required></label><label>|Delta| máxima<input type="number" name="max_abs_delta" min="0" max="1" step="0.01" value="{escape(v['max_abs_delta'])}" required></label>
<label class="mode"><input id="fake-mode" type="checkbox" name="fake" value="1"{checked}> Modo demostración</label><button id="scan-button" type="submit">Scan</button></form><p id="demo-label" class="demo-label"{' hidden' if not checked else ''}>Datos simulados — no proceden de Interactive Brokers</p><div id="scan-status" class="scan-status" role="status" aria-live="polite" hidden><span class="spinner" aria-hidden="true"></span><div><strong id="scan-title"></strong><span id="scan-source"></span><span>Tiempo transcurrido: <b id="scan-timer">00:00</b></span></div></div><div id="scan-output" aria-live="polite">{alert}{table}{_summary(result)}</div><script>
const box=document.querySelector('#connection'),fake=document.querySelector('#fake-mode'),label=document.querySelector('#demo-label'),form=document.querySelector('form'),scanButton=document.querySelector('#scan-button'),scanStatus=document.querySelector('#scan-status'),scanOutput=document.querySelector('#scan-output'),timer=document.querySelector('#scan-timer');let scanning=false,interval;
function elapsed(seconds){{const value=Math.floor(seconds);return String(Math.floor(value/60)).padStart(2,'0')+':'+String(value%60).padStart(2,'0')}}
function finishScan(){{scanning=false;clearInterval(interval);scanStatus.hidden=true;scanButton.disabled=false;scanButton.textContent='Scan'}}
form.addEventListener('submit',async event=>{{event.preventDefault();if(scanning)return;scanning=true;scanButton.disabled=true;scanButton.textContent='Scan en curso...';scanOutput.replaceChildren();scanStatus.hidden=false;document.querySelector('#scan-title').textContent='Escaneando '+form.elements.ticker.value.trim().toUpperCase()+'...';document.querySelector('#scan-source').textContent=fake.checked?'Consultando datos de demostración...':'Consultando Interactive Brokers...';const started=performance.now();timer.textContent='00:00';interval=setInterval(()=>timer.textContent=elapsed((performance.now()-started)/1000),250);try{{const response=await fetch('/',{{method:'POST',body:new URLSearchParams(new FormData(form)),headers:{{'X-Requested-With':'fetch'}}}}),html=await response.text(),doc=new DOMParser().parseFromString(html,'text/html'),output=doc.querySelector('#scan-output');if(!output)throw new Error('invalid response');scanOutput.replaceChildren(...Array.from(output.childNodes).map(node=>document.importNode(node,true)));const seconds=(performance.now()-started)/1000;if(response.ok){{const done=document.createElement('p');done.className='completion';done.textContent='Scan completado en '+seconds.toFixed(1)+' s';scanOutput.prepend(done)}}else if(!scanOutput.querySelector('[role="alert"]'))throw new Error('unsafe response')}}catch(error){{scanOutput.replaceChildren();const alert=document.createElement('div');alert.className='error';alert.setAttribute('role','alert');alert.textContent='No se pudo completar el scan. Inténtalo de nuevo.';scanOutput.append(alert)}}finally{{finishScan()}}}});
function demoStatus(){{box.className='connection demo';box.querySelector('strong').textContent='Modo demostración';box.querySelector('small').textContent='La conexión IBKR no es necesaria para este scan.';}}
async function refresh(){{if(fake.checked){{demoStatus();return}} box.className='connection';box.querySelector('strong').textContent='Comprobando IBKR…';try{{const r=await fetch('/ibkr-status',{{cache:'no-store'}}),s=await r.json();box.className='connection '+s.state;box.querySelector('strong').textContent=s.text;box.querySelector('small').textContent=s.message}}catch(e){{box.className='connection disconnected';box.querySelector('strong').textContent='IBKR desconectado';box.querySelector('small').textContent='No se pudo comprobar Client Portal Gateway.'}}}}
fake.addEventListener('change',()=>{{label.hidden=!fake.checked;refresh()}});document.querySelector('#refresh-status').addEventListener('click',refresh);refresh();
</script></main></body></html>'''
    return html.encode()


def create_app(service: PutScanService | None = None, *, base_url: str = "https://localhost:5000/v1/api", status_transport: object | None = None):
    scanner = service or PutScanService()
    transport = status_transport or __import__("options_scanner.ibkr", fromlist=["ClientPortalTransport"]).ClientPortalTransport(base_url, allow_insecure_tls=True, timeout=2.0)
    def application(environ, start_response):
        if environ.get("PATH_INFO") == "/ibkr-status" and environ.get("REQUEST_METHOD") == "GET":
            body = json.dumps(ibkr_connection_status(transport)).encode()
            start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")])
            return [body]
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
                result = scanner.run(request, base_url=base_url, allow_insecure_tls=True)
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
