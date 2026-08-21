# Ranking Short PUT v1

El ranking se ejecuta **después** de los filtros existentes. Por tanto, una nota no rescata un
contrato inválido. Es una comparación explicable, no una recomendación, y Delta no se interpreta
como probabilidad exacta.

## Fórmula (100 puntos)

Todas las operaciones se limitan al intervalo del bloque y el total a `[0, 100]`.

* **Riesgo/protección (30):** 18 puntos para Delta y 12 para distancia. `|Delta| <= 0.20`
  obtiene 18; desde 0.20 hasta 0.30 desciende linealmente hasta 8. La distancia obtiene
  `min(12, 12 × distancia / 30%)`; así, el colchón satura al 30%.
* **Técnico (25):** en Multi, `18 × horizontes participantes / horizontes solicitados`, más
  hasta 7 por posición. Dentro suma 7; bajo suma `max(2, 6-distancia_al_borde_pp)` (ser mucho
  más bajo no recibe el máximo); sobre suma `max(0, 6-2×distancia_pp)`. Se informa por separado
  cuántos horizontes estaban disponibles, por lo que 2/3 nunca se presenta como 2/2. En modo
  individual se usa exclusivamente el soporte existente: dentro 20, bajo
  `max(5,18-distancia_pp)`, sobre `max(0,16-2×distancia_pp)`, más 5/3/1 para fuerza
  fuerte/media/débil (2 si la etiqueta no se reconoce). Sin soporte: 0.
* **Prima/IV (20):** `min(20, 20 × premium_yield / 5%)`. IV se muestra como contexto y no
  aporta puntos: una IV elevada no se considera automáticamente mejor. Annualized yield sigue
  visible y ordenable, pero no entra en el score.
* **Theta (15):** `min(15, max(0, 15 × theta_decay_pct_per_day / 5))`, donde se conserva
  `theta_decay_pct_per_day = short_theta / mid × 100`. Contract theta y short theta se muestran,
  pero no se puntúan de nuevo.
* **Liquidez (10):** spread relativo `(ask-bid)/mid`. Aporta
  `6 × max(0, 1-spread/50%)`; OI aporta `4 × sqrt(min(OI,500)/500)`. El OI satura y no garantiza
  ejecución.

## Etiquetas, ausencias y desempates

Umbrales centralizados: **Muy sólida** desde 80, **Sólida** desde 65, **Intermedia** desde 45 y
**Débil** por debajo. Si falta un dato, el bloque no inventa un valor: asigna cero a esa parte y
lo enumera como dato ausente, haciendo explícita la menor confianza.

Dentro de un ticker se ordena por: (1) score descendente, (2) menor `|Delta|`, (3) mayor premium
yield, (4) mayor OI y (5) ticker, vencimiento y strike como identidad estable. El screener ordena
los mejores contratos por score y ticker. La función `rank_candidates` anterior permanece como
ranking diagnóstico por annualized yield.

## Límites de v1

Quedan expresamente fuera earnings/event risk, IV Rank, IV Percentile, horizontes 2A/5A,
machine learning, backtesting, recomendaciones de operaciones y personalización de pesos en UI.
