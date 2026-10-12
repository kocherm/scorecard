"""Re-owning a metric is a pick, not a trip through the edit form: the owner on
Admin > Sections & metrics and on the metric's own page is a dropdown that saves
on change."""
import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app.auth import hash_password

PW = "a-fine-password-123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app
    with dbm.get_db() as con:
        dbm.init_db(con)
        for uid, email, name, role, active in ((1, "b@x.co", "Boss", "admin", 1),
                                               (2, "m@x.co", "Mo", "editor", 1),
                                               (3, "g@x.co", "Gone", "editor", 0)):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password, is_active)
                   VALUES (?,?,?,?,?,0,?)""",
                (uid, email, hash_password(PW), name, role, active))
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1, 'Sales', 1)")
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup, direction,
                                    dri_user_id, start_week, sort_order)
               VALUES (7, 1, 'Calls', 'numeric', 'sum', 'up', NULL, '2026-01-05', 1)""")
    return TestClient(app)


def login(c, email):
    c.post("/login", data={"email": email, "password": PW})
    return c


def owner():
    with dbm.get_db() as con:
        return con.execute("SELECT dri_user_id FROM metrics WHERE id = 7").fetchone()[0]


def test_metrics_list_owner_is_a_picker(env):
    body = login(env, "b@x.co").get("/admin/metrics").text
    form = body.split('action="/admin/metrics/7/owner"')[1].split("</form>")[0]
    assert 'onchange="this.form.submit()"' in form
    assert '<option value="2">Mo</option>' in form
    assert "Gone" not in form  # inactive people cannot be picked


def test_set_owner_saves_and_returns_to_the_section(env):
    r = login(env, "b@x.co").post("/admin/metrics/7/owner", data={"dri_user_id": "2"},
                                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/metrics#sec-1"
    assert owner() == 2
    env.post("/admin/metrics/7/owner", data={"dri_user_id": ""})
    assert owner() is None


def test_set_owner_follows_next_but_never_off_site(env):
    c = login(env, "b@x.co")
    r = c.post("/admin/metrics/7/owner", data={"dri_user_id": "2", "next": "/m/7"},
               follow_redirects=False)
    assert r.headers["location"] == "/m/7"
    r = c.post("/admin/metrics/7/owner", data={"dri_user_id": "2", "next": "//evil.example"},
               follow_redirects=False)
    assert r.headers["location"] == "/admin/metrics#sec-1"


def test_inactive_or_unknown_owner_is_refused(env):
    c = login(env, "b@x.co")
    assert c.post("/admin/metrics/7/owner", data={"dri_user_id": "3"}).status_code == 422
    assert c.post("/admin/metrics/7/owner", data={"dri_user_id": "99"}).status_code == 422
    assert c.post("/admin/metrics/99/owner", data={"dri_user_id": "2"}).status_code == 404
    assert owner() is None


def test_only_admins_can_reassign(env):
    c = login(env, "m@x.co")
    r = c.post("/admin/metrics/7/owner", data={"dri_user_id": "2"}, follow_redirects=False)
    assert r.status_code in (303, 403) and owner() is None


def test_metric_page_offers_the_picker_to_admins_only(env):
    c = login(env, "b@x.co")
    assert 'action="/admin/metrics/7/owner"' in c.get("/m/7").text
    c.post("/logout")
    c.cookies.clear()
    assert 'action="/admin/metrics/7/owner"' not in login(c, "m@x.co").get("/m/7").text


# ------------------------------------------------- Users page: hand metrics over
def add_metric(mid, name, dri, archived=False):
    with dbm.get_db() as con:
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup, direction,
                                    dri_user_id, start_week, sort_order, archived_at)
               VALUES (?, 1, ?, 'numeric', 'sum', 'up', ?, '2026-01-05', ?, ?)""",
            (mid, name, dri, mid, "2026-02-01" if archived else None))


def owners():
    with dbm.get_db() as con:
        return dict(con.execute("SELECT id, dri_user_id FROM metrics").fetchall())


def test_drawer_lists_what_each_person_owns_with_a_picker(env):
    add_metric(8, "Demos", 2)
    body = login(env, "b@x.co").get("/admin/users").text
    drawer = body.split('id="usr-2"')[1].split('class="usr-drawer"')[0]
    assert "Metrics they own (1)" in drawer
    assert 'action="/admin/metrics/8/owner"' in drawer
    assert 'value="/admin/users?open=2#usr-2"' in drawer
    assert 'action="/admin/users/2/handover"' in drawer
    # The hand-over list offers active people other than the owner.
    hand = drawer.split('action="/admin/users/2/handover"')[1].split("</form>")[0]
    assert ">Boss<" in hand and ">Mo<" not in hand and ">Gone<" not in hand


def test_hand_over_moves_every_live_metric_and_reopens_the_drawer(env):
    add_metric(8, "Demos", 3)
    add_metric(9, "Calls booked", 3)
    add_metric(10, "Old one", 3, archived=True)
    c = login(env, "b@x.co")
    r = c.post("/admin/users/3/handover", data={"to_user_id": "2"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/users?open=3&handed=2&to=2#usr-3"
    o = owners()
    assert o[8] == 2 and o[9] == 2
    assert o[10] == 3            # archived rows keep the owner they had
    page = c.get(r.headers["location"]).text
    assert "Handed 2 metrics from Gone to Mo." in page
    assert '<tr class="usr-drawer" id="usr-3">' in page        # open, not hidden


def test_hand_over_refuses_self_and_inactive_targets(env):
    add_metric(8, "Demos", 2)
    c = login(env, "b@x.co")
    assert c.post("/admin/users/2/handover", data={"to_user_id": "2"}).status_code == 422
    assert c.post("/admin/users/2/handover", data={"to_user_id": "3"}).status_code == 422
    assert c.post("/admin/users/99/handover", data={"to_user_id": "1"}).status_code == 404
    assert owners()[8] == 2


def test_deactivate_note_warns_when_they_still_own_metrics(env):
    add_metric(8, "Demos", 2)
    drawer = login(env, "b@x.co").get("/admin/users").text.split('id="usr-2"')[1]
    assert "hand the 1 above to someone first" in drawer


# ----------------------------------------- "no owner" warnings name the metrics
def test_users_banner_names_unowned_and_deactivated_owner_metrics(env):
    add_metric(8, "Demos", 3)                  # owned by someone deactivated
    add_metric(9, "Fine", 2)
    body = login(env, "b@x.co").get("/admin/users").text
    banner = body.split('class="flash warn"')[1].split("</div>")[0]
    assert "2 live metrics have no active owner" in banner
    assert '<a href="/admin/metrics#met-7">Calls</a>' in banner
    assert '<a href="/admin/metrics#met-8">Demos</a>' in banner and "Gone, deactivated" in banner
    assert "Fine" not in banner


def test_metric_rows_carry_the_anchor_the_warnings_link_to(env):
    assert '<tr id="met-7">' in login(env, "b@x.co").get("/admin/metrics").text


def test_status_check_lists_each_unowned_metric(env):
    from datetime import datetime, timezone
    from app import readiness
    add_metric(8, "Demos", 3)
    with dbm.get_db() as con:
        c = next(c for c in readiness.local_checks(con, datetime.now(timezone.utc))
                 if c.key == "dris")
    assert c.state == readiness.WARN
    assert [(i["name"], i["url"]) for i in c.items] == [
        ("Calls", "/admin/metrics#met-7"), ("Demos", "/admin/metrics#met-8")]
    assert "Gone is deactivated" in c.items[1]["detail"]
    page = login(env, "b@x.co").get("/admin/status").text
    assert '<a href="/admin/metrics#met-8">Demos</a>' in page
