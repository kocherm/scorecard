"""Security hardening: the /tv shortcut is LAN-only, names never reach a JS
string, the MCP path form takes read tokens only, /login costs the same with
or without an account, a password change signs out other sessions, `next` is
never an open redirect, cross-site writes are refused, and credentials in
URLs never reach the access log."""
import glob
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm

PW = "correct horse battery"
START = "2026-01-05"  # a Monday
EVIL = """x');alert(1);//"><img src=x>"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app  # imported late so DB_PATH is already patched

    with dbm.get_db() as con:
        dbm.init_db(con)
        dbm.set_setting(con, "display_token", "tok")
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        for uid, role in ((1, "admin"), (2, "editor")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role)
                   VALUES (?,?,?,?,?)""",
                (uid, f"u{uid}@example.com", auth.hash_password(PW), f"User {uid}", role))
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                    dri_user_id, start_week)
               VALUES (1, 1, ?, 'numeric', 'sum', 2, ?)""", (EVIL, START))
        sessions = {uid: auth.create_session(con, uid) for uid in (1, 2)}

    def client(uid=None, host="testclient"):
        c = TestClient(app, client=(host, 50000))
        if uid:
            c.cookies.set(auth.SESSION_COOKIE, sessions[uid])
        return c
    client.sessions = sessions
    return client


# ---------------------------------------------------------------- 1. /tv
@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.7", "172.16.4.4",
                                  "127.0.0.1", "::1", "fe80::1", "169.254.3.3",
                                  "fd00::5", "::ffff:192.168.1.20"])
def test_tv_redirects_on_the_local_network(env, host):
    r = env(host=host).get("/tv", params={"view": "ceo"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/display?token=tok&view=ceo"


@pytest.mark.parametrize("host", ["8.8.8.8", "2001:4860:4860::8888",
                                  "::ffff:8.8.8.8", "testclient", ""])
def test_tv_refuses_everyone_else_without_leaking_the_token(env, host):
    r = env(host=host).get("/tv", follow_redirects=False)
    assert r.status_code == 403
    assert "location" not in r.headers
    assert "office network" in r.text and "Admin > Settings" in r.text
    assert "tok" not in r.text


def test_tv_refuses_a_request_with_no_client(env):
    from app.main import _on_local_network

    class Req:
        client = None
    assert _on_local_network(Req()) is False


def test_display_token_check_still_refuses_a_wrong_token(env):
    c = env(host="192.168.1.20")
    assert c.get("/display/body", params={"token": "nope"}).status_code == 403
    assert c.get("/display/body", params={"token": ""}).status_code == 403
    assert c.get("/display/body", params={"token": "tok"}).status_code == 200


# ---------------------------------------------------------------- 2. XSS
def test_metric_name_never_lands_in_a_js_string(env):
    html = env(1).get("/admin/metrics").text
    assert "confirm('Archive" not in html
    # The name rides in a data- attribute, entity-escaped, read by JS as data.
    assert 'data-confirm="Archive x&#39;);alert(1);//&#34;&gt;&lt;img src=x&gt;? History is kept."' in html
    assert "onclick=\"return confirm(this.dataset.confirm)\"" in html


def test_token_names_never_land_in_a_js_string(env):
    c = env(1)
    r = c.post("/admin/tokens", data={"name": EVIL, "scope": "read"})
    assert r.status_code == 200
    assert "confirm('" not in r.text
    assert 'data-confirm="Rotate x&#39;);alert(1);//' in r.text


# Expressions that are server-side constants (a step size, CEO slot keys)
# and so may stay inside an inline handler. Anything else is refused.
_JS_ATTR_OK = {"{{ step }}", "{{ t.slot }}", "{{ slot }}"}


def test_no_template_interpolates_into_inline_javascript():
    root = Path(__file__).resolve().parent.parent / "app" / "templates"
    bad = []
    for f in sorted(glob.glob(str(root / "*.html"))):
        s = Path(f).read_text()
        for m in re.finditer(r"""\s(on[a-z]+|hx-on[:\w-]*)\s*=\s*("([^"]*)"|'([^']*)')""",
                             s, re.S):
            val = m.group(3) if m.group(3) is not None else m.group(4)
            for expr in re.findall(r"\{\{.*?\}\}|\{%.*?%\}", val, re.S):
                if expr not in _JS_ATTR_OK:
                    bad.append(f"{Path(f).name}: {m.group(1)}={val[:60]!r}")
        for m in re.finditer(r"<script[^>]*>(.*?)</script>", s, re.S):
            if "{{" in m.group(1) or "{%" in m.group(1):
                bad.append(f"{Path(f).name}: <script> block")
    assert not bad, "Jinja inside JavaScript (use a data- attribute):\n" + "\n".join(bad)


# ---------------------------------------------------------------- 3. MCP
def _rpc(c, path):
    return c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def test_mcp_path_form_accepts_read_tokens_only(env):
    with dbm.get_db() as con:
        tk = {s: auth.new_api_token(con, s, s, 1)
              for s in ("read", "read_write", "write", "admin")}
    c = env()
    assert _rpc(c, f"/mcp/t/{tk['read']}").status_code == 200
    unknown = _rpc(c, "/mcp/t/sc_nope")
    for s in ("read_write", "write", "admin"):
        r = _rpc(c, f"/mcp/t/{tk[s]}")
        assert r.status_code == 401
        assert r.json() == unknown.json(), "same shape as an unknown token"
    # The header form is unchanged: any valid token may read.
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
               headers={"Authorization": f"Bearer {tk['read_write']}"})
    assert r.status_code == 200


def test_connector_url_is_offered_for_read_tokens_only(env):
    c = env(1)
    r = c.post("/admin/tokens", data={"name": "n8n", "scope": "read_write"})
    assert "/mcp/t/sc_" not in r.text
    r = c.post("/admin/tokens", data={"name": "claude", "scope": "read"})
    assert "/mcp/t/sc_" in r.text


# ---------------------------------------------------------------- 4. /login
def test_login_with_no_account_still_verifies_a_hash(env, monkeypatch):
    from app import main
    seen = []
    real = main.verify_password
    monkeypatch.setattr(main, "verify_password",
                        lambda pw, h: seen.append(h) or real(pw, h))
    r = env().post("/login", data={"email": "nobody@example.com", "password": PW})
    assert "Wrong email or password." in r.text
    assert seen == [main._DUMMY_PASSWORD_HASH]
    r = env().post("/login", data={"email": "u2@example.com", "password": PW},
                   follow_redirects=False)
    assert r.status_code == 303


# ---------------------------------------------------------------- 5. password
def test_password_change_signs_out_other_sessions_only(env):
    with dbm.get_db() as con:
        other = auth.create_session(con, 2)
    c = env(2)
    r = c.post("/account/password", data={"current": PW, "new": "a brand new passphrase"},
               follow_redirects=False)
    assert r.status_code == 303
    assert env(2).get("/account", follow_redirects=False).status_code == 200  # kept
    stale = env()
    stale.cookies.set(auth.SESSION_COOKIE, other)
    assert stale.get("/account", follow_redirects=False).status_code != 200


def test_password_change_is_refused_while_viewing_as(env):
    c = env(1)
    c.post("/admin/users/2/impersonate")
    r = c.post("/account/password", data={"current": PW, "new": "a brand new passphrase"},
               follow_redirects=False)
    assert r.status_code == 403
    with dbm.get_db() as con:
        h = con.execute("SELECT password_hash FROM users WHERE id = 2").fetchone()[0]
    assert auth.verify_password(PW, h)


# ---------------------------------------------------------------- 6. next=
@pytest.mark.parametrize("target", ["//evil.example", "/\\evil.example", "\\\\evil.example",
                                    "/\t/evil.example", "https://evil.example",
                                    "", "/ok\\path"])
def test_owner_next_is_never_an_open_redirect(env, target):
    r = env(1).post("/admin/metrics/1/owner", data={"dri_user_id": "2", "next": target},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/metrics#sec-1"


def test_owner_next_keeps_a_local_path(env):
    r = env(1).post("/admin/metrics/1/owner", data={"dri_user_id": "2", "next": "/m/1?x=1"},
                    follow_redirects=False)
    assert r.headers["location"] == "/m/1?x=1"


# ---------------------------------------------------------------- 7. CSRF
def test_cross_site_writes_are_refused(env):
    c = env(2)
    for h in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
              {"Origin": "https://evil.example"}, {"Origin": "null"}):
        r = c.post("/logout", headers=h, follow_redirects=False)
        assert r.status_code == 403, h
    assert env(2).get("/account").status_code == 200, "session survived"


def test_same_origin_and_headerless_writes_pass(env):
    # What htmx and a form on the app's own pages send.
    for h in ({"Sec-Fetch-Site": "same-origin", "HX-Request": "true"},
              {"Sec-Fetch-Site": "none"},
              {"Origin": "http://testserver"},
              {}):
        r = env(2).post("/logout", headers=h, follow_redirects=False)
        assert r.status_code == 303, h


def test_a_cross_site_get_is_not_touched(env):
    r = env(2).get("/account", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/mcp", "/mcp/t/sc_nope", "/api/v1/metrics/1/entries",
                                  "/slack/events", "/telegram/webhook", "/twilio/webhook"])
def test_machine_endpoints_are_exempt(env, path):
    r = env().post(path, json={}, headers={"Sec-Fetch-Site": "cross-site",
                                           "Origin": "https://elsewhere.example"})
    assert not (r.status_code == 403 and "Cross-site" in r.text), path


# ---------------------------------------------------------------- 8. access log
def _access_record(path):
    # The exact shape uvicorn's httptools/h11 protocols log.
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0,
                             '%s - "%s %s HTTP/%s" %d',
                             ("203.0.113.9:5123", "GET", path, "1.1", 200), None)


@pytest.mark.parametrize("path,want", [
    ("/checkin?t=SECRET", "/checkin?t=[redacted]"),
    ("/reset?t=SECRET&x=1", "/reset?t=[redacted]&x=1"),
    ("/display?token=SECRET&view=ceo", "/display?token=[redacted]&view=ceo"),
    ("/display/body?view=ceo&token=SECRET", "/display/body?view=ceo&token=[redacted]"),
    ("/mcp/t/sc_SECRET", "/mcp/t/[redacted]"),
    ("/m/3?week=2026-01-05", "/m/3?week=2026-01-05"),
    ("/x?at=1&tt=2", "/x?at=1&tt=2"),
])
def test_access_log_redacts_credentials(path, want):
    import app.main  # noqa: F401  installs the filter on import
    log = logging.getLogger("uvicorn.access")
    rec = _access_record(path)
    assert all(f.filter(rec) for f in log.filters)
    msg = rec.getMessage()
    assert want in msg and "SECRET" not in msg


def test_access_log_filter_is_installed_once():
    import app.main  # noqa: F401
    n = sum(type(f).__name__ == "RedactCredentialsFilter"
            for f in logging.getLogger("uvicorn.access").filters)
    assert n == 1
