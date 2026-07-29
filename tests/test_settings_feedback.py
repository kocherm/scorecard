"""Saving a settings panel should land you back on that panel and say so.

The bare redirect this replaces looked identical whether or not anything was
written, which is how a public base URL got filled in, discarded by a sibling
toggle's page reload, and reported as saved."""
import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm
from app import readiness


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    # Saving the Slack panel verifies the token against Slack. No test reaches
    # the network for that; app/readiness.py owns the verification behaviour.
    monkeypatch.setattr(readiness, "_slack_call",
                        lambda *a, **kw: (False, {"error": "invalid_auth"}))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (1,'a@example.com','x','Admin','admin')""")
        sess = auth.create_session(con, 1)
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, sess)
    return c


def setting(key):
    with dbm.get_db() as con:
        return dbm.get_setting(con, key)


def test_save_returns_to_the_panel_and_confirms(client):
    r = client.post("/admin/settings/nudges",
                    data={"public_base_url": "https://score.example.com/",
                          "nudge_preset": "mon_tue"}, follow_redirects=False)
    assert r.status_code == 303
    # Anchor so the browser restores position; query param so the page can say so.
    assert r.headers["location"] == "/admin/settings?saved=nudges#nudges"

    page = client.get("/admin/settings?saved=nudges").text
    assert 'id="nudges"' in page
    assert page.count("Saved.") == 1, "only the panel that was saved says so"


@pytest.mark.parametrize("path,data,anchor", [
    ("/admin/settings/nudges", {"public_base_url": "https://x.test",
                                "nudge_preset": "mon"}, "nudges"),
    ("/admin/settings/nudges-toggle", {}, "nudges"),
    ("/admin/settings/slack", {"slack_bot_token": "xoxb-t"}, "slack"),
    ("/admin/settings/alerts-toggle", {}, "slack"),
    ("/admin/settings/display-months", {"display_months": "3"}, "display-window"),
    ("/admin/settings/goal-band", {}, "goal-band"),
    ("/admin/settings/channels", {"telegram_bot_token": "123:abc"}, "channels"),
    ("/admin/settings/screensaver-toggle", {}, "screensaver"),
    ("/admin/settings/rotate-display-token", {}, "tv-display"),
])
def test_every_settings_post_anchors_back(client, path, data, anchor):
    r = client.post(path, data=data, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/settings?saved={anchor}#{anchor}"


def test_public_base_url_actually_persists(client):
    """The bug that started this: it silently did not."""
    assert setting("public_base_url") is None
    client.post("/admin/settings/nudges",
                data={"public_base_url": "https://score.example.com/",
                      "nudge_preset": "mon_tue"})
    assert setting("public_base_url") == "https://score.example.com"  # slash trimmed


def test_the_warning_shows_only_while_it_is_unset(client):
    assert "Nudges are <strong>off</strong>" in client.get("/admin/settings").text
    client.post("/admin/settings/nudges",
                data={"public_base_url": "https://score.example.com",
                      "nudge_preset": "mon_tue"})
    assert "Nudges are <strong>off</strong>" not in client.get("/admin/settings").text


def test_a_plain_page_load_confirms_nothing(client):
    assert "Saved." not in client.get("/admin/settings").text


def test_more_channels_actually_saves(client):
    """It used to 500: the handler was async, so it ran on the event loop while
    its SQLite connection had been opened in a worker thread, and sqlite3
    refuses to be used across the two. Every save of this panel failed."""
    r = client.post("/admin/settings/channels",
                    data={"teams_webhook_url": "https://teams.example.com/hook",
                          "telegram_bot_token": "123:abc"}, follow_redirects=False)
    assert r.status_code == 303
    assert setting("teams_webhook_url") == "https://teams.example.com/hook"
    assert setting("telegram_bot_token") == "123:abc"
