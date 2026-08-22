# Auditoría de readiness y seguridad — private beta

**Fecha de revisión:** 2026-08-22  
**Alcance:** 2–3 usuarios invitados, aplicación *read-only*, acceso por Internet
con HTTPS. Esta revisión es de arquitectura y código; **no autoriza un despliegue**.

## Veredicto ejecutivo

El scanner es un MVP local razonablemente defensivo, pero **no es hoy una
aplicación web multiusuario ni está listo para exponerse a Internet**. La
separación por `user_id` existe en modelos, repositorio en memoria y pruebas,
pero la identidad se inyecta al construir una instancia WSGI; no se obtiene de
una sesión autenticada. La persistencia desaparece al reiniciar, las cachés de
gráficos son globales y la interfaz carece de autenticación, autorización por
request, CSRF, rate limiting y el conjunto completo de cabeceras de seguridad.

La beta mínima recomendada es un único proceso de aplicación detrás de un
reverse proxy, una base SQLite en volumen persistente (con backups) y
autenticación gestionada en el proxy por lista cerrada de emails. La aplicación
debe recibir una identidad verificada, volver a autorizar cada objeto por
`user_id` y no confiar en una cabecera que el cliente pueda enviar directamente.
Postgres gestionado sólo compensa si el hosting no ofrece disco persistente
fiable o si se necesitan varias réplicas.

Para datos reales, la recomendación de beta es **mantener Client Portal Gateway
y la sesión IBKR exclusivamente bajo control del operador** y compartir sólo
datos de mercado read-only, tras confirmar contractualmente permisos de
redistribución/uso. No se debe presentar esa sesión como si perteneciera a cada
tester. Si esa confirmación no existe, publicar únicamente modo demo o contratar
un proveedor apto para una aplicación multiusuario.

## 1. Estado actual y elementos ya preparados

### Lo que sí existe

- El dominio contiene propietarios explícitos para `Watchlist`,
  `StrategyParameters` y `SavedScanResult` (`models.py:37-70, 135-145`).
- `UserWorkspaceStore` separa diccionarios por `user_id`, exige que el usuario
  exista y comprueba que un resultado apunte a parámetros del mismo usuario
  (`workspace.py:16-84`). Las pruebas cubren dos usuarios, IDs de objeto iguales,
  borrado acotado y referencia cruzada rechazada (`tests/test_workspace.py:15-70`).
- La ruta de watchlists verifica propiedad antes de actualizar y pasa el
  `user_id` al borrar (`web.py:923-939`). Se validan y normalizan tickers a un
  alfabeto reducido y un máximo de 12 caracteres (`scan_service.py:37-55`).
- La salida HTML dinámica usa `html.escape` de forma extensa y los mensajes de
  excepciones inesperadas no se renderizan. Los errores IBKR visibles son
  categorías allow-listed/sanitizadas (`web.py:74-83, 969-1011`).
- El transporte IBKR construye la query con `urlencode`, fija timeout, no recibe
  credenciales y convierte errores de transporte en errores de dominio
  (`ibkr.py:90-124`). El proveedor cuenta nombres de endpoint, no URLs ni
  payloads (`ibkr.py:258-266`).
- Hay presupuesto de scan (30 s por defecto), reserva para market data y límites
  de concurrencia internos. El multi-scan limita workers a 4 y trabajo HTTP a 16
  como techo programático (`scan_service.py:137-144`; `multi_scan.py:31-58`).
- La respuesta principal ya usa `Cache-Control: no-store` y
  `X-Content-Type-Options: nosniff` (`web.py:1012-1017`). El servidor sólo escucha
  en `127.0.0.1`, una base correcta para colocarlo detrás de un proxy
  (`web.py:1021-1025`).
- No hay dependencias externas de runtime declaradas, órdenes de trading ni
  secretos conocidos versionados. El adaptador productivo es de solo lectura.

### Lo que sigue siendo local-only

- `create_app` elige un único `User("local", ...)` o un usuario inyectado para
  **toda la instancia**, no para cada request (`web.py:870-878`). No hay login,
  cookies ni sesiones web.
- `UserWorkspaceStore` es memoria de proceso: no sobrevive a reinicios, no
  coordina procesos y sus mutaciones no están protegidas por locks. Es una buena
  prueba del modelo de ownership, no una persistencia de producción.
- Sólo las watchlists están conectadas actualmente a la UI. Los parámetros y
  resultados guardados existen como modelos/repositorio, pero los scans web no
  los persisten.
- `scan_cache` y `technical_cache` pertenecen a la instancia WSGI y están
  indexadas sólo por ticker. Las rutas GET de gráficos consultan esas cachés sin
  usuario (`web.py:878-901`). Un usuario puede ver el último gráfico del mismo
  ticker generado por otro; además hay escrituras concurrentes sin política de
  expiración o tamaño.
- El proveedor IBKR creado por el servicio conserva cachés contractuales y
  estado `last_*`; este estado es seguro sólo si el proveedor no se comparte
  concurrentemente entre identidades. Los datos públicos de mercado pueden
  compartirse, pero el objeto mutable no debe convertirse accidentalmente en
  contexto de usuario.
- `wsgiref.simple_server` es un servidor de desarrollo, no un servidor de
  producción endurecido. No hay health check ni shutdown/drenaje operacional.

## 2. Hallazgos por prioridad

## **BLOCKER antes de publicar**

| Hallazgo | Riesgo | Cambio mínimo obligatorio | Complejidad |
|---|---|---|---|
| No hay autenticación ni sesión web | Cualquiera que alcance la URL puede escanear, cambiar/borrar listas y consultar IBKR | Autenticación cerrada, identidad por request, expiración y logout | Media |
| Identidad global de la instancia | Suplantación o mezcla de datos; no existe autorización real por request | Resolver `user_id` desde identidad verificada y filtrar toda consulta/mutación en DB | Media |
| Cachés de gráficos globales por ticker | Contaminación cruzada y memoria sin límite | Eliminar caché de resultados sensibles o clave `(user_id, request_id)` con TTL/LRU y autorización | Baja–media |
| Persistencia sólo en memoria | Pérdida de watchlists y comportamiento distinto con múltiples workers | SQLite transaccional en volumen persistente, migraciones y backup probado | Media |
| POST sin CSRF | Un sitio externo puede provocar scans y cambios de watchlist con la cookie del usuario | Token CSRF ligado a sesión en todos los POST; validar `Origin`/`Host` como defensa adicional | Media |
| Sin límites externos de uso | 2–3 usuarios pueden multiplicar scans y saturar Gateway/CPU/hilos | Cuotas por usuario y límite global compartido, cola corta y rechazo `429` | Media |
| Límite de request incompleto | Se leen 8192 bytes, pero un `Content-Length` mayor se trunca y se procesa; faltan límites en proxy | Rechazar `411/413`, máximo 16 KiB, content type exacto y timeout de body en proxy | Baja |
| TLS inseguro forzado hacia IBKR | Todos los scans web pasan `allow_insecure_tls=True` (`web.py:956-963`) | Sólo permitirlo para Gateway en loopback/segmento privado explícito; nunca para hosts remotos; pin/CA si cruza red | Baja |
| Sin decisión válida para sesión/licencia IBKR | Una sesión personal compartida confunde identidad, permisos y responsabilidad; puede incumplir términos de market data | Adoptar la decisión IBKR descrita abajo y validarla con IBKR antes de invitar testers | Media/externa |
| Servidor y headers no aptos para Internet | DoS fácil y falta de CSP, anti-clickjacking, política de referrer y permisos | Servidor WSGI de producción + reverse proxy; baseline de headers en todas las respuestas | Media |
| Gestión de secretos no definida | Un futuro secreto de sesión/auth puede acabar en repo, CLI, logs o UI | Variables/secret store, rotación, `.env*` ignorado y plantilla sin valores; redacción central | Baja–media |

## **Recomendado para beta**

| Hallazgo/mejora | Tratamiento propuesto | Complejidad |
|---|---|---|
| Watchlist sin máximo ni nombre acotado | 50 tickers/lista, 20 tickers/scan y nombre 1–80 caracteres normalizado | Baja |
| Preferencias no conectadas | Persistir un único conjunto por usuario; validación servidor idéntica a `ScanRequest` | Baja–media |
| CORS indefinido | No habilitar CORS; aceptar sólo same-origin. Si se añade API, allowlist exacta, nunca `*` con credenciales | Baja |
| Observabilidad insuficiente | JSON a stdout, request ID aleatorio, `user_id` pseudónimo, ruta/estado/duración; métricas mínimas y health checks | Media |
| Backup no probado | Snapshot diario cifrado, retención 7–14 días y simulacro de restore antes de beta | Baja |
| Dependencias/build sin lock | Fijar herramientas/dependencias de producción con hashes o lock reproducible y escanear en CI | Baja |
| Sin acciones de feedback | Formularios seguros con contexto allow-listed, límite de longitud y rate limit | Baja–media |
| No hay auditoría de acciones | Registrar login/logout y CRUD/scan como evento, sin valores sensibles ni payload completo | Baja |

## **Post-beta**

- Postgres gestionado, réplicas o workers múltiples si hay necesidad medida.
- OAuth directo dentro de la app, RBAC, panel administrativo, billing o invitaciones
  automatizadas.
- Histórico completo de resultados; para la beta basta no guardarlo o conservar
  sólo metadatos/resumen con retención breve.
- Plataforma completa de tracing/APM/SIEM. Para tres usuarios, logs, tres métricas,
  alertas de uptime y backup son suficientes.
- Sesión IBKR individual por usuario, sólo después de un diseño específico de
  aislamiento, consentimiento, soporte operativo y gestión de secretos.

## 3. Auditoría multiusuario detallada

| Área | Estado real | Contaminación cruzada / acción |
|---|---|---|
| Usuarios | Modelo, sin almacenamiento ni autenticador | Crear tabla y mapear identidad externa a ID interno opaco |
| Watchlists | CRUD aislado si se construye una app distinta por usuario | Hacer autorización en cada request y constraint único `(user_id, id)` |
| Preferencias | Modelo/repo, sin integración web | Persistir por `user_id`; no aceptar `user_id` del formulario |
| Resultados guardados | Modelo/repo, sin uso web | Omitir inicialmente; si se añade, FK compuesta que impida ownership cruzado |
| Sesión web | Inexistente | Añadir sesión opaca o sesión gestionada por proxy; regenerar al login |
| `scan_cache` | Global, ticker → resultado | Eliminar o separar por usuario/request con TTL y máximo |
| `technical_cache` | Global, ticker → resultado | Puede compartirse sólo si se demuestra que contiene únicamente market data; aun así TTL/LRU y sincronización |
| Caché de contratos IBKR | Mutable por instancia; datos no personales | Instancia compartida sólo como servicio de market data, nunca como contexto de cuenta; locks/TTL/máximo |
| Concurrencia | Semáforo por llamada a `run_multi_ticker`, no global entre requests | Un limitador/cola de proceso único para todos los requests y usuarios |

**Regla de autorización:** cada operación debe derivar `user_id` exclusivamente
de la sesión autenticada; la consulta debe incluirlo (`WHERE user_id = ? AND id
= ?`). Un UUID impredecible no sustituye esta comprobación. Las respuestas 404
para objetos ajenos evitan confirmar su existencia.

## 4. Persistencia mínima

### Recomendación: SQLite para esta beta

Un fichero SQLite en volumen persistente es suficiente para tres usuarios y un
único proceso. Reduce coste y superficie operativa, proporciona transacciones y
backup sencillo. Requisitos: WAL, `foreign_keys=ON`, `busy_timeout`, migraciones,
permisos de fichero `0600`, un único writer breve y backups consistentes mediante
la API de backup (no copiar el fichero abierto sin coordinación).

Esquema mínimo:

```text
users(id UUID/TEXT PK, external_subject UNIQUE, email_normalized UNIQUE,
      display_name, active, created_at, last_login_at)
watchlists(id UUID/TEXT, user_id FK, name, created_at, updated_at,
           PRIMARY KEY(user_id,id))
watchlist_symbols(user_id, watchlist_id, position, ticker,
                  FK(user_id,watchlist_id), UNIQUE(user_id,watchlist_id,ticker))
preferences(user_id PK/FK, min_dte, max_dte, min_safety_margin,
            min_abs_delta, max_abs_delta, min_iv, min_short_theta,
            historical_period, updated_at)
```

No guardar scans inicialmente. Si el feedback demuestra valor, añadir
`scan_runs(id, user_id, created_at, mode, universe_summary, filters_json,
status, expires_at)` y resultados mínimos, con retención de 7–30 días; no guardar
payloads IBKR.

### Comparación

| Opción | Ventajas | Coste/riesgo | Decisión |
|---|---|---|---|
| SQLite local | Sin servicio adicional, barato, backup/restore simple | Requiere volumen fiable; un solo proceso/host; cuidado con backups | **Elegida** en VPS/hosting con disco persistente |
| Postgres gestionado | Backups, alta durabilidad, concurrencia y PaaS stateless | Más coste, credencial/red/migración y operación | Elegir si el PaaS no garantiza disco o exige múltiples instancias |
| KV/serverless/proprietaria | Puede simplificar auth en una plataforma concreta | Lock-in y modelo menos natural | No aporta ventaja clara ahora |

Ni SQLite ni Postgres deben contener contraseña, cookie o token IBKR. Una futura
integración individual requeriría cifrado envelope con KMS, separación de claves,
rotación y threat model antes de almacenar nada.

## 5. Autenticación y sesiones

### Alternativas

| Opción | Seguridad/operación | Encaje beta |
|---|---|---|
| Reverse-proxy auth gestionada + allowlist de 3 emails | Poco código sensible; MFA/OAuth y revocación delegadas; hay que cerrar acceso directo y verificar cabeceras sólo desde el proxy | **Recomendada** |
| Magic link gestionado | Sin contraseñas; depende de email, anti-replay y proveedor | Buena alternativa si el hosting lo integra |
| OAuth directo | MFA y cuentas conocidas, pero callback/state/PKCE y sesiones son código propio | No necesario si lo resuelve el proxy |
| Email/password propio | Hash Argon2id, resets, rate limit y protección de credential stuffing elevan el coste | No recomendado para tres usuarios |
| Basic Auth en proxy | Simple, pero UX/logout/auditoría peores y contraseña reutilizable | Sólo contingencia temporal con claves aleatorias y rate limit |

Contrato mínimo, incluso con auth en proxy:

1. El backend sólo escucha en loopback/red privada y el proxy **elimina** cualquier
   cabecera de identidad enviada por Internet antes de insertar la suya.
2. Mapear `(issuer, subject)` a un usuario activo allow-listed; no confiar sólo en
   el texto del email. Bloquear usuarios desconocidos.
3. Si la app emite cookie: identificador opaco aleatorio, rotación tras login,
   `Secure; HttpOnly; SameSite=Lax; Path=/`, sin `Domain`, TTL inactivo de 30 min y
   absoluto de 8 h. Logout invalida servidor y expira cookie. No guardar identidad
   o permisos confiables en una cookie sin firma/cifrado y rotación.
4. Todos los endpoints salvo `/health/live` exigen identidad; `/health/ready` no
   revela detalles. Respuestas de gráficos, status IBKR, feedback y estáticos están
   incluidas.
5. Para password propio, Argon2id con parámetros calibrados y salt único; nunca
   texto plano, cifrado reversible ni logs del campo. Para esta beta se evita ese
   bloque usando identidad gestionada.

## 6. Seguridad web

- **CSRF:** token sincronizado por sesión en cada POST, comparación constante,
  `SameSite=Lax`, comprobación de `Origin` same-origin. No usar GET para mutar.
- **XSS/HTML:** mantener `escape` en texto y atributos. Los SVG/HTML generados
  deben construirse sólo desde tipos validados; nunca insertar errores o payloads
  remotos. Añadir tests con `"'><script>` para nombre de watchlist, ticker y
  feedback. El CSP recomendado evita que un escape omitido se convierta
  inmediatamente en ejecución.
- **Validación:** tickers 1–12 con política actual; nombre Unicode normalizado,
  1–80 caracteres, sin controles; feedback máximo 4 KiB; IDs con formato UUID;
  números finitos (rechazar NaN/Inf además de rangos).
- **SSRF:** `base_url` no puede venir del request. Configurarlo al arrancar contra
  una allowlist exacta (`localhost`/IP privada del Gateway), sin redirects hacia
  otros hosts. Aplicar egress firewall sólo a proveedor configurado. Hoy el
  ticker va como query codificada, no como host/path.
- **Path traversal/command injection:** no se usan rutas de fichero ni shell desde
  inputs web actualmente. Mantener esa propiedad; no construir nombres de backup,
  comandos o paths con email/ticker.
- **Request/header smuggling:** terminar HTTP en Caddy/nginx/plataforma mantenida;
  rechazar `Content-Length` ambiguo y `Transfer-Encoding` inesperado; no reflejar
  cabeceras. Confiar `X-Forwarded-*` sólo del proxy y allowlist de `Host`.
- **CORS:** ausente por diseño y debe seguir deshabilitado. No devolver ACAO salvo
  una necesidad concreta.
- **Cabeceras:** en todas las respuestas:
  `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src
  'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors
  'none'; form-action 'self'` (mover JS/CSS inline a ficheros para evitar
  `unsafe-inline`), `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  no-referrer`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`, y
  CSP `frame-ancestors` (opcionalmente `X-Frame-Options: DENY` por compatibilidad).
- **Clickjacking:** cubierto por `frame-ancestors 'none'`.
- **HTTPS:** cookie segura, redirect HTTP y HSTS sólo después de probar HTTPS.
- **DoS/timeouts:** proxy con connect/header/body/idle timeout, máximo de body,
  cola acotada y deadline total. Cancelar trabajo pendiente cuando el cliente se
  desconecta si el servidor lo permite; ninguna request crea pools ilimitados.

## 7. Límites conservadores de beta

Aplicarlos en servidor, no sólo HTML, y contar por `user_id` (con un límite IP
adicional para login). La cuota debe envolver scan single y multi, technical
check y cualquier ruta nueva.

| Recurso | Límite inicial |
|---|---:|
| Tickers por scan | 20 |
| Tickers por watchlist | 50 |
| Watchlists por usuario | 10 |
| Scan activo por usuario | 1 |
| Scans iniciados por usuario | 2/min, burst 2; 30/h |
| Scans activos globales | 2 |
| Tickers activos globales | 3 |
| Requests simultáneas al proveedor | 4 globales (bajar desde 8 hasta medir) |
| Cola | 3 scans, espera máxima 10 s; después `429 Retry-After` |
| Duración total | 30 s por scan y timeout HTTP de proveedor ≤10 s |
| Body HTTP | 16 KiB; feedback 4 KiB |

Los límites globales deben vivir en un coordinador compartido por todas las
requests del proceso; el semáforo actual se crea por lote y por tanto no limita
dos requests web simultáneas entre sí. Publicar métricas de rechazos y ajustar
sólo con medición.

## 8. Secretos, configuración y logs

- Inventario actual: no se encontraron ficheros de credenciales/certificados
  versionados ni dependencias que pidan tokens. `.gitignore` sólo cubre artefactos
  Python; debe añadir `.env`, `.env.*` (excepto `.env.example`), `*.pem`, `*.key`,
  DB y backups.
- Configuración no sensible en variables de entorno: entorno, versión, URL fija
  del Gateway/proveedor, timeouts y límites. Secretos (clave de sesión, client
  secret de auth, DSN si contiene password) en el secret store del hosting o
  fichero root-only fuera del repo; nunca como argumento CLI.
- TLS público termina en proxy con certificado automático. El certificado del
  Gateway no se copia al repo. Si se necesita una CA privada, montarla como
  secreto read-only.
- Redacción central de nombres de cabecera/campo: `Cookie`, `Set-Cookie`,
  `Authorization`, tokens, passwords, account IDs y query strings. No registrar
  request/response body, URL IBKR completa ni headers. Desactivar access logs que
  incluyan query para rutas con contexto.
- El usuario recibe código de error/request ID y mensaje genérico; traceback sólo
  en stderr interno. Nunca renderizar `repr(exc)`. Los logs actuales evitan el
  mensaje inesperado; preservar esa propiedad.
- Añadir secret scanning local/CI y rotar, no sólo borrar, cualquier secreto que
  aparezca alguna vez en Git.

## 9. IBKR: decisión crítica

### Estado técnico observado

La aplicación llama a Client Portal Gateway en
`https://localhost:5000/v1/api`, reutiliza una sesión abierta externamente y no
implementa login (`ibkr.py:90-124`). Antes de escanear comprueba
`/iserver/auth/status` (`ibkr.py:268-271`). La UI local fuerza certificado no
verificado porque el Gateway local usa certificado self-signed. Contratos,
histórico y snapshots comparten esa misma sesión, permisos y suscripciones.

Esto no es multiusuario IBKR. Separar watchlists no separa sesión de broker,
entitlements, pacing, cachés, auditoría o fallos. Una sesión personal compartida
también revela indirectamente si el operador está autenticado y hace que la
disponibilidad dependa de su login/reautenticación. Gateway no debe exponerse a
Internet ni aceptar credenciales de testers a través de la app.

### Opciones

| Opción | Riesgo/beneficio | Decisión beta |
|---|---|---|
| Una sesión Gateway por usuario | Aislamiento conceptual, pero exige procesos/red/estado por usuario, login interactivo y secretos; gran superficie | No implementar ahora |
| Sesión del operador como backend de market data | Arquitectura simple; todos comparten entitlements, pacing y disponibilidad. Requiere confirmar licencia/redistribución y dejar claro que no es cuenta del tester | **Preferida sólo si IBKR lo autoriza** |
| IBKR sólo para operador; testers demo | Cero exposición de sesión y opción más segura, pero beta no valida datos live | Fallback obligatorio |
| Proveedor alternativo | API server-to-server y licencia multiusuario pueden simplificar; coste e integración | Preferido si IBKR no autoriza/soporta el uso compartido |

**Propuesta:** fase 1 con demo para testers y live sólo para operador. En paralelo,
obtener confirmación escrita y vigente de IBKR sobre Client Portal Gateway,
sesiones concurrentes, pacing, entitlements y exhibición de datos a usuarios de
la beta. Si es compatible, ejecutar un Gateway dedicado en el mismo host o red
privada, usuario Unix/container separado, firewall sin puerto público, sesión
manual del operador y backend read-only. La aplicación no almacena credenciales,
no automatiza login y no expone account IDs ni endpoints de portfolio/órdenes.
Si no es compatible, seleccionar proveedor con licencia server-side para tres
usuarios. Esta decisión es un **gate**, no una suposición técnica.

## 10. Arquitectura y hosting

```text
Internet
  → DNS: beta.example.com
  → HTTPS reverse proxy + auth allowlist + request/rate limits
  → WSGI app de producción en 127.0.0.1/red privada (1 proceso)
  → SQLite en volumen persistente → backup cifrado diario
  → adaptador market data
       → demo, o Gateway IBKR del operador en red privada, o proveedor alternativo
```

- **VPS pequeño (recomendado si se usa Gateway):** coste bajo/predecible y control
  de loopback, firewall y volumen. Requiere parches, backup, monitorización y
  restart propios. Caddy simplifica TLS; systemd/Podman/Docker puede reiniciar la
  app. Un único proceso evita inconsistencias de SQLite/caches.
- **PaaS:** despliegue, logs, TLS y rollback simples. Elegir sólo con volumen
  persistente y proceso único; si el filesystem es efímero, usar Postgres
  gestionado. El Gateway local/interactivo suele encajar peor.
- **Container hosting:** imagen reproducible, pero almacenamiento y red privada
  pueden elevar complejidad. Útil si soporta persistent volume, secrets y proceso
  no root; no es una ventaja por sí sola para tres usuarios.

Actualizar mediante imagen/artefacto inmutable, migración con backup previo,
health check y rollback a versión anterior. Disponibilidad objetivo razonable de
beta: best effort con monitor externo cada 5 min; no diseñar HA antes de medir.

## 11. Dominio y TLS

- Subdominio no indexado como `scanner-beta.example.com`; DNS no es control de
  acceso. Añadir `X-Robots-Tag: noindex, nofollow` y auth obligatoria.
- TLS 1.2/1.3 con certificado automático ACME y renovación supervisada; puerto 80
  sólo para challenge/redirect permanente a HTTPS.
- Activar `Strict-Transport-Security: max-age=86400` tras validar todo el flujo y
  subir gradualmente a seis meses. No usar `includeSubDomains` hasta auditar el
  dominio completo; nunca HSTS en desarrollo localhost.
- Firewall: público sólo 80/443 (y SSH restringido/VPN si VPS). App, DB y Gateway
  no tienen listener público.

## 12. Observabilidad y continuidad

- JSON estructurado: timestamp UTC, level, event, app_version/commit, request_id,
  `user_ref` HMAC/pseudónimo, ruta normalizada, status, duración, modo y recuento
  de tickers. Nunca ticker si se considera sensible; para beta puede permitirse
  en eventos de scan, pero no query completa.
- `/health/live`: proceso responde, sin auth y sin datos. `/health/ready`:
  comprobación local de DB, protegida por red/auth y sin consultar IBKR en cada
  probe. El estado del proveedor se mide aparte con baja frecuencia.
- Métricas: requests/estado/latencia, scans activos/encolados/rechazados y duración,
  llamadas/errores/timeouts del proveedor; tamaño/edad del último backup. No usar
  labels email, ticker libre o request ID (alta cardinalidad).
- Monitor de uptime externo sobre una ruta protegida sintética o endpoint mínimo;
  alerta por caída sostenida, errores 5xx y backup atrasado.
- SQLite: backup cifrado diario fuera del host, 7 diarios + 4 semanales, checksum
  y restore probado antes de publicar y luego mensual. Documentar RPO 24 h/RTO
  4 h para beta.

## 13. Feedback de beta

Dos botones autenticados: **Reportar problema** y **Proponer mejora**. Ambos
abren un formulario POST con CSRF, categoría fija, texto (máximo 4 KiB) y opt-in
visible para adjuntar contexto. Puede enviarse a una tabla `feedback` o a email/
issue tracker mediante integración server-side; nunca colocar secretos en URL.

Allowlist automática:

```json
{
  "app_version": "commit/build id",
  "ticker_or_universe": ["NVDA"],
  "filters": {"min_dte": 30, "max_dte": 45, "historical_period": "6m"},
  "mode": "demo",
  "timestamp": "UTC ISO-8601",
  "request_id": "opaque id"
}
```

Reconstruir este objeto en servidor desde campos tipados; no adjuntar el request
original. Excluir cookies, tokens, cabeceras, email si no es necesario, account
IDs, conids si pudieran correlacionar actividad, stack traces y payloads IBKR.
Mostrar al usuario exactamente el contexto antes de enviar. Rate limit: 5/h por
usuario; escape al renderizar en cualquier panel/email HTML.

## 14. Dependencias y supply chain

- `pyproject.toml` no declara dependencias de runtime, lo que reduce superficie,
  pero `setuptools>=68` no da build reproducible y no existe lock/constraints.
- Fijar una versión revisada de setuptools en constraints/lock de build; si se
  añaden servidor WSGI, DB migrations o auth SDK, fijar transitivas con hashes y
  actualizar deliberadamente. Generar SBOM es recomendado, no blocker.
- CI mínimo: tests en Python soportado, compile, revisión de dependencias contra
  una base pública y secret scan. Fallar en vulnerabilidades explotables high/
  critical, con excepción documentada y caducidad.
- Imagen, si se crea: base oficial mínima fijada por digest, multi-stage, sin
  compiladores en runtime, usuario UID no root, filesystem read-only excepto
  volumen DB/tmp, `no-new-privileges`, sin Docker socket, healthcheck y límites de
  CPU/memoria/PIDs. No copiar `.git`, `.env`, tests o backups a la imagen.
- Revisar también sistema operativo, proxy e imagen Gateway; un `pip audit` limpio
  no cubre esos componentes.

## 15. Checklist de implementación priorizada

### P0 — gates antes de cualquier exposición

- [ ] Decidir demo/IBKR y obtener confirmación de permisos/licencia para usuarios.
- [ ] Elegir hosting, threat model corto, dominio y diagrama de fronteras de red.
- [ ] Integrar auth gestionada con allowlist; probar bypass directo y spoofing de
      cabecera de identidad.
- [ ] Reemplazar usuario global por identidad por request y añadir tests de IDOR
      para cada endpoint/objeto.
- [ ] Implementar SQLite, migraciones, ownership compuesto y restore probado.
- [ ] Eliminar/aislar cachés por usuario/request con TTL y límite.
- [ ] CSRF, validación de Origin/Host, límite de body y content type.
- [ ] Limitador global + por usuario, cola acotada y máximos de ticker/watchlist.
- [ ] Servidor WSGI de producción detrás de proxy HTTPS; headers completos y
      acceso directo cerrado.
- [ ] Secret policy, `.gitignore`, redacción y secret scan.
- [ ] Suite de seguridad: usuario A/B, CSRF, XSS, límites, concurrencia y timeouts.

### P1 — beta operable

- [ ] Health checks, logs JSON sanitizados, métricas y monitor de uptime.
- [ ] Backup cifrado automático y restore drill.
- [ ] Feedback problema/mejora con contexto allow-listed.
- [ ] Build fijado, dependencia/secret scan CI, contenedor no root si aplica.
- [ ] Runbook de alta/baja de los tres usuarios, logout/revocación, actualización,
      rollback, caída de Gateway y respuesta a incidente.
- [ ] Prueba privada desde Internet con un usuario sin privilegios y escaneo DAST
      ligero sobre staging sin datos reales.

### P2 — después de aprender de la beta

- [ ] Medir necesidad de histórico de scans, Postgres y mayor disponibilidad.
- [ ] Evaluar proveedor alternativo o sesiones IBKR individuales sólo con diseño
      y acuerdos resueltos.
- [ ] Automatizar retención/auditoría avanzada y ampliar observabilidad según uso.

## 16. Estimación cualitativa consolidada

| Bloque | Complejidad | Motivo |
|---|---|---|
| Auth gestionada + sesión/identidad | Media | Integración pequeña, pero los fallos son críticos |
| Autorización multiusuario | Media | Hay modelo base; hay que rehacer el contexto por request y todas las rutas |
| SQLite + migraciones/backups | Media | Esquema pequeño; restore y constraints requieren disciplina |
| CSRF/headers/validación | Media | Poco código, muchos casos y tests |
| Límites/cola/timeouts globales | Media | El límite actual no cruza requests |
| Hosting/TLS/proxy | Media | Estándar, con cuidado especial de Gateway y acceso directo |
| Decisión IBKR | Media + dependencia externa | La implementación puede ser simple; autorización/licencia es el gate |
| Observabilidad/backups | Baja–media | Herramientas simples bastan, pero hay que probarlas |
| Feedback seguro | Baja–media | Dos formularios y un objeto allow-listed |
| Supply chain/contenedor | Baja–media | Pocas dependencias hoy; faltan reproducibilidad y runtime productivo |

**Criterio de salida:** todos los P0 completos y verificados, cero acceso directo
a app/Gateway/DB, restauración exitosa, dos usuarios de prueba sin posibilidad de
leer o mutar datos del otro, y decisión IBKR documentada. Hasta entonces, el
único uso aceptable sigue siendo local o en una red privada no expuesta.
