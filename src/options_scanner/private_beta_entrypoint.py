"""Supported configuration, user administration and server for private beta."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import sqlite3
import sys
from wsgiref.simple_server import make_server

from options_scanner.private_beta import SQLiteWorkspaceStore, create_private_beta_app
from options_scanner.web import create_app


class ConfigurationError(ValueError):
    """Private-beta configuration is missing or unsafe."""


@dataclass(frozen=True)
class PrivateBetaConfig:
    database: str
    environment: str
    secure_cookies: bool
    host: str = "127.0.0.1"
    port: int = 8000
    session_seconds: int = 28_800
    public_url: str = ""

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> "PrivateBetaConfig":
        env = os.environ if environ is None else environ
        database = env.get("OPTIONS_SCANNER_DB", "").strip()
        environment = env.get("OPTIONS_SCANNER_ENV", "").strip()
        raw_secure = env.get("OPTIONS_SCANNER_SECURE_COOKIES", "").strip()
        if not database:
            raise ConfigurationError("Falta OPTIONS_SCANNER_DB (ruta al fichero SQLite).")
        if environment not in {"local-smoke", "production"}:
            raise ConfigurationError("OPTIONS_SCANNER_ENV debe ser 'local-smoke' o 'production'.")
        if raw_secure not in {"0", "1"}:
            raise ConfigurationError("OPTIONS_SCANNER_SECURE_COOKIES debe ser explícitamente 0 o 1.")
        secure = raw_secure == "1"
        if environment == "production" and not secure:
            raise ConfigurationError("Private beta de producción exige HTTPS y cookies Secure (valor 1).")
        public_url = env.get("OPTIONS_SCANNER_PUBLIC_URL", "").strip()
        if environment == "production" and not public_url.startswith("https://"):
            raise ConfigurationError("Producción exige OPTIONS_SCANNER_PUBLIC_URL con esquema https://.")
        if environment != "local-smoke" and not secure:
            raise ConfigurationError("Las cookies no Secure sólo se permiten en local-smoke.")
        try:
            port = int(env.get("OPTIONS_SCANNER_PORT", "8000"))
            seconds = int(env.get("OPTIONS_SCANNER_SESSION_SECONDS", "28800"))
        except ValueError as exc:
            raise ConfigurationError("PORT y SESSION_SECONDS deben ser enteros.") from exc
        if not 1 <= port <= 65535 or not 60 <= seconds <= 28_800:
            raise ConfigurationError("PORT o SESSION_SECONDS fuera del rango seguro.")
        return cls(database, environment, secure, env.get("OPTIONS_SCANNER_HOST", "127.0.0.1"), port, seconds, public_url)


def build_application(config: PrivateBetaConfig):
    store = _store(config)
    scanner_app = create_app(workspace_store=store)
    return create_private_beta_app(scanner_app, store, secure_cookies=config.secure_cookies,
                                   session_seconds=config.session_seconds)


def _store(config: PrivateBetaConfig) -> SQLiteWorkspaceStore:
    parent = Path(config.database).expanduser().resolve().parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = SQLiteWorkspaceStore(config.database)
    try:
        os.chmod(config.database, 0o600)
    except OSError:
        pass
    return store


def create_user(config: PrivateBetaConfig, role: str) -> int:
    username = input("Username: ").strip()
    display_name = input("Display name: ").strip()
    password = getpass.getpass("Password (mínimo 12 caracteres): ")
    confirmation = getpass.getpass("Repite password: ")
    if password != confirmation:
        print("Error: las contraseñas no coinciden.", file=sys.stderr)
        return 2
    try:
        _store(config).add_login(username, password, display_name, role)
    except ValueError as exc:
        print(f"Error: {exc}.", file=sys.stderr)
        return 2
    print(f"Usuario {username} creado con rol {role}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Administración y servidor de Private Beta")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="crear/inicializar la SQLite configurada")
    user_parser = sub.add_parser("create-user", help="crear un usuario sin password en argumentos")
    user_parser.add_argument("--role", choices=("operator", "tester"), required=True)
    sub.add_parser("serve", help="arrancar la Private Beta")
    args = parser.parse_args(argv)
    try:
        config = PrivateBetaConfig.from_environ()
        if args.command == "init-db":
            _store(config)
            print(f"SQLite Private Beta preparada en {config.database}.")
            return 0
        if args.command == "create-user":
            return create_user(config, args.role)
        app = build_application(config)
    except (ConfigurationError, OSError, sqlite3.Error) as exc:
        print(f"Error de configuración Private Beta: {exc}", file=sys.stderr)
        return 2
    scheme = "https" if config.secure_cookies else "http"
    print(f"Private Beta ({config.environment}): {scheme}://{config.host}:{config.port}")
    with make_server(config.host, config.port, app) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
