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
├── ibkr.py         # adaptador IBKR y transporte inyectable
├── workspace.py    # espacios en memoria aislados por usuario
└── brokers.py      # puertos futuros de conexión por usuario
```

`MarketDataProvider` es el puerto de solo lectura para obtener el subyacente y
las cotizaciones de opciones. El scanner depende exclusivamente de ese
protocolo. `FakeMarketDataProvider` ofrece un snapshot determinista de NVDA y
varias PUTs; `IbkrMarketDataProvider` transforma respuestas de la IBKR Web API
mediante un `IbkrTransport` inyectado y comprobable con mocks. No se incluye una
implementación HTTP, autenticación ni llamadas reales a IBKR.

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

## Evolución prevista

Una iteración futura podrá proporcionar un transporte HTTP autenticado para
IBKR en modo solo lectura, aislado por usuario, sin cambiar el dominio ni el
scanner. También podrá añadir filtros de liquidez y rentabilidad sobre las
métricas ya disponibles.
