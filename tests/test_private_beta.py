from io import BytesIO
import json
import sqlite3
import tempfile

import pytest

from options_scanner.models import User, Watchlist
from options_scanner.market_data import FakeMarketDataProvider
from options_scanner.private_beta import PrivateBetaMiddleware, SQLiteWorkspaceStore
from options_scanner.scan_service import PutScanService
from options_scanner.web import create_app


def call(app, path="/", method="GET", body="", cookie=""):
    seen = {}
    raw = body.encode()
    env = {"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": BytesIO(raw), "HTTP_COOKIE": cookie}
    output = b"".join(app(env, lambda status, headers, exc_info=None: seen.update(status=status, headers=headers)))
    return seen, output


def fixture():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
    store = SQLiteWorkspaceStore(tmp.name)
    store.add_login("ana", "correct horse battery", "<script>alert(1)</script>")
    def inner(environ, start):
        user = environ["options_scanner.user"]
        body = (f'<form method="post"><input value="{user.id}"></form>').encode()
        start("200 OK", [("Content-Type", "text/html"), ("Access-Control-Allow-Origin", "*")])
        return [body]
    return tmp, store, PrivateBetaMiddleware(inner, store)


def test_auth_login_csrf_logout_headers_and_health():
    tmp, store, app = fixture()
    assert call(app)[0]["status"] == "303 See Other"
    seen, _ = call(app, "/login", "POST", "username=ana&password=correct+horse+battery")
    cookie_header = dict(seen["headers"])["Set-Cookie"]
    assert all(flag in cookie_header for flag in ("Secure", "HttpOnly", "SameSite=Strict"))
    cookie = cookie_header.split(";", 1)[0]
    seen, body = call(app, cookie=cookie)
    headers = dict(seen["headers"])
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers
    token = body.split(b'name="csrf_token" value="')[1].split(b'"')[0].decode()
    assert call(app, "/logout", "POST", "csrf_token=bad", cookie)[0]["status"] == "403 Forbidden"
    assert call(app, "/logout", "POST", "csrf_token=" + token, cookie)[0]["status"] == "303 See Other"
    assert call(app, "/health/live")[0]["status"] == "200 OK"
    assert call(app, "/health/ready")[0]["status"] == "200 OK"
    tmp.close()


def test_sqlite_persists_and_enforces_ownership_and_limits():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
    first = SQLiteWorkspaceStore(tmp.name)
    a, b = User("a", "A"), User("b", "B")
    first.add_user(a); first.add_user(b)
    first.save_watchlist(Watchlist("one", "a", '<img src=x onerror="alert(1)">', ("AAPL",)))
    second = SQLiteWorkspaceStore(tmp.name)
    assert second.watchlists_for("a")[0].name.startswith("<img")
    assert second.watchlists_for("b") == ()
    try: second.delete_watchlist("b", "one")
    except KeyError: pass
    else: raise AssertionError("cross-owner delete succeeded")
    assert second.watchlists_for("a")[0].id == "one"
    try: second.save_watchlist(Watchlist("many", "a", "x", tuple("T" + str(i) for i in range(51))))
    except ValueError: pass
    else: raise AssertionError("watchlist limit missing")
    tmp.close()


def test_watchlist_names_are_unique_normalized_per_owner_and_survive_restart():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
    store = SQLiteWorkspaceStore(tmp.name)
    store.add_user(User("a", "A")); store.add_user(User("b", "B"))
    store.save_watchlist(Watchlist("one", "a", "  Techbeta  ", ("NVDA",)))
    assert store.watchlists_for("a")[0].name == "Techbeta"
    for item_id, name in (("two", "Techbeta"), ("three", "TECHBETA"), ("four", " techbeta ")):
        try:
            store.save_watchlist(Watchlist(item_id, "a", name, ("AAPL",)))
        except ValueError as exc:
            assert str(exc) == "Ya existe una watchlist con ese nombre."
        else:
            raise AssertionError("duplicate watchlist name accepted")
    store.save_watchlist(Watchlist("other", "a", "Other", ("MSFT",)))
    try:
        store.save_watchlist(Watchlist("other", "a", "techBETA", ("MSFT",)))
    except ValueError:
        pass
    else:
        raise AssertionError("rename to duplicate accepted")
    store.save_watchlist(Watchlist("same-name-other-owner", "b", "TECHBETA", ("SPY",)))
    restarted = SQLiteWorkspaceStore(tmp.name)
    assert [item.name for item in restarted.watchlists_for("a")] == ["Other", "Techbeta"]
    assert [item.name for item in restarted.watchlists_for("b")] == ["TECHBETA"]
    restarted.delete_watchlist("a", "one")
    assert [item.name for item in restarted.watchlists_for("a")] == ["Other"]
    assert [item.name for item in restarted.watchlists_for("b")] == ["TECHBETA"]
    tmp.close()


def test_legacy_duplicate_migration_keeps_oldest_id_and_merges_tickers():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
    with sqlite3.connect(tmp.name) as db:
        db.executescript("""
            CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
            INSERT INTO schema_version VALUES(1);
            CREATE TABLE users(id TEXT PRIMARY KEY, display_name TEXT NOT NULL);
            INSERT INTO users VALUES('a','A');
            CREATE TABLE watchlists(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                                    name TEXT NOT NULL, symbols TEXT NOT NULL);
            CREATE TABLE preferences(user_id TEXT, key TEXT, value TEXT);
        """)
        db.execute("INSERT INTO watchlists VALUES(?,?,?,?)", ("oldest", "a", " Techbeta ", json.dumps(["NVDA", "AAPL"])))
        db.execute("INSERT INTO watchlists VALUES(?,?,?,?)", ("newer", "a", "TECHBETA", json.dumps(["AAPL", "MSFT"])))
    store = SQLiteWorkspaceStore(tmp.name)
    assert store.watchlists_for("a") == (Watchlist("oldest", "a", "Techbeta", ("NVDA", "AAPL", "MSFT")),)
    with sqlite3.connect(tmp.name) as db:
        assert db.execute("SELECT COUNT(*) FROM watchlists").fetchone() == (1,)
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone() == (2,)
        assert any(row[1] == "watchlists_owner_name" and row[2]
                   for row in db.execute("PRAGMA index_list(watchlists)"))
    # A second logical restart proves that the migration is idempotent.
    assert SQLiteWorkspaceStore(tmp.name).watchlists_for("a") == store.watchlists_for("a")
    tmp.close()


def test_v2_database_with_legacy_keys_and_duplicates_is_repaired_on_every_startup():
    """Reproduce the inconsistent shape observed in a pre-existing beta DB."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
    with sqlite3.connect(tmp.name) as db:
        db.executescript("""
            CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
            INSERT INTO schema_version VALUES(2);
            CREATE TABLE users(
              id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
              username TEXT UNIQUE, password_hash TEXT, role TEXT NOT NULL DEFAULT 'tester',
              enabled INTEGER NOT NULL DEFAULT 1);
            INSERT INTO users(id,display_name,username,role)
              VALUES('operator-id','Operator One','operator1','operator');
            INSERT INTO users(id,display_name,username)
              VALUES('other-id','Other','other');
            CREATE TABLE watchlists(
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
              symbols TEXT NOT NULL, name_key TEXT);
            CREATE INDEX watchlists_owner ON watchlists(user_id);
            CREATE UNIQUE INDEX watchlists_owner_name
              ON watchlists(user_id,name_key);
            CREATE TRIGGER watchlists_name_key_insert
              BEFORE INSERT ON watchlists WHEN NEW.name_key IS NULL OR NEW.name_key = ''
              BEGIN SELECT RAISE(ABORT, 'watchlist name key required'); END;
            CREATE TRIGGER watchlists_name_key_update
              BEFORE UPDATE OF name_key ON watchlists WHEN NEW.name_key IS NULL OR NEW.name_key = ''
              BEGIN SELECT RAISE(ABORT, 'watchlist name key required'); END;
            CREATE TABLE preferences(user_id TEXT, key TEXT, value TEXT);
        """)
        db.execute(
            "INSERT INTO watchlists VALUES(?,?,?,?,?)",
            ("oldest", "operator-id", "Techbeta", json.dumps(["NVDA", "AAPL"]), "Techbeta"),
        )
        db.execute(
            "INSERT INTO watchlists VALUES(?,?,?,?,?)",
            ("newer", "operator-id", "Techbeta", json.dumps(["AAPL", "MSFT"]), "TECHBETA"),
        )
        before = db.execute(
            "SELECT id,user_id,name,name_key,symbols,rowid FROM watchlists ORDER BY rowid"
        ).fetchall()
        assert len(before) == 2 and {row[3] for row in before} == {"Techbeta", "TECHBETA"}

    store = SQLiteWorkspaceStore(tmp.name)
    expected = (Watchlist("oldest", "operator-id", "Techbeta", ("NVDA", "AAPL", "MSFT")),)
    assert store.watchlists_for("operator-id") == expected
    with sqlite3.connect(tmp.name) as db:
        assert db.execute(
            "SELECT id,user_id,name,name_key,symbols,rowid FROM watchlists"
        ).fetchone() == (
            "oldest", "operator-id", "Techbeta", "techbeta",
            json.dumps(["NVDA", "AAPL", "MSFT"]), before[0][5],
        )
        index = next(row for row in db.execute("PRAGMA index_list(watchlists)")
                     if row[1] == "watchlists_owner_name")
        assert index[2] == 1
        assert tuple(row[2] for row in db.execute(
            "PRAGMA index_info(watchlists_owner_name)")) == ("user_id", "name_key")

    app = create_app(workspace_store=store, user=User("operator-id", "Operator One"))
    for _ in range(2):  # initial render and browser refresh
        status, page = call(app)
        assert status["status"] == "200 OK"
        text = page.decode()
        assert text.count("Watchlist: Techbeta") == 1
        assert text.count('value="Techbeta"') == 1

    restarted = SQLiteWorkspaceStore(tmp.name)
    assert restarted.watchlists_for("operator-id") == expected
    with pytest.raises(ValueError, match="Ya existe"):
        restarted.save_watchlist(Watchlist("duplicate", "operator-id", "TECHBETA", ("SPY",)))
    restarted.save_watchlist(Watchlist("other-copy", "other-id", "techbeta", ("SPY",)))
    assert len(restarted.watchlists_for("operator-id")) == 1
    assert len(restarted.watchlists_for("other-id")) == 1
    tmp.close()


def test_v2_database_repairs_wrong_non_unique_owner_name_index():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
    store = SQLiteWorkspaceStore(tmp.name)
    store.add_user(User("a", "A"))
    with sqlite3.connect(tmp.name) as db:
        db.execute("DROP INDEX watchlists_owner_name")
        db.execute("CREATE INDEX watchlists_owner_name ON watchlists(name_key)")

    SQLiteWorkspaceStore(tmp.name)
    with sqlite3.connect(tmp.name) as db:
        index = next(row for row in db.execute("PRAGMA index_list(watchlists)")
                     if row[1] == "watchlists_owner_name")
        assert index[2] == 1
        assert tuple(row[2] for row in db.execute(
            "PRAGMA index_info(watchlists_owner_name)")) == ("user_id", "name_key")
    tmp.close()


def test_tester_real_scan_form_blocks_live_before_provider_and_allows_demo():
    class ProviderSpy(FakeMarketDataProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        def get_underlying(self, ticker):
            self.calls.append(("underlying", ticker))
            return super().get_underlying(ticker)

        def get_option_market_data(self, ticker):
            self.calls.append(("options", ticker))
            return super().get_option_market_data(ticker)

    class ServiceWithProvider(PutScanService):
        def __init__(self, provider):
            super().__init__()
            self.provider = provider
            self.requests = []

        def run(self, request, **kwargs):
            self.requests.append(request)
            return super().run(request, provider=self.provider, **kwargs)

    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
    store = SQLiteWorkspaceStore(tmp.name)
    store.add_login("tester1", "correct horse battery", "Tester One", role="tester")
    provider = ProviderSpy()
    service = ServiceWithProvider(provider)
    app = PrivateBetaMiddleware(create_app(service, workspace_store=store), store,
                                secure_cookies=False)

    seen, _ = call(app, "/login", "POST", "username=tester1&password=correct+horse+battery")
    cookie = dict(seen["headers"])["Set-Cookie"].split(";", 1)[0]
    _, page = call(app, cookie=cookie)
    token = page.split(b'name="csrf_token" value="')[1].split(b'"')[0].decode()

    # This is the complete payload emitted by the real scan form when its Demo
    # checkbox is off: an unchecked checkbox contributes no ``fake`` field.
    form = ("ticker=NVDA&universe_source=manual&min_dte=30&max_dte=45"
            "&min_safety_margin=20&min_abs_delta=0.15&max_abs_delta=0.30"
            "&min_iv=&min_short_theta=&historical_period=6m&csrf_token=" + token)
    seen, body = call(app, "/", "POST", form, cookie)
    assert seen["status"] == "403 Forbidden"
    assert "IBKR live no está habilitado todavía para usuarios beta." in body.decode()
    assert b'id="scan-output"' in body and b'role="alert"' in body
    assert service.requests == []
    assert provider.calls == []

    seen, body = call(app, "/", "POST", form + "&fake=1", cookie)
    assert seen["status"] == "200 OK"
    assert service.requests[0].ticker == "NVDA" and service.requests[0].fake is True
    assert ("underlying", "NVDA") in provider.calls
    assert ("options", "NVDA") in provider.calls
    assert b"Datos simulados" in body
    tmp.close()


def test_tester_can_create_update_and_delete_own_watchlist_via_post():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
    store = SQLiteWorkspaceStore(tmp.name)
    store.add_login("tester1", "correct horse battery", "Tester One")
    app = PrivateBetaMiddleware(create_app(workspace_store=store), store)

    seen, _ = call(app, "/login", "POST", "username=tester1&password=correct+horse+battery")
    cookie = dict(seen["headers"])["Set-Cookie"].split(";", 1)[0]
    _, page = call(app, cookie=cookie)
    token = page.split(b'name="csrf_token" value="')[1].split(b'"')[0].decode()

    create = ("csrf_token=" + token
              + "&action=watchlist_create&watchlist_name=Core&watchlist_tickers=NVDA%2C+SPY")
    seen, body = call(app, "/", "POST", create, cookie)
    assert seen["status"] == "200 OK"
    assert b"Watchlist guardada." in body
    item = store.watchlists_for(store.authenticate("tester1", "correct horse battery")[0].id)[0]
    assert item.name == "Core" and item.symbols == ("NVDA", "SPY")

    update = ("csrf_token=" + token + "&action=watchlist_update&watchlist_id=" + item.id
              + "&watchlist_name=Growth&watchlist_tickers=QQQ%2C+MSFT")
    assert call(app, "/", "POST", update, cookie)[0]["status"] == "200 OK"
    updated = store.watchlists_for(item.user_id)[0]
    assert updated.name == "Growth" and updated.symbols == ("QQQ", "MSFT")

    delete = "csrf_token=" + token + "&action=watchlist_delete&watchlist_id=" + item.id
    assert call(app, "/", "POST", delete, cookie)[0]["status"] == "200 OK"
    assert store.watchlists_for(item.user_id) == ()
    tmp.close()


def test_authenticated_ui_shows_identity_role_and_post_logout():
    tmp, store, app = fixture()
    seen, _ = call(app, "/login", "POST", "username=ana&password=correct+horse+battery")
    cookie = dict(seen["headers"])["Set-Cookie"].split(";", 1)[0]
    _, page = call(app, cookie=cookie)
    assert b"Usuario: <strong>ana</strong>" in page
    assert b"Rol: <strong>tester</strong>" in page
    assert b'action="/logout"' in page and b"Cerrar sesi" in page
    logout = page.split(b'action="/logout"', 1)[1].split(b"</form>", 1)[0]
    assert b'name="csrf_token"' in logout
