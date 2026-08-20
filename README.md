# Options Scanner

Base inicial, pequeña y modular, para una aplicación multiusuario de análisis de
opciones financieras. Esta fase **no** se conecta a Interactive Brokers
(IBKR) y no incluye interfaz web, base de datos ni infraestructura adicional.

## Objetivo del MVP

El MVP identifica contratos que podrían encajar en una estrategia de venta de
PUT con intención de adquirir las acciones en caso de asignación. DTE, margen
de seguridad y delta son parámetros configurables; los valores por defecto de
esta primera versión son:

- subyacente: **NVDA**;
- tipo de opción: **PUT**;
- vencimiento (DTE): entre **30 y 45 días**, ambos inclusive;
- margen de seguridad: al menos **20 %** respecto al precio actual, calculado
  como `(precio_actual - strike) / precio_actual`;
- delta: valor absoluto entre **0,15 y 0,30**, ambos inclusive.

La delta de una PUT puede ser negativa. Por ese motivo, el filtro no compara
su signo, sino su valor absoluto (`|delta|`): por ejemplo, una delta de `-0,20`
se evalúa como `0,20` y queda dentro del intervalo configurado por defecto.

Los datos son ficticios y sirven únicamente para demostrar y probar las reglas;
no constituyen datos de mercado ni una recomendación de inversión.

## Arquitectura inicial

```text
.
├── pyproject.toml
├── src/options_scanner/
│   ├── models.py       # modelos de dominio: subyacente y contrato
│   ├── filters.py      # reglas de selección independientes de la fuente de datos
│   ├── workspace.py    # repositorio en memoria aislado por usuario
│   ├── brokers.py      # puerto y perfil no sensible para conexiones futuras
│   ├── sample_data.py  # opciones PUT ficticias de NVDA
│   └── example.py      # ejemplo ejecutable
└── tests/               # reglas del scanner y aislamiento multiusuario
```

La separación entre modelos, reglas y datos permite sustituir en el futuro los
datos ficticios por un adaptador de IBKR sin modificar la lógica de filtrado.
También facilita añadir estrategias y tickers mediante nuevos filtros y
servicios, manteniendo el núcleo de dominio independiente de proveedores.

## Orientación multiusuario

Los modelos `User`, `Watchlist`, `StrategyParameters` y `SavedScanResult`
preparan el dominio para que listas, configuraciones y resultados tengan un
propietario explícito mediante `user_id`. Por ahora `UserWorkspaceStore` los
guarda únicamente en memoria y crea un espacio separado para cada usuario; no
es una base de datos ni pretende ofrecer persistencia entre ejecuciones.

El scanner y sus filtros siguen siendo funciones de dominio puras: reciben
precios, contratos, fechas y parámetros, y no conocen usuarios, sesiones ni
autenticación. Una capa de aplicación futura podrá leer la configuración del
workspace, invocar el scanner y guardar el resultado sin acoplar esas reglas al
modelo multiusuario.

`BrokerConnection` define el puerto para adaptadores futuros y
`BrokerConnectionProfile` identifica a su propietario sin guardar secretos.
Cada usuario tendrá su **propia cuenta y su propia sesión de IBKR**. Las
credenciales y sesiones no se compartirán entre usuarios. En esta fase no hay
conexión real, almacenamiento de credenciales, login, OAuth, contraseñas, base
de datos ni frontend.

## Requisitos e instalación

- Python 3.11 o posterior.

Desde la raíz del repositorio se recomienda crear un entorno virtual e instalar
el proyecto en modo editable:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

El proyecto no tiene dependencias de ejecución externas.

## Ejecutar el ejemplo

```bash
python -m options_scanner.example
```

El ejemplo fija una fecha de análisis para que el resultado sea reproducible y
muestra únicamente los contratos ficticios que cumplen todas las reglas.

## Ejecutar los tests

Los tests usan exclusivamente la biblioteca estándar:

```bash
python -m unittest discover -s tests -v
```

## Evolución prevista

En iteraciones posteriores se podrán incorporar, de forma incremental:

1. un adaptador real para el puerto de IBKR cuya primera
   integración será exclusivamente de lectura, destinada a consultar market
   data y cadenas de opciones, sin envío de órdenes y con una conexión aislada
   por usuario;
2. métricas de theta, volatilidad implícita, liquidez, bid/ask, open interest y
   rentabilidad de la prima;
3. un sistema de scoring configurable;
4. nuevas estrategias, subyacentes y parámetros de selección;
5. persistencia o una interfaz web solo cuando aporten valor al flujo principal.

Mantener la lógica de negocio libre de detalles de IBKR hará posible probarla
con datos controlados y cambiar de proveedor sin reescribir las reglas.
