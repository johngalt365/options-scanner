from io import BytesIO
import tempfile

from options_scanner.models import User, Watchlist
from options_scanner.private_beta import PrivateBetaMiddleware, SQLiteWorkspaceStore


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


def test_tester_demo_allowed_and_live_blocked():
    tmp, store, app = fixture()
    seen, _ = call(app, "/login", "POST", "username=ana&password=correct+horse+battery")
    cookie = dict(seen["headers"])["Set-Cookie"].split(";", 1)[0]
    _, page = call(app, cookie=cookie)
    token = page.split(b'name="csrf_token" value="')[1].split(b'"')[0].decode()
    assert call(app, "/", "POST", "csrf_token=" + token + "&fake=1", cookie)[0]["status"] == "200 OK"
    seen, body = call(app, "/", "POST", "csrf_token=" + token, cookie)
    assert seen["status"] == "403 Forbidden" and b"IBKR live" in body
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
