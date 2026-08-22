"""Security boundary and durable storage for the small private beta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs
from uuid import uuid4

from options_scanner.models import User, Watchlist, normalize_watchlist_name

MAX_BODY = 16_384
SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
)


class SQLiteWorkspaceStore:
    """SQLite implementation of the workspace port; every query includes owner."""

    request_identity_required = True

    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())
        self.last_watchlist_migration = None
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES(1);
                CREATE TABLE IF NOT EXISTS users(
                  id TEXT PRIMARY KEY, display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 80),
                  username TEXT UNIQUE, password_hash TEXT, role TEXT NOT NULL DEFAULT 'tester'
                    CHECK(role IN ('tester','operator')), enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS watchlists(
                  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80), symbols TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS watchlists_owner ON watchlists(user_id);
                CREATE TABLE IF NOT EXISTS preferences(
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(user_id,key));
            """)
            self.last_watchlist_migration = self._migrate_watchlist_names(db)

    @staticmethod
    def _migrate_watchlist_names(db):
        """Repair and upgrade watchlist names, regardless of recorded version.

        Rows are read before any mutation, then redundant rows are removed before
        their keeper is normalized.  That order matters for partially upgraded
        databases whose unique index still contains distinct, non-normalized
        keys which normalize to the same value.
        """
        # Do not trust schema_version: beta databases can contain only part of
        # v2 after an interrupted/manual upgrade.  One explicit transaction
        # makes the repair all-or-nothing (executescript would commit early).
        db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in db.execute("PRAGMA table_info(watchlists)")}
        if "name_key" not in columns:
            db.execute("ALTER TABLE watchlists ADD COLUMN name_key TEXT")
        rows = db.execute(
            "SELECT rowid,id,user_id,name,symbols FROM watchlists ORDER BY rowid"
        ).fetchall()
        groups = {}
        for row in rows:
            display_name, name_key = normalize_watchlist_name(row[3])
            groups.setdefault((row[2], name_key), []).append((row, display_name))
        duplicate_groups = sum(len(group) > 1 for group in groups.values())
        duplicates_deleted = 0
        for (_, name_key), duplicates in groups.items():
            keeper, display_name = duplicates[0]
            merged = []
            for row, _ in duplicates:
                for symbol in json.loads(row[4]):
                    if symbol not in merged:
                        merged.append(symbol)
            # Delete first: an already-present unique index may otherwise reject
            # changing the keeper's legacy key to a duplicate's normalized key.
            for row, _ in duplicates[1:]:
                db.execute("DELETE FROM watchlists WHERE rowid=?", (row[0],))
                duplicates_deleted += 1
            db.execute(
                "UPDATE watchlists SET name=?,name_key=?,symbols=? WHERE rowid=?",
                (display_name, name_key, json.dumps(merged), keeper[0]),
            )

        # CREATE INDEX IF NOT EXISTS would silently accept an index with the
        # expected name but the wrong uniqueness/columns.  Validate its shape so
        # startup can also self-heal an interrupted or manually altered v2 DB.
        indexes = {row[1]: row for row in db.execute("PRAGMA index_list(watchlists)")}
        owner_name = indexes.get("watchlists_owner_name")
        index_recreated = owner_name is None
        if owner_name is not None:
            columns = tuple(
                row[2] for row in db.execute("PRAGMA index_info(watchlists_owner_name)")
            )
            if not owner_name[2] or columns != ("user_id", "name_key"):
                db.execute("DROP INDEX watchlists_owner_name")
                index_recreated = True
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS watchlists_owner_name "
            "ON watchlists(user_id,name_key)"
        )
        db.execute("""CREATE TRIGGER IF NOT EXISTS watchlists_name_key_insert
            BEFORE INSERT ON watchlists WHEN NEW.name_key IS NULL OR NEW.name_key = ''
            BEGIN SELECT RAISE(ABORT, 'watchlist name key required'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS watchlists_name_key_update
            BEFORE UPDATE OF name_key ON watchlists WHEN NEW.name_key IS NULL OR NEW.name_key = ''
            BEGIN SELECT RAISE(ABORT, 'watchlist name key required'); END""")
        db.execute("DELETE FROM schema_version")
        db.execute("INSERT INTO schema_version(version) VALUES(2)")
        db.commit()
        return {
            "executed": True,
            "rows_scanned": len(rows),
            "duplicate_groups": duplicate_groups,
            "duplicates_deleted": duplicates_deleted,
            "index_recreated": index_recreated,
            "committed": True,
        }

    def add_user(self, user: User) -> None:
        try:
            with self._connect() as db:
                db.execute("INSERT INTO users(id,display_name) VALUES(?,?)", (user.id, user.display_name))
        except sqlite3.IntegrityError as exc:
            raise ValueError("ya existe el usuario") from exc

    def add_login(self, username: str, password: str, display_name: str, role="tester") -> str:
        if not username.strip() or not display_name.strip() or len(password) < 12 or role not in {"tester", "operator"}:
            raise ValueError("username/display name inválido o contraseña menor de 12 caracteres")
        user_id = uuid4().hex
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        encoded = f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"
        try:
            with self._connect() as db:
                db.execute("INSERT INTO users(id,display_name,username,password_hash,role) VALUES(?,?,?,?,?)",
                           (user_id, display_name.strip(), username.strip(), encoded, role))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"ya existe el username '{username}'") from exc
        return user_id

    def authenticate(self, username: str, password: str):
        with self._connect() as db:
            row = db.execute("SELECT id,display_name,password_hash,role FROM users WHERE username=? AND enabled=1", (username,)).fetchone()
        if not row or not row[2]:
            hashlib.scrypt(password.encode(), salt=b"0" * 16, n=2**14, r=8, p=1)
            return None
        _, n, r, p, salt, expected = row[2].split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return (User(row[0], row[1]), row[3], username) if hmac.compare_digest(actual, bytes.fromhex(expected)) else None

    def watchlists_for(self, user_id: str):
        self._require_user(user_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,name,symbols FROM watchlists WHERE user_id=? ORDER BY name_key,id",
                (user_id,),
            ).fetchall()
        return tuple(Watchlist(row[0], user_id, row[1], tuple(json.loads(row[2]))) for row in rows)

    def save_watchlist(self, item: Watchlist):
        self._require_user(item.user_id)
        name, name_key = normalize_watchlist_name(item.name)
        if len(item.symbols) > 50 or len(name) > 80:
            raise ValueError("límite de watchlist excedido")
        try:
            with self._connect() as db:
                owned = db.execute("SELECT user_id FROM watchlists WHERE id=?", (item.id,)).fetchone()
                if owned and owned[0] != item.user_id:
                    raise KeyError("watchlist desconocida para este usuario")
                db.execute(
                    "INSERT INTO watchlists(id,user_id,name,name_key,symbols) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,name_key=excluded.name_key,"
                    "symbols=excluded.symbols WHERE user_id=excluded.user_id",
                    (item.id, item.user_id, name, name_key, json.dumps(item.symbols)),
                )
        except sqlite3.IntegrityError as exc:
            if "watchlists.user_id, watchlists.name_key" in str(exc):
                raise ValueError("Ya existe una watchlist con ese nombre.") from exc
            raise

    def delete_watchlist(self, user_id, watchlist_id):
        self._require_user(user_id)
        with self._connect() as db:
            cursor = db.execute("DELETE FROM watchlists WHERE id=? AND user_id=?", (watchlist_id, user_id))
            if not cursor.rowcount:
                raise KeyError("watchlist desconocida para este usuario")

    def _require_user(self, user_id):
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise KeyError("usuario desconocido")

    def ready(self):
        with self._connect() as db:
            return db.execute("SELECT MAX(version) FROM schema_version").fetchone() == (2,)


@dataclass
class _Session:
    user: User
    role: str
    username: str
    csrf: str
    expires: float


class PrivateBetaMiddleware:
    """In-app allowlist auth, per-request identity, CSRF and web hardening."""

    def __init__(self, app, store: SQLiteWorkspaceStore, *, secure_cookies=True, session_seconds=8 * 3600):
        self.app, self.store = app, store
        self.secure_cookies, self.session_seconds = secure_cookies, session_seconds
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._admission = threading.Condition(self._lock)
        self._active_users: set[str] = set()
        self._active_global = 0
        self._queued = 0
        self.log = logging.getLogger("options_scanner.requests")

    def __call__(self, environ, start_response):
        started, request_id = time.monotonic(), uuid4().hex
        path, method = environ.get("PATH_INFO", "/"), environ.get("REQUEST_METHOD", "GET")
        status_seen = ["500 Internal Server Error"]
        def respond(status, headers, body=b""):
            status_seen[0] = status
            start_response(status, list(headers) + list(SECURITY_HEADERS) + [("X-Request-ID", request_id)])
            return [body]
        if path == "/health/live":
            return respond("200 OK", [("Content-Type", "application/json")], b'{"status":"ok"}')
        if path == "/health/ready":
            try: ready = self.store.ready()
            except sqlite3.Error: ready = False
            return respond("200 OK" if ready else "503 Service Unavailable", [("Content-Type", "application/json")], b'{"status":"ok"}' if ready else b'{"status":"unavailable"}')
        if int(environ.get("CONTENT_LENGTH") or 0) > MAX_BODY:
            return respond("413 Payload Too Large", [("Content-Type", "text/plain")], b"Request too large")
        session_id = self._cookie(environ).get("beta_session")
        with self._lock:
            session = self._sessions.get(session_id or "")
            if session and session.expires <= time.time():
                self._sessions.pop(session_id, None); session = None
        if path == "/login":
            if method == "GET":
                return respond("200 OK", [("Content-Type", "text/html; charset=utf-8")], self._login_page())
            data = self._form(environ)
            authenticated = self.store.authenticate(data.get("username", "")[:80], data.get("password", "")[:256])
            if not authenticated:
                return respond("401 Unauthorized", [("Content-Type", "text/html; charset=utf-8")], self._login_page("Credenciales no válidas."))
            user, role, username = authenticated
            sid = secrets.token_urlsafe(32); session = _Session(user, role, username, secrets.token_urlsafe(32), time.time() + self.session_seconds)
            with self._lock: self._sessions[sid] = session
            flags = "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" + str(self.session_seconds) + ("; Secure" if self.secure_cookies else "")
            return respond("303 See Other", [("Location", "/"), ("Set-Cookie", "beta_session=" + sid + flags)])
        if not session:
            return respond("303 See Other", [("Location", "/login"), ("Cache-Control", "no-store")])
        if path == "/logout" and method == "POST":
            data = self._form(environ)
            if not hmac.compare_digest(data.get("csrf_token", ""), session.csrf):
                return respond("403 Forbidden", [("Content-Type", "text/plain")], b"CSRF validation failed")
            with self._lock: self._sessions.pop(session_id, None)
            return respond("303 See Other", [("Location", "/login"), ("Set-Cookie", "beta_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if self.secure_cookies else ""))])
        form_data = None
        if method in {"POST", "PUT", "DELETE"}:
            data = form_data = self._form(environ); token = data.get("csrf_token", "")
            if not hmac.compare_digest(token, session.csrf):
                return respond("403 Forbidden", [("Content-Type", "text/plain")], b"CSRF validation failed")
            if data.get("fake") != "1" and session.role != "operator":
                return respond("403 Forbidden", [("Content-Type", "text/plain; charset=utf-8")], "IBKR live no está habilitado todavía para usuarios beta.".encode())
        environ["options_scanner.user"] = session.user
        environ["options_scanner.role"] = session.role
        admitted = False
        if method == "POST" and (form_data or {}).get("action", "scan") == "scan":
            deadline = time.monotonic() + 2.0
            with self._admission:
                if session.user.id in self._active_users or self._queued >= 3:
                    return respond("429 Too Many Requests", [("Content-Type", "text/plain; charset=utf-8"), ("Retry-After", "2")],
                                   "Demasiados scans en curso. Inténtalo de nuevo en unos instantes.".encode())
                self._queued += 1
                try:
                    while self._active_global >= 2:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return respond("429 Too Many Requests", [("Content-Type", "text/plain; charset=utf-8"), ("Retry-After", "2")],
                                           "Demasiados scans en curso. Inténtalo de nuevo en unos instantes.".encode())
                        self._admission.wait(remaining)
                    self._active_global += 1
                    self._active_users.add(session.user.id)
                    admitted = True
                finally:
                    self._queued -= 1
        captured = []
        def hardened_start(status, headers, exc_info=None):
            status_seen[0] = status
            clean = [(k, v) for k, v in headers if k.lower() != "access-control-allow-origin"]
            captured[:] = [status, clean, exc_info]
        try:
            chunks = self.app(environ, hardened_start)
            body = b"".join(chunks)
            content_type = next((v for k, v in captured[1] if k.lower() == "content-type"), "")
            if "text/html" in content_type:
                hidden = ('<input type="hidden" name="csrf_token" value="' + session.csrf + '">').encode()
                from html import escape
                session_ui = (
                    '<aside class="session-summary" aria-label="Sesión">'
                    f'<span>Usuario: <strong>{escape(session.username)}</strong> '
                    f'({escape(session.user.display_name)})</span> '
                    f'<span>Rol: <strong>{escape(session.role)}</strong></span> '
                    '<form method="post" action="/logout"><button type="submit">Cerrar sesión</button></form>'
                    '</aside>'
                ).encode()
                with_session = re.sub(br"(<body\b[^>]*>)", lambda match: match.group(1) + session_ui, body, count=1)
                body = with_session if with_session != body else session_ui + body
                body = re.sub(br"(<form\b[^>]*>)", lambda match: match.group(1) + hidden, body)
                captured[1] = [(k, v) for k, v in captured[1] if k.lower() != "content-length"]
                captured[1].append(("Content-Length", str(len(body))))
            start_response(captured[0], captured[1] + list(SECURITY_HEADERS) + [("X-Request-ID", request_id)], captured[2])
            return [body]
        finally:
            if admitted:
                with self._admission:
                    self._active_global -= 1
                    self._active_users.discard(session.user.id)
                    self._admission.notify()
            self.log.info("request", extra={"request_id": request_id, "user_id": session.user.id,
                          "endpoint": path, "duration_ms": round((time.monotonic()-started)*1000),
                          "status": status_seen[0].split()[0]})

    @staticmethod
    def _cookie(environ):
        result = {}
        for item in environ.get("HTTP_COOKIE", "").split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1); result[key] = value
        return result

    @staticmethod
    def _form(environ):
        length = min(int(environ.get("CONTENT_LENGTH") or 0), MAX_BODY)
        raw = environ["wsgi.input"].read(length)
        environ["wsgi.input"] = __import__("io").BytesIO(raw)
        return {key: values[0] for key, values in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}

    @staticmethod
    def _login_page(error=""):
        from html import escape
        return ("<!doctype html><html lang=es><meta charset=utf-8><title>Acceso beta</title><h1>Short PUT Scanner — beta privada</h1>"
                + (f"<p role=alert>{escape(error)}</p>" if error else "")
                + '<form method=post><label>Usuario <input name=username maxlength=80 required></label><label>Contraseña <input type=password name=password maxlength=256 required></label><button>Entrar</button></form></html>').encode()


def create_private_beta_app(scanner_app, store, **kwargs):
    return PrivateBetaMiddleware(scanner_app, store, **kwargs)
