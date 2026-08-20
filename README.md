# Options Scanner

Base inicial, pequeña y modular, para una aplicación personal de análisis de
opciones financieras. Esta primera fase **no** se conecta a Interactive Brokers
(IBKR) y no incluye interfaz web, base de datos ni infraestructura adicional.

## Objetivo del MVP

El MVP identifica contratos que podrían encajar en una estrategia de venta de
PUT con intención de adquirir las acciones en caso de asignación. Su alcance es:

- subyacente: **NVDA**;
- tipo de opción: **PUT**;
- vencimiento (DTE): entre **30 y 45 días**, ambos inclusive;
- margen de seguridad: al menos **20 %** respecto al precio actual, calculado
  como `(precio_actual - strike) / precio_actual`;
- delta: valor absoluto entre **0,15 y 0,30**, ambos inclusive.

Los datos son ficticios y sirven únicamente para demostrar y probar las reglas;
no constituyen datos de mercado ni una recomendación de inversión.

## Arquitectura inicial

```text
.
├── pyproject.toml
├── src/options_scanner/
│   ├── models.py       # modelos de dominio: subyacente y contrato
│   ├── filters.py      # reglas de selección independientes de la fuente de datos
│   ├── sample_data.py  # opciones PUT ficticias de NVDA
│   └── example.py      # ejemplo ejecutable
└── tests/
    └── test_filters.py
```

La separación entre modelos, reglas y datos permite sustituir en el futuro los
datos ficticios por un adaptador de IBKR sin modificar la lógica de filtrado.
También facilita añadir estrategias y tickers mediante nuevos filtros y
servicios, manteniendo el núcleo de dominio independiente de proveedores.

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

1. un puerto de datos de mercado y un adaptador real para IBKR;
2. métricas de theta, volatilidad implícita, liquidez, bid/ask, open interest y
   rentabilidad de la prima;
3. un sistema de scoring configurable;
4. nuevas estrategias, subyacentes y parámetros de selección;
5. persistencia o una interfaz web solo cuando aporten valor al flujo principal.

Mantener la lógica de negocio libre de detalles de IBKR hará posible probarla
con datos controlados y cambiar de proveedor sin reescribir las reglas.
