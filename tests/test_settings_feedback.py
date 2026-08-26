"""Saving a settings panel should land you back on that panel and say so.

The bare redirect this replaces looked identical whether or not anything was
written, which is how a public base URL got filled in, discarded by a sibling
toggle's page reload, and reported as saved."""
import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm
from app import readiness
from app.main import SETTINGS_TABS


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
    # Anchor so the browser restores position, query param so the page can say
    # so, tab because the anchor cannot land on a panel that is not rendered.
    assert r.headers["location"] == "/admin/settings?tab=notify&saved=nudges#nudges"

    page = client.get("/admin/settings?tab=notify&saved=nudges").text
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
    tab = SETTINGS_TABS[anchor]
    assert r.headers["location"] == f"/admin/settings?tab={tab}&saved={anchor}#{anchor}"
    # And the panel it points at is actually on that tab.
    assert f'id="{anchor}"' in client.get(f"/admin/settings?tab={tab}").text


def test_public_base_url_actually_persists(client):
    """The bug that started this: it silently did not."""
    assert setting("public_base_url") is None
    client.post("/admin/settings/nudges",
                data={"public_base_url": "https://score.example.com/",
                      "nudge_preset": "mon_tue"})
    assert setting("public_base_url") == "https://score.example.com"  # slash trimmed


def test_the_warning_shows_only_while_it_is_unset(client):
    notify = "/admin/settings?tab=notify"
    assert "Nudges are <strong>off</strong>" in client.get(notify).text
    client.post("/admin/settings/nudges",
                data={"public_base_url": "https://score.example.com",
                      "nudge_preset": "mon_tue"})
    assert "Nudges are <strong>off</strong>" not in client.get(notify).text


@pytest.mark.parametrize("tab", ["display", "notify", "advanced"])
def test_a_plain_page_load_confirms_nothing(client, tab):
    assert "Saved." not in client.get(f"/admin/settings?tab={tab}").text


def test_every_panel_lives_on_exactly_one_tab(client):
    """A panel rendered under no tab is unreachable; one rendered under two
    would take a "Saved." flash it did not earn."""
    pages = {t: client.get(f"/admin/settings?tab={t}").text
             for t in ("display", "notify", "advanced")}
    for panel, tab in SETTINGS_TABS.items():
        on = [t for t, body in pages.items() if f'id="{panel}"' in body]
        assert on == [tab], f"{panel} rendered on {on}, expected [{tab}]"


def test_an_unknown_tab_falls_back_rather_than_rendering_nothing(client):
    body = client.get("/admin/settings?tab=nonsense").text
    assert 'id="tv-display"' in body


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
