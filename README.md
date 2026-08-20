# Options Scanner

Base modular para una aplicación multiusuario de análisis de opciones. El MVP
busca PUTs de NVDA por DTE, margen de seguridad y delta. No implementa órdenes,
login, base de datos, frontend, Docker ni conexiones de red reales.

## Arquitectura

```text
src/options_scanner/
├── models.py       # modelos inmutables de dominio y modelos multiusuario
├── filters.py      # reglas puras, independientes de proveedores
├── market_data.py  # puerto MarketDataProvider y FakeMarketDataProvider
├── scanner.py      # servicio que orquesta cualquier proveedor
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
