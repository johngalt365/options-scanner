# Private Beta en un único VPS Hetzner (Ubuntu LTS)

Esta guía **prepara**, pero no ejecuta, un despliegue. La arquitectura es
`Internet → Caddy (TLS) → Gunicorn (127.0.0.1) → WSGI → SQLite`. Se mantiene un
único worker porque las sesiones de autenticación viven en memoria. Demo sigue
disponible a testers; los controles existentes mantienen IBKR Live bloqueado a
testers y no convierten IBKR en multiusuario.

## Rutas, usuario y permisos

| Ruta | Propietario/modo recomendado |
|---|---|
| `/opt/options-scanner/app` | `root:options-scanner`, `0750` (release inmutable) |
| `/opt/options-scanner/venv` | `root:options-scanner`, `0750` |
| `/var/lib/options-scanner/beta.sqlite3` | `options-scanner:options-scanner`, `0600`; directorio `0700` |
| `/etc/options-scanner/options-scanner.env` | `root:options-scanner`, `0640`; directorio `0750` |
| `/var/backups/options-scanner/` | `options-scanner:options-scanner`, `0700`; ficheros `0600` |

## Instalación paso a paso

1. Cree la cuenta sin login y los directorios:
   `sudo useradd --system --home /var/lib/options-scanner --shell /usr/sbin/nologin options-scanner`.
   Instale `python3-venv sqlite3 caddy git` desde repositorios Ubuntu y aplique
   los propietarios/modos de la tabla. Copie o clone una revisión auditada en
   `/opt/options-scanner/app`; no ejecute la aplicación desde una rama mutable.
2. Cree el entorno con `python3 -m venv /opt/options-scanner/venv` y ejecute
   `/opt/options-scanner/venv/bin/pip install /opt/options-scanner/app`. Gunicorn
   es la única dependencia runtime nueva.
3. Cree el EnvironmentFile (sin comillas shell ni secretos en el unit):

       OPTIONS_SCANNER_ENV=production
       OPTIONS_SCANNER_DB=/var/lib/options-scanner/beta.sqlite3
       OPTIONS_SCANNER_SECURE_COOKIES=1
       OPTIONS_SCANNER_PUBLIC_URL=https://scanner.example.com
       OPTIONS_SCANNER_HOST=127.0.0.1
       OPTIONS_SCANNER_PORT=8000
       OPTIONS_SCANNER_SESSION_SECONDS=28800
       OPTIONS_SCANNER_GUNICORN_TIMEOUT=120
       OPTIONS_SCANNER_GUNICORN_GRACEFUL_TIMEOUT=30
       OPTIONS_SCANNER_BACKUP_DIR=/var/backups/options-scanner
       OPTIONS_SCANNER_BACKUP_RETENTION_DAYS=14

4. Inicialice como el usuario del servicio:
   `sudo -u options-scanner env PYTHONPATH=/opt/options-scanner/app/src $(cat /etc/options-scanner/options-scanner.env | xargs) /opt/options-scanner/venv/bin/python -m options_scanner.private_beta_entrypoint init-db`.
   Para evitar que passwords aparezcan en argumentos/history, cree operator y
   2–3 testers interactivamente con el mismo comando terminado en
   `create-user --role operator` o `create-user --role tester`.
5. Copie `deploy/options-scanner.service` a `/etc/systemd/system/`, ejecute
   `systemctl daemon-reload`, habilite e inicie el servicio. Compruebe con
   `systemctl status options-scanner` y `journalctl -u options-scanner`.
6. Sustituya sólo `scanner.example.com` en `deploy/Caddyfile.example`, copie el
   bloque a `/etc/caddy/Caddyfile`, valide con `caddy validate --config
   /etc/caddy/Caddyfile`, y recargue Caddy. Caddy obtiene TLS y redirige HTTP a
   HTTPS automáticamente. Active HSTS **sólo después** de verificar certificado,
   hostname y HTTPS; hasta entonces elimine esa línea. El límite 16 KB coincide
   con la app y Caddy no modifica CSP/CORS ni los headers de seguridad.
7. Verifique localmente `curl --fail http://127.0.0.1:8000/health/live` y
   `/health/ready`. Externamente monitorice
   `https://scanner.example.com/health/live` (proceso) y `/health/ready`
   (SQLite/esquema); ambos sólo devuelven estado, nunca detalles. Verifique el
   redirect de HTTP, certificado, login/logout, cookie `Secure`, Demo tester y
   que Live sea rechazado al tester.

## Proxy, logs, firewall y límites

Gunicorn sólo enlaza loopback. La capa WSGI acepta `X-Forwarded-*` y el request
ID de Caddy únicamente si `REMOTE_ADDR` es loopback, y exige host/proto públicos;
Caddy reemplaza esos headers. No publique el puerto 8000. Permita en Hetzner/UFW
TCP 80/443 y SSH 22, restringiendo SSH a IPs administrativas cuando sea posible.
No se configura automáticamente ningún firewall o recurso Hetzner.

Gunicorn escribe access/error logs a stdout/stderr; consulte `journalctl -u
options-scanner -f`. La app registra request ID, ruta, estado y duración con la
sanitización P0 existente. No añada passwords, cookies, CSRF, Authorization,
account IDs ni payloads IBKR. Caddy elimina IDs aportados por clientes y la
aplicación genera uno seguro; si en el futuro un proxy local genera uno válido,
la aplicación lo preserva.

## Backup diario y restore manual

Copie y habilite el timer y service de `deploy/`; compruebe con `systemctl
list-timers options-scanner-backup.timer` y pruebe `systemctl start
options-scanner-backup.service`. El script usa `.backup` de SQLite (snapshot
consistente), `quick_check`, timestamp UTC, `0600`, publicación por rename y
retención configurable. Exit 2 indica configuración inválida, 3 DB ausente, 4
integridad fallida; otros fallos de SQLite/filesystem son no cero.

Restore nunca es automático: detenga la app, conserve la DB actual, ejecute
`sqlite3 /ruta/backup.sqlite3 'PRAGMA integrity_check'`, copie el backup a un
temporal dentro de `/var/lib/options-scanner`, aplique owner `options-scanner` y
modo `0600`, haga `mv` atómico a `beta.sqlite3`, arranque y compruebe readiness y
login. Ensaye primero con una copia/ruta temporal, nunca contra producción.

## Upgrade y rollback

Antes del cambio cree/verifique backup. Instale cada revisión en un directorio
de release, actualice el symlink `/opt/options-scanner/app`, reinicie y valide
health/login. Para rollback detenga, restaure el symlink a la revisión previa y
arranque. Si hubo migración incompatible, restaure manualmente el backup previo.

## Bloqueos antes de Internet

Faltan acciones humanas: dominio/DNS real; servidor Ubuntu parcheado; reglas de
firewall/SSH; TLS y HSTS verificados; secretos y permisos revisados; usuarios
creados; prueba de backup **y restore**; pruebas operator/tester de Demo/Live;
monitorización externa y procedimiento de incidentes. No se incluyen Terraform,
Ansible, contenedores, Postgres/Redis, escalado ni más workers.
