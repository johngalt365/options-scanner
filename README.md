# Options Scanner

Base modular para una aplicación multiusuario de análisis de opciones. El MVP
busca PUTs de NVDA por DTE, margen de seguridad y delta. No implementa órdenes,
login web, base de datos, Docker ni ejecución de operaciones.

## Arquitectura

```text
src/options_scanner/
├── models.py       # modelos inmutables de dominio y modelos multiusuario
├── filters.py      # reglas puras, independientes de proveedores
├── market_data.py  # puerto MarketDataProvider y FakeMarketDataProvider
├── scanner.py      # servicio que orquesta cualquier proveedor
├── scan_service.py # caso de uso compartido por CLI y web
├── web.py           # interfaz local WSGI sin dependencias externas
├── ibkr.py         # adaptador IBKR y transporte HTTP inyectable
├── ibkr_diagnostic.py # diagnóstico CLI de Client Portal Gateway
├── workspace.py    # espacios en memoria aislados por usuario
└── brokers.py      # puertos futuros de conexión por usuario
```

`MarketDataProvider` es el puerto de solo lectura para obtener el subyacente y
las cotizaciones de opciones. El scanner depende exclusivamente de ese
protocolo. `FakeMarketDataProvider` ofrece un snapshot determinista de NVDA y
varias PUTs; `IbkrMarketDataProvider` transforma respuestas de la IBKR Web API
mediante un `IbkrTransport` inyectado y comprobable con mocks. El transporte
HTTP no almacena credenciales ni inicia sesiones: reutiliza la
sesión que el usuario haya abierto externamente en Client Portal Gateway.

`Underlying`, `OptionContract` y `MarketData` son modelos internos inmutables.
La cotización contempla bid, ask, delta, gamma, theta, vega, volatilidad
implícita (IV), volumen y open interest. Ni estos modelos, ni los filtros, ni el
scanner conocen detalles de IBKR.

## Multiusuario

`User`, `Watchlist`, `StrategyParameters` y `SavedScanResult` conservan un
propietario explícito. `UserWorkspaceStore` mantiene espacios separados en
memoria. Los datos de mercado son deliberadamente globales y todavía no están
asociados a usuarios ni cuentas. Los perfiles futuros de broker sí pertenecen
a un usuario, pero esta fase no abre sesiones ni guarda credenciales.

## Reglas por defecto

- DTE entre 30 y 45 días, inclusive;
- margen de seguridad mínimo del 20 %, calculado como
  `(precio_actual - strike) / precio_actual`;
- valor absoluto de delta entre 0,15 y 0,30, inclusive.

Los datos fake son demostrativos y no constituyen datos reales ni una
recomendación financiera.

## Instalación y uso

Requiere Python 3.11 o posterior y no tiene dependencias externas de ejecución.

```bash
python -m pip install -e .
python -m options_scanner.example
python -m unittest discover -s tests -v
```

## Interfaz web local

La interfaz usa únicamente la biblioteca estándar de Python y el mismo flujo
productivo de solo lectura del scanner. No almacena credenciales, cookies ni
sesiones, y no contiene controles para operar. Arráncala desde la raíz:

```bash
python -m pip install -e .
python -m options_scanner.web
```

### Desarrollo local

Con un entorno virtual ya creado en `.venv`, se puede iniciar la versión actual
desde cualquier directorio con:

```bash
./run_web.sh
```

El script activa `.venv`, detiene de forma segura una instancia anterior que
esté escuchando en `127.0.0.1:8000` y arranca la interfaz actual. El comando
manual continúa disponible:

```bash
PYTHONPATH=src python -m options_scanner.web
```

Abre exactamente <http://127.0.0.1:8000/>. El formulario comienza en **modo
demostración**, que usa el proveedor fake y no requiere IBKR. Para datos reales,
desmarca «Modo demostración», inicia Client Portal Gateway por separado en
`https://localhost:5000`, autentícate directamente en Gateway y pulsa `Scan`.
La aplicación solo escucha en loopback (`127.0.0.1`) y usa el Gateway en modo
read-only; el certificado local self-signed se acepta únicamente para esa
conexión local. Si Gateway no responde o perdió la autenticación, la página
muestra un mensaje seguro y no expone el traceback ni respuestas internas.

El campo **Tickers** admite símbolos separados por comas o espacios, elimina
duplicados preservando su orden y muestra varios resultados en un screener
comparativo compacto. Cada detalle se abre bajo demanda y su gráfico permanece
cerrado inicialmente. Los scans de subyacentes son secuenciales por defecto
(`ticker_workers=1` en `create_app`) y pueden configurarse hasta un máximo de
dos; esto no cambia los workers internos de resolución contractual. Un fallo se
confina a su fila. Como la implementación continúa siendo WSGI estándar y no
añade streaming ni dependencias, el progreso identifica el lote activo pero la
tabla se incorpora al terminar la respuesta completa, no fila a fila.

## Scanner real de venta de PUTs

El primer scanner ejecutable está limitado por defecto a NVDA, es de **solo
lectura** y no contiene ninguna operación de órdenes. Obtiene el subyacente,
descubre los vencimientos exactos confirmados por `secdef/info`, limita DTE y
margen antes de pedir snapshots y filtra finalmente por delta absoluta:

```bash
PYTHONPATH=src python -m options_scanner.scan_puts \
  --ticker NVDA --min-dte 30 --max-dte 45 \
  --min-safety-margin 0.20 --min-abs-delta 0.15 \
  --max-abs-delta 0.30 --insecure
```

La tabla muestra vencimiento, DTE, strike, subyacente, margen, bid/ask/mid,
griegas, IV, open interest, el estado 6509, `premium_yield` (prima mid / capital
`strike × 100`) y su anualización. El ranking se ordena por rentabilidad
anualizada descendente. Si falta bid, ask o delta, el contrato aparece como
incompleto en una tabla separada y nunca se inventa un mid. IV, theta y open
interest se presentan pero no son filtros.

El scanner real reúne primero todos los contratos confirmados que pasan DTE y
margen, y solicita sus snapshots en batches (50 conids por defecto). Cada batch
hace un pre-flight y como máximo dos lecturas posteriores, fusionando los
fields parciales de cada conid. Solo bid, ask y delta son esenciales: gamma,
theta, vega, IV y open interest se incorporan si llegan, pero no prolongan la
espera. Se puede ajustar el comportamiento con `--batch-size`,
`--snapshot-attempts` y `--scan-timeout`. Dentro del límite global se reservan
10 segundos para adquisición mediante `--market-data-timeout`: descubrimiento
y `secdef/info` no pueden tomar prestada esa reserva, por lo que los snapshots
no comienzan con un deadline ya agotado. `--progress` muestra el avance real de
resolución y `Market data batch X/Y`; al final añade tiempos aproximados para
subyacente, expiraciones/strikes, filtros DTE/margen, confirmación contractual,
snapshots y filtrado/ranking. El resumen identifica además la fase que agotó
su presupuesto con `timeout_phase`, y `--progress`/`--verbose` muestran solo
contadores por endpoint (nunca payloads, cabeceras, cookies o credenciales).

Las respuestas validadas de `secdef/info` y las listas de strikes se reutilizan
durante la vida del proveedor con claves de subyacente, símbolo, mes y strike.
Una entrada contractual solo llega a esa caché después de ejecutar y validar
estrictamente `secdef/info`; la reutilización no convierte una respuesta de
búsqueda mensual en una confirmación.

### Resolución contractual: investigación y medición

El flujo sigue la secuencia documentada por IBKR Client Portal Web API:
[`/iserver/secdef/search`](https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#security-definition-search)
descubre el subyacente y los *meses* disponibles;
[`/iserver/secdef/strikes`](https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#security-definition-strikes)
devuelve las listas de strikes call/put de un mes; y
[`/iserver/secdef/info`](https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#security-definition-information)
recibe un único `strike` y puede devolver una lista con varios contratos (por
ejemplo, varios vencimientos del mismo strike dentro del mes). Por ello el
scanner ya aprovecha una sola respuesta `info` para **todos los vencimientos de
ese strike**. La documentación no define una petición de múltiples strikes en
`info`, y ni `search` ni `strikes` devuelven la identidad completa y exacta del
contrato. No se agrupan más strikes ni se usan esas respuestas como sustituto:
cada `conid` conserva la validación inequívoca de símbolo, `OPT`, PUT, strike y
vencimiento exactos antes de market data.

Las optimizaciones seguras son, por tanto, evitar duplicados, reutilizar la
caché ya validada y mantener varias llamadas independientes en vuelo. El
default de `--contract-workers` es 8 (configurable, limitado siempre a 16),
elegido con el benchmark local de latencia simulada descrito en
`tests/test_contract_resolution_performance.py`; no representa una medición de
la latencia ni de los límites del servicio real de IBKR. En timeout se atienden
primero los meses cercanos al centro del DTE solicitado y, dentro de cada mes,
los strikes más próximos al límite de margen. El criterio usa solo información
previa a market data, es determinista y no descarta el resto si hay tiempo.

El resumen separa **Contratos objetivo** (strikes únicos que requerían
resolución) de **Considerados** (contratos exactos que llegaron a market data y
filtros finales). También publica strikes candidatos, llamadas reales a
`secdef/info`, cache hits, deduplicados, validaciones correctas/fallidas,
latencia media/p50/p95 aproximada y concurrencia máxima. Son exclusivamente
contadores y tiempos: no se registran URLs completas, parámetros, headers,
cookies ni payloads.

Para probar exactamente el mismo comando sin Gateway ni datos reales:

```bash
PYTHONPATH=src python -m options_scanner.scan_puts --ticker NVDA --fake
```

## Diagnóstico local de IBKR

El diagnóstico es de **solo lectura** y no envía órdenes. Antes de ejecutarlo,
inicia Client Portal Gateway por separado, completa allí el login de IBKR y
mantén esa sesión activa. Desde la raíz del repositorio:

```bash
PYTHONPATH=src python -m options_scanner.ibkr_diagnostic --insecure-tls
```

`--insecure-tls` acepta explícitamente el certificado local/self-signed y solo
debe usarse en desarrollo. Sin esa opción se verifica TLS normalmente. La URL
predeterminada es `https://localhost:5000/v1/api`; se puede cambiar, así como el
símbolo, mes y número de contratos:

```bash
PYTHONPATH=src python -m options_scanner.ibkr_diagnostic \
  --base-url https://localhost:5000/v1/api \
  --symbol NVDA --expiration 2026-09 --contracts 3 --insecure-tls --verbose
```

El comando localiza el subyacente, muestra su precio y los meses disponibles,
selecciona un vencimiento, elige strikes PUT cercanos al precio, resuelve sus
contratos y muestra bid, ask, delta, theta, IV y open interest. Los snapshots de
opciones usan los field IDs 84, 86, 7308, 7310, 7633 y 7638, respectivamente,
y realizan un pre-flight seguido de reintentos porque la entrega es asíncrona.
El field 6509 (`Market Data Availability`) se solicita y muestra literalmente
por contrato, junto con su estado RealTime/Delayed/Frozen/Frozen-Delayed/Not
Subscribed, la marca `incomplete` y la disponibilidad de book. Por tanto, un
bid/ask ausente se describe como dato parcial o no disponible, pero no se
interpreta por sí solo como falta de suscripción.
Siguiendo el prerrequisito de Client Portal Web API para derivados, el flujo
llama primero a `/iserver/secdef/search` para el subyacente; solo después
consulta strikes, resuelve los contratos y solicita sus snapshots.

La identidad de una opción no queda determinada por un mes ni por un `conid`
devuelto durante una resolución mensual. El flujo productivo conserva
vencimientos exactos `YYYY-MM-DD` y, para cada strike, acepta un `conid` solo
después de que `/iserver/secdef/info` confirme exactamente `symbol`,
`secType=OPT`, `right=P`, `strike` y `maturityDate`. Los candidatos discordantes
se descartan sin mezclarlos con los válidos y únicamente entonces se solicita
`/iserver/marketdata/snapshot`. Los valores Frozen (`Z`) y Frozen-Delayed (`Y`)
de `6509` se conservan en el modelo; no se rechazan automáticamente.

En particular, `ZBd` se interpreta como **Frozen** (`Z`) con book disponible
(`B`); `d` es otra marca del field compuesto y no convierte el feed en
Delayed. Frozen describe el tipo de datos que IBKR está entregando, mientras
que `B` indica disponibilidad del book: ninguna de esas marcas garantiza que
bid, ask o las griegas estén presentes en una entrega asíncrona concreta.

La documentación oficial actual de Client Portal Web API documenta para
`/iserver/marketdata/snapshot` una primera petición que inicia/prepara la
suscripción y posteriores peticiones para recibir los fields. No documenta en
ese endpoint un parámetro ni una operación Client Portal equivalente a
`reqMarketDataType` de TWS API para seleccionar explícitamente live/frozen.
Por ello este proyecto conserva el pre-flight read-only ya implementado y no
inventa una llamada para cambiar el tipo de market data. El tipo efectivo se
diagnostica exclusivamente con `6509`; permisos y suscripciones siguen siendo
configuración de la cuenta/usuario y no se modifican desde el scanner.

El resumen productivo separa la resolución contractual en objetivo, resueltos,
fallidos, no resueltos por timeout y duplicados evitados. Para cada grupo se
mantiene la invariante `resueltos + fallidos + no_resueltos_timeout == objetivo`.
También cruza la completitud esencial (bid+ask y delta) con las categorías
RealTime, Frozen, Delayed, Not Subscribed y desconocida comunicadas por `6509`.

Que una respuesta incluya `31` (last), `7308` (delta) y `7310` (theta), pero no
bid/ask, IV u open interest, no prueba por sí solo un problema con OPRA: Client
Portal entrega el snapshot de forma asíncrona y cada field puede estar ausente.
El diagnóstico conserva las entregas parciales entre reintentos y presenta
`6509` por contrato para distinguir el estado que comunica IBKR sin inferirlo
de los fields ausentes. Open interest (`7638`) tampoco debe confundirse con
`7698`, que es Last Yield para bonos.

Referencia revisada: [Client Portal Web API v1, endpoints y campos de market
data de IBKR](https://ibkrcampus.com/campus/ibkr-api-page/cpapi-v1/).
Un `N/D` lleva su causa: pendiente después del pre-flight, campo marcado como no
disponible por IBKR o respuesta parcial. La falta de suscripción/permisos genera
un error específico. `--verbose` registra por intento solo conid, field IDs y
valores recibidos (y clasifica los mensajes de IBKR), sin cabeceras, cookies ni
credenciales. El proyecto y sus tests nunca necesitan hacer una llamada real:
`IbkrMarketDataProvider` recibe un transporte inyectable.

### Diagnóstico profundo de un solo contrato

Para observar la evolución temporal de **un único PUT** sin cambiar el scanner
ni sus suscripciones, indica mes, strike y, si el mes contiene varios
vencimientos, el día exacto. El modo ejecuta primero `/iserver/secdef/search`,
consulta `/iserver/secdef/strikes` y exige que `/iserver/secdef/info` confirme
el símbolo, tipo, right PUT, strike y vencimiento antes de usar el conid en
market data. Si hay varios contratos compatibles y no se da el día exacto, el
diagnóstico rechaza la ambigüedad en vez de escoger uno. Después hace el
pre-flight y cinco snapshots adicionales con esperas crecientes (0,25; 0,5; 1;
2 y 3 s):

```bash
PYTHONPATH=src python -m options_scanner.ibkr_diagnostic \
  --deep --symbol NVDA --expiration 2026-09 --maturity 2026-09-18 \
  --strike 100 --insecure-tls
```

La salida de confirmación contiene únicamente los atributos seguros `conid`,
`symbol`, `secType`, `exchange`, `listingExchange`, `right`, `strike`,
`maturityDate`, `multiplier`, `tradingClass` y `validExchanges`. Para cada
entrega muestra exclusivamente los fields
`31,84,86,6509,7308,7309,7310,7311,7633,7635,7638`. Indica
por separado `field no recibido` y `field recibido con valor N/A`, por lo que se
puede comprobar si bid (`84`), ask (`86`) o IV (`7633`) aparecen más tarde. El
valor `6509` se interpreta conservadoramente: `RpBd` significa `RealTime` y
book disponible y **no** se convierte en una inferencia de falta de
suscripción. Delta y theta se muestran como valores crudos de diagnóstico, no
como datos fiables para decidir. No se imprimen respuestas arbitrarias,
cookies, cabeceras o credenciales, y este modo no contiene operaciones de
órdenes.

Opcionalmente, `--websocket` abre durante unos segundos el WebSocket del mismo
Client Portal Gateway y envía la suscripción oficial `smd` **solo para ese
conid**. Solicita exactamente la misma lista de fields, conserva su evolución
temporal, envía `umd` al terminar y muestra las diferencias entre fields vistos
en snapshot y WebSocket:

```bash
PYTHONPATH=src python -m options_scanner.ibkr_diagnostic \
  --deep --websocket --stream-seconds 7 \
  --symbol NVDA --expiration 2026-09 --maturity 2026-09-18 \
  --strike 100 --insecure-tls
```

El parser del stream acepta solo el topic `smd` del conid seleccionado y los
field IDs permitidos; descarta metadatos y cualquier otro topic. La salida no
imprime el handshake, cookies, headers ni credenciales. Este contraste no crea
suscripciones de mercado adicionales y no interpreta la ausencia de un field
como falta de OPRA cuando `6509` comunica RealTime/book.

Referencias oficiales revisadas para este flujo: [Client Portal Web API v1
(Security Definition y Market Data)](https://ibkrcampus.com/campus/ibkr-api-page/cpapi-v1/)
y [WebSocket de Client Portal API](https://ibkrcampus.com/campus/ibkr-api-page/cpapi-v1/#websocket).

## Evolución prevista

Una iteración futura podrá añadir filtros de liquidez y rentabilidad sobre las
métricas ya disponibles.
### Histórico y contexto técnico

Después de un scan, la interfaz muestra barras OHLC diarias, zonas y los strikes de
los candidatos completos en un SVG sin dependencias JavaScript externas. El selector
admite 3M, 6M (valor predeterminado) y 1A. En vivo se reutiliza la sesión del Client
Portal Gateway y se consulta exclusivamente `GET /iserver/marketdata/history` con
`bar=1d`; en demo se genera una serie determinista y reproducible. El estado de market
data `Frozen` no invalida el histórico: se identifica explícitamente la última sesión.

El cálculo es determinista y está separado del scanner financiero:

* **ATR de Wilder (14 por defecto):** `TR(t) = max(high-low,
  |high-close(t-1)|, |low-close(t-1)|)`. Hasta completar el periodo se usa la media
  acumulada de TR y después `ATR(t) = (ATR(t-1) * (periodo-1) + TR(t)) / periodo`.
* **Pivote (ventana 3 por defecto):** un pivot low tiene un `low` estrictamente menor
  que todos los lows de las tres barras anteriores y posteriores; un pivot high aplica
  la regla simétrica a `high`. La ventana y el periodo ATR son configurables.
* **Zona:** se separan pivotes low (soporte) y high (resistencia), se ordenan por precio
  y se incorporan al primer grupo cuya media diste como máximo `max(0.6 * ATR,
  0.2 % del último close)`. Sus límites son mínimo/máximo contacto más media tolerancia
  a cada lado; el centro es la media de contactos.
* **Fuerza, no probabilidad:** `min(100, 15 + min(40, 10*contactos) + 20*recencia
  + 15*reacción + 10*persistencia)`. Recencia decrece linealmente de 1 a 0 en 180 días;
  reacción es la reacción media posterior, limitada a 2 ATR y normalizada a 0–1;
  persistencia es el tiempo entre primer y último contacto, limitado a 120 días y
  normalizado. Menos de 45 es **débil**, 45–69.9 **media**, y 70 o más **fuerte**.
* **Rotura conservadora:** soporte roto si un cierre posterior queda por debajo del
  límite inferior menos `0.5 ATR`; resistencia rota si queda por encima del límite
  superior más `0.5 ATR`. No se infiere inversión sin confirmación adicional, por lo
  que actualmente `inverted` permanece falso.

El endpoint de IBKR puede entregar menos sesiones que el periodo solicitado, omitir
volumen, devolver una respuesta parcial o requerir permisos/sesión autenticada según
la configuración de la cuenta y del Gateway. El adaptador omite filas malformadas y
mantiene los modelos internos independientes del payload de IBKR.

Las zonas se derivan del comportamiento histórico del precio y no garantizan
reacciones futuras. No constituyen una recomendación de inversión.

## Mapa técnico informativo

El análisis histórico conserva todas las zonas detectadas y expone los soportes activos por debajo del precio (`supports_below_price`, de S1 hacia abajo) y las resistencias activas por encima (`resistances_above_price`, de R1 hacia arriba). La interfaz limita deliberadamente el resumen y el SVG a **S1–S3 y R1–R2** para mantenerlos legibles; esto no cambia los filtros ni el ranking de PUTs.

Ejemplo obtenido con una serie sintética/demo (los niveles varían con la serie):

```text
Precio actual: $105.00
S1  $101.50–$102.50  -2.86 %  Media   3 contactos  último contacto 8 sesiones
S2  $94.40–$95.60    -9.52 %  Fuerte  5 contactos  último contacto 31 sesiones
R1  $109.30–$110.70  +4.76 %  Media   2 contactos  último contacto 4 sesiones
Strike $100: BELOW_SUPPORT respecto de S1 y por encima de S2
```

Cada candidato puede incluir de forma opcional su soporte relevante, posición (`ABOVE_SUPPORT`, `INSIDE_SUPPORT` o `BELOW_SUPPORT`), distancia porcentual al límite más cercano de esa zona y fuerza. El mapa visible distingue por encima de S1, dentro de cada zona, cada intervalo S1/S2… y por debajo del último soporte activo. Estos campos son exclusivamente descriptivos: no filtran contratos, no alteran yields y no intervienen en el orden del ranking.

La unidad interna canónica de IV es una **fracción decimal** (`0.482` representa
`48.2 %`). El adaptador de Client Portal convierte una sola vez el campo 7633,
que IBKR entrega en puntos porcentuales, al construir la cotización interna; la
UI aplica el formato porcentual solamente para presentarla.

## Validación técnica multi-ticker

El diagnóstico técnico permite comparar los mismos pivotes, ATR, clustering y
score entre varios activos sin ejecutar el scanner de opciones:

```bash
PYTHONPATH=src python -m options_scanner.technical_check \
  --tickers NVDA,AAPL,MSFT,AMZN,TSLA --period 6M --insecure-tls
```

La tabla web compacta muestra por ticker precio y estado de market data, S1 y
R1 (rango real de cada zona), distancia al borde más cercano, fuerza, contactos
de S1, número de barras y estado del histórico. Si una zona no existe se muestra
`N/D`: la presentación nunca crea niveles. La distancia de S1 añade una etiqueta
informativa calculada desde su borde superior: **Dentro de soporte**, **Muy
cerca** (hasta 2 % por encima), **Cerca** (más de 2 % y hasta 5 %), **Alejado**
(más de 5 %) o **Por debajo**. Esta etiqueta no interviene en filtros ni ranking.

Cada ticker se procesa de forma
aislada: si falla su precio o histórico, el diagnóstico conserva el error y
continúa con los demás. Para generar un informe visual bajo demanda, añade
`--charts technical-check.html`; el HTML deja todos los gráficos colapsados por
defecto. Este flujo solo resuelve el subyacente y su histórico diario, y no
descubre ni solicita contratos de opciones.

Con la interfaz local iniciada, `http://127.0.0.1:8000/technical-check` ofrece
la misma validación 6M como screener separado. La tabla obtiene las barras para
calcular las zonas, pero no incluye ningún SVG inicialmente: el botón **📈 Ver
gráfico** solicita y abre únicamente el detalle del ticker elegido, cerrando
cualquier otro detalle abierto. Un fallo se
muestra como `No disponible` sin impedir que las demás filas terminen.
