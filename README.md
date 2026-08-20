# options-scanner

App opciones financieras. Incluye un diagnóstico mínimo, independiente del scanner y de solo lectura, para comprobar Interactive Brokers Client Portal Web API.

## Diagnóstico de IBKR

Requisitos previos:

1. Arranca **Client Portal Gateway** por separado.
2. Abre su interfaz en el navegador y completa el login **manualmente**. Este proyecto no guarda credenciales ni automatiza la autenticación.
3. Asegúrate de disponer de permisos/suscripciones para los datos solicitados.

Ejecuta desde la raíz del repositorio:

```bash
python -m ibkr_diagnostic --insecure
```

El gateway local usa por defecto `https://localhost:5000/v1/api`. Para otra dirección:

```bash
python -m ibkr_diagnostic --base-url https://gateway.example/v1/api
```

`--insecure` solo resulta útil cuando el gateway local presenta su certificado autofirmado. El comando localiza NVDA, consulta su precio, vencimientos y strikes PUT, selecciona strikes cercanos al precio, obtiene algunos contratos y muestra bid, ask, delta, theta, volatilidad implícita y open interest cuando IBKR los proporciona. Los campos ausentes se muestran como `null` y generan un aviso; la ausencia total de bid/ask es un error claro. También se distinguen gateway inaccesible, sesión no autenticada, falta de autorización, ticker inexistente y datos incompletos.

No se crean órdenes ni se modifica el scanner. La primera petición de snapshot de IBKR puede servir para iniciar la suscripción; si los datos aún no aparecen, vuelve a ejecutar el diagnóstico unos segundos después.

### Tests

Los tests usan un transporte fake y no realizan conexiones de red:

```bash
python -m unittest discover -s tests -v
```
