# options-scanner

Aplicación sencilla para filtrar opciones financieras.

## Parámetros del escáner

El DTE, el margen de seguridad y la delta son parámetros configurables. Los
valores por defecto del MVP son:

- **DTE:** entre 30 y 45 días.
- **Margen de seguridad:** 20 %.
- **Delta:** entre 0,15 y 0,30 en valor absoluto.

Las puts pueden tener delta negativa. Por eso, el filtro utiliza siempre el
valor absoluto de delta (`|delta|`); por ejemplo, una delta de `-0,20` se evalúa
como `0,20`.

## Evolución prevista

La primera integración con Interactive Brokers (IBKR) será exclusivamente de
lectura: consultará market data y cadenas de opciones, sin enviar órdenes.
