import os
import sqlite3
import tempfile
import unittest.mock

import pytest

from options_scanner.private_beta_entrypoint import (ConfigurationError,
                                                      PrivateBetaConfig,
                                                      build_application,
                                                      create_user,
                                                      main)


def config_env(**updates):
    values = {"OPTIONS_SCANNER_DB": "/tmp/beta.sqlite3",
              "OPTIONS_SCANNER_ENV": "local-smoke",
              "OPTIONS_SCANNER_SECURE_COOKIES": "0"}
    values.update(updates)
    return values


def test_configuration_requires_explicit_safe_cookie_mode():
    assert PrivateBetaConfig.from_environ(config_env()).secure_cookies is False
    assert PrivateBetaConfig.from_environ(config_env(
        OPTIONS_SCANNER_ENV="production", OPTIONS_SCANNER_SECURE_COOKIES="1",
        OPTIONS_SCANNER_PUBLIC_URL="https://beta.example.test")).secure_cookies
    with pytest.raises(ConfigurationError, match="HTTPS"):
        PrivateBetaConfig.from_environ(config_env(OPTIONS_SCANNER_ENV="production"))
    with pytest.raises(ConfigurationError, match="OPTIONS_SCANNER_DB"):
        PrivateBetaConfig.from_environ({})


def test_entrypoint_builds_auth_app_without_local_user():
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database:
        config = PrivateBetaConfig(database.name, "local-smoke", False)
        app = build_application(config)
        assert app.store.authenticate("local", "irrelevant password") is None
        seen = {}
        body = b"".join(app({"PATH_INFO": "/", "REQUEST_METHOD": "GET", "CONTENT_LENGTH": "0",
                             "wsgi.input": __import__("io").BytesIO()},
                            lambda status, headers: seen.update(status=status, headers=headers)))
        assert seen["status"] == "303 See Other" and body == b""


def test_create_user_reads_password_with_getpass_and_reports_invalid(capsys):
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database:
        config = PrivateBetaConfig(database.name, "local-smoke", False)
        with unittest.mock.patch("builtins.input", side_effect=["tester1", "Tester Uno"]), \
             unittest.mock.patch("getpass.getpass", side_effect=["short", "short"]):
            assert create_user(config, "tester") == 2
        assert "contraseña" in capsys.readouterr().err


def test_create_operator_then_duplicate_has_clear_message(capsys):
    with tempfile.TemporaryDirectory() as directory:
        config = PrivateBetaConfig(os.path.join(directory, "beta.sqlite3"), "local-smoke", False)
        answers = ["operator1", "Operador Uno"]
        passwords = ["a sufficiently long password", "a sufficiently long password"]
        with unittest.mock.patch("builtins.input", side_effect=answers), \
             unittest.mock.patch("getpass.getpass", side_effect=passwords):
            assert create_user(config, "operator") == 0
        with unittest.mock.patch("builtins.input", side_effect=answers), \
             unittest.mock.patch("getpass.getpass", side_effect=passwords):
            assert create_user(config, "operator") == 2
        assert "ya existe el username 'operator1'" in capsys.readouterr().err


def test_inspect_db_is_read_only_and_reports_watchlist_schema(monkeypatch, capsys):
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database:
        with sqlite3.connect(database.name) as db:
            db.executescript("""
                CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
                INSERT INTO schema_version VALUES(2);
                CREATE TABLE watchlists(id TEXT, user_id TEXT, name TEXT,
                                        symbols TEXT, name_key TEXT);
                CREATE UNIQUE INDEX watchlists_owner_name
                  ON watchlists(user_id,name_key);
                CREATE TRIGGER watchlists_guard BEFORE INSERT ON watchlists
                  BEGIN SELECT 1; END;
                INSERT INTO watchlists VALUES('one','user','Techbeta','["AAPL"]','techbeta');
            """)
        before = os.stat(database.name).st_mtime_ns
        monkeypatch.setenv("OPTIONS_SCANNER_DB", database.name)
        assert main(["inspect-db"]) == 0
        output = capsys.readouterr().out
        assert f"database: {os.path.realpath(database.name)}" in output
        assert "schema_version: [(2,)]" in output
        assert "PRAGMA table_info(watchlists)" in output
        assert "PRAGMA index_list(watchlists)" in output
        assert "PRAGMA index_info(watchlists_owner_name)" in output
        assert "watchlists_guard" in output
        assert "(1, 'one', 'user', 'Techbeta', 'techbeta', '[\"AAPL\"]')" in output
        assert os.stat(database.name).st_mtime_ns == before


def test_debug_migration_reports_commit_and_absolute_server_database(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as directory:
        database = os.path.join(directory, "beta.sqlite3")
        config = PrivateBetaConfig(database, "local-smoke", False)
        build_application(config, debug_migrations=True)
        output = capsys.readouterr().out
        assert "executed=true" in output
        assert "duplicates_deleted=0" in output
        assert "committed=true" in output
