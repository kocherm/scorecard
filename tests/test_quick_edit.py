"""The board's pencil: quick-edit a number without leaving the board.

Covers who gets the affordance, which week it opens on, that a save swaps back
the whole ROW (not just the cell), the two shapes the editor takes for status
and binary metrics, and the API-overwrite caution.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import weeks as wk
from app.auth import hash_password

PW = "a-fine-password-123"


def due_week():
    return wk.last_closed_week(datetime.now(timezone.utc)).isoformat()


def cur_week():
    return wk.current_week(datetime.now(timezone.utc)).isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    start = (wk.parse_week(due_week()) - timedelta(days=70)).isoformat()
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        for uid, email, name, role in ((1, "boss@x.co", "Boss Person", "admin"),
                                       (2, "ed@x.co", "Eddie Owner", "editor"),
                                       (3, "vi@x.co", "Val Viewer", "viewer")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), name, role))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Eddie Calls', 'numeric', 'sum', ?, 2)""", (start,))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                            start_week, dri_user_id)
                       VALUES (2, 1, 'Client Health', 'status', ?, 2)""", (start,))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                            start_week, dri_user_id)
                       VALUES (3, 1, 'Shipped It', 'binary', ?, 2)""", (start,))
    yield TestClient(app)


def login(client, email="boss@x.co"):
    return client.post("/login", data={"email": email, "password": PW})


def squash(html: str) -> str:
    """The lede is wrapped across lines by the template; assert on its words."""
    return " ".join(html.split())


# ------------------------------------------------------------- the affordance
def test_board_shows_a_pencil_and_a_dialog_mount_for_editors(env):
    client = env
    login(client, "ed@x.co")
    r = client.get("/")
    assert 'class="mpen"' in r.text
    assert 'hx-get="/quick/1"' in r.text
    assert 'id="quickedit"' in r.text
    # Every metric gets one, not only the ones this editor owns: an admin
    # bumping someone else's number on a call is the case that asked for this.
    for mid in (1, 2, 3):
        assert f'hx-get="/quick/{mid}"' in r.text


def test_viewer_gets_neither_pencil_nor_mount_nor_route(env):
    client = env
    login(client, "vi@x.co")
    r = client.get("/")
    assert "Read-only view" in r.text
    assert 'class="mpen"' not in r.text
    assert 'id="quickedit"' not in r.text
    assert client.get("/quick/1").status_code == 403
    assert client.post(f"/quick/1/{due_week()}", data={"value": "9"}).status_code == 403


def test_rows_carry_a_stable_id_for_the_swap(env):
    client = env
    login(client)
    r = client.get("/")
    assert 'id="mrow-1"' in r.text


# ------------------------------------------------------------ what it opens on
def test_dialog_opens_on_the_due_week_with_the_owner_named(env):
    client = env
    login(client)
    r = client.get("/quick/1")
    assert r.status_code == 200
    assert "Eddie Calls" in r.text
    assert f'hx-post="/quick/1/{due_week()}"' in r.text
    assert f'<option value="{due_week()}" selected>' in r.text.replace("\n", " ")
    # The admin is told whose metric they are about to write.
    assert "Eddie Owner" in r.text and "owns this" in r.text
    assert "qeOpen()" in r.text


def test_week_picker_offers_this_week_and_switches_to_it(env):
    client = env
    login(client)
    r = client.get("/quick/1")
    assert cur_week() in r.text
    r = client.get("/quick/1", params={"week": cur_week()})
    assert f'hx-post="/quick/1/{cur_week()}"' in r.text


def test_unknown_week_falls_back_to_the_due_week(env):
    client = env
    login(client)
    r = client.get("/quick/1", params={"week": "1999-01-04"})
    assert f'hx-post="/quick/1/{due_week()}"' in r.text


def test_missing_metric_is_404(env):
    client = env
    login(client)
    assert client.get("/quick/999").status_code == 404


# -------------------------------------------------------------------- saving
def test_save_writes_the_number_and_swaps_the_whole_row_out_of_band(env):
    client = env
    login(client)
    r = client.post(f"/quick/1/{due_week()}", data={"value": "7"})
    assert r.status_code == 200
    assert 'id="mrow-1"' in r.text and 'hx-swap-oob="true"' in r.text
    # The row, not the cell: Actual and the subtotal ride along.
    assert 'class="pin actual">7<' in r.text
    assert "7" in r.text
    with dbm.get_db() as con:
        e = con.execute("SELECT * FROM entries WHERE metric_id=1 AND week_start=?",
                        (due_week(),)).fetchone()
        assert e["value_numeric"] == 7.0
        assert e["source"] == "manual"


def test_save_is_audited_to_the_admin_not_the_owner(env):
    client = env
    login(client)                                   # Boss, id 1; metric is Eddie's
    client.post(f"/quick/1/{due_week()}", data={"value": "4"})
    with dbm.get_db() as con:
        a = con.execute("SELECT * FROM entry_audit WHERE metric_id=1 "
                        "ORDER BY id DESC LIMIT 1").fetchone()
        assert a["actor_user_id"] == 1
        assert a["source"] == "manual"


def test_stepping_then_saving_writes_once(env):
    """The - / + buttons move the FIELD, not the database: four taps then Save
    is one row in the metric's history, not four."""
    client = env
    login(client)
    client.post(f"/quick/1/{due_week()}", data={"value": "11"})
    with dbm.get_db() as con:
        n = con.execute("SELECT COUNT(*) c FROM entry_audit WHERE metric_id=1").fetchone()["c"]
    assert n == 1


def test_empty_value_clears_the_entry(env):
    client = env
    login(client)
    client.post(f"/quick/1/{due_week()}", data={"value": "5"})
    client.post(f"/quick/1/{due_week()}", data={"value": ""})
    with dbm.get_db() as con:
        assert con.execute("SELECT * FROM entries WHERE metric_id=1 AND week_start=?",
                           (due_week(),)).fetchone() is None


def test_a_bad_number_re_renders_the_dialog_rather_than_erroring(env):
    """htmx does not swap a 4xx, so a 422 here is a Save button that silently
    does nothing. The dialog comes back with the message and the typed text."""
    client = env
    login(client)
    r = client.post(f"/quick/1/{due_week()}", data={"value": "twelve"})
    assert r.status_code == 200
    assert "qe-err" in r.text
    assert "Not a number" in r.text
    assert 'value="twelve"' in r.text
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0


def test_future_week_is_refused(env):
    client = env
    nxt = (wk.parse_week(cur_week()) + timedelta(days=7)).isoformat()
    login(client)
    assert client.post(f"/quick/1/{nxt}", data={"value": "3"}).status_code == 422


# ---------------------------------------------------------- non-numeric shapes
def test_status_metric_offers_three_taps_and_no_stepper(env):
    client = env
    login(client)
    r = client.get("/quick/2")
    assert 'value="G"' in r.text and 'value="Y"' in r.text and 'value="R"' in r.text
    assert "qe-step" not in r.text
    r = client.post(f"/quick/2/{due_week()}", data={"value": "G"})
    assert r.status_code == 200
    with dbm.get_db() as con:
        assert con.execute("SELECT value_status s FROM entries WHERE metric_id=2 "
                           "AND week_start=?", (due_week(),)).fetchone()["s"] == "G"


def test_binary_metric_offers_yes_and_no(env):
    client = env
    login(client)
    r = client.get("/quick/3")
    assert ">Yes<" in r.text and ">No<" in r.text
    assert "qe-step" not in r.text
    client.post(f"/quick/3/{due_week()}", data={"value": "0"})
    with dbm.get_db() as con:
        assert con.execute("SELECT value_numeric v FROM entries WHERE metric_id=3 "
                           "AND week_start=?", (due_week(),)).fetchone()["v"] == 0.0


def test_numeric_metric_gets_the_stepper(env):
    client = env
    login(client)
    r = client.get("/quick/1")
    assert "qe-step" in r.text and 'id="qe-value"' in r.text


# ------------------------------------------------------------ the api caution
def test_dialog_warns_when_an_automation_owns_the_number(env):
    """A metric written by the API is upserted absolutely every run, so a hand
    edit is gone by morning. Say so before the edit, not in a support call."""
    client = env
    login(client)
    assert "An automation writes this metric" not in client.get("/quick/1").text
    with dbm.get_db() as con:
        con.execute("""INSERT INTO entry_audit (metric_id, week_start, new_numeric,
                                                source) VALUES (1, ?, 3, 'api')""",
                    (due_week(),))
    assert "An automation writes this metric" in client.get("/quick/1").text


# ------------------------------- the inline cell editor now swaps its row too
def test_clicking_a_cell_still_edits_and_now_refreshes_the_whole_row(env):
    client = env
    login(client)
    r = client.get(f"/cell/1/{due_week()}/edit")
    assert "cell-form" in r.text and 'hx-swap="none"' in r.text
    r = client.post(f"/cell/1/{due_week()}", data={"value": "6"})
    assert r.status_code == 200
    assert 'id="mrow-1"' in r.text
    assert 'class="pin actual">6<' in r.text


def test_save_refreshes_the_summary_above_the_grid(env):
    """The lede and the Act cards are the rows added up. A save that takes a
    metric out of red must not leave "0 of 1 on target" and a File 1-3-1 button
    sitting above a green row."""
    client = env
    login(client)
    # Two red weeks running, so the board offers a 1-3-1 for this metric.
    with dbm.get_db() as con:
        con.execute("INSERT INTO targets (metric_id,year,quarter,baseline_value,"
                    "stretch_value) VALUES (1,?,?,10,10)",
                    wk.quarter_of(wk.parse_week(due_week())))
    prev = (wk.parse_week(due_week()) - timedelta(days=7)).isoformat()
    client.post(f"/quick/1/{prev}", data={"value": "1"})
    client.post(f"/quick/1/{due_week()}", data={"value": "1"})
    board = squash(client.get("/").text)
    assert "File 1-3-1" in board
    assert "0 of 3</b> on target" in board and "needs a 1-3-1" in board

    r = client.post(f"/quick/1/{due_week()}", data={"value": "12"})
    assert 'id="board-lede" hx-swap-oob="true"' in r.text
    after = squash(r.text)
    assert "1 of 3</b> on target" in after
    assert "needs a 1-3-1" not in after
    assert "File 1-3-1" not in after
