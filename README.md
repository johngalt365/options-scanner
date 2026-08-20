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
  --symbol NVDA --expiration 2026-09 --contracts 3 --insecure-tls
```

El comando localiza el subyacente, muestra su precio y los meses disponibles,
selecciona un vencimiento, elige strikes PUT cercanos al precio, resuelve sus
contratos y muestra bid, ask, delta, theta, IV y open interest. `N/D` indica un
campo parcial/no disponible. Los errores distinguen Gateway inaccesible, sesión
no autenticada, ticker desconocido, falta de autorización de market data y
respuestas incompletas. El proyecto y sus tests nunca necesitan hacer una
llamada real: `IbkrMarketDataProvider` recibe un transporte inyectable.

## Evolución prevista

Una iteración futura podrá añadir filtros de liquidez y rentabilidad sobre las
métricas ya disponibles.
