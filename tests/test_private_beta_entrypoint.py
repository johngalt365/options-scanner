import os
import tempfile
import unittest.mock

import pytest

from options_scanner.private_beta_entrypoint import (ConfigurationError,
                                                      PrivateBetaConfig,
                                                      build_application,
                                                      create_user)


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
