"""The board's first screen: one sentence, then the act cards the TV already
had. Both read the same GridVM/ActionItem path the grid below them renders."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import weeks as wk
from app.auth import hash_password

PW = "a-fine-password-123"


def due_week():
    return wk.last_closed_week(datetime.now(timezone.utc))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    due = due_week()
    start = (due - timedelta(days=56)).isoformat()
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  must_change_password) VALUES (1,'b@x.co',?,'Boss','admin',0)""",
            (hash_password(PW),))
        for mid, name in ((1, "Red Metric"), (2, "Green Metric"), (3, "Silent Metric")):
            con.execute(
                """INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                        start_week, dri_user_id)
                   VALUES (?,1,?,'numeric','sum',?,1)""", (mid, name, start))
            con.execute(
                """INSERT INTO targets (metric_id, year, quarter, baseline_value,
                                        stretch_value) VALUES (?,?,?,10,10)""",
                (mid,) + wk.quarter_of(due))
        # Red Metric: three closed weeks well under target -> a red streak.
        for i in range(3):
            dbm.upsert_entry(con, 1, due - timedelta(days=7 * i), value_numeric=1.0,
                             source="manual", user_id=1)
        dbm.upsert_entry(con, 2, due, value_numeric=12.0, source="manual", user_id=1)
        # Silent Metric gets nothing at all.
    client = TestClient(app)
    client.post("/login", data={"email": "b@x.co", "password": PW})
    return client


def test_board_opens_with_a_sentence_not_a_counter_strip(env):
    r = env.get("/")
    assert "lede-line" in r.text and "on target" in r.text
    assert "1 of 3" in r.text
    assert 'class="stat green"' not in r.text     # the five boxes are gone


def test_act_cards_carry_the_owner_and_the_next_step(env):
    r = env.get("/")
    assert 'class="acts"' in r.text and "Act on this" in r.text
    assert "Red Metric" in r.text and "RED WK 3" in r.text
    assert f"/131/1/{due_week().isoformat()}" in r.text   # the button, not a label
    assert "Green Metric" not in r.text.split('class="acts"')[1].split("</section>")[0]


def test_escalation_and_reporting_are_counted_as_separate_axes(env):
    """A red streak survives a week with no number, so the two clauses overlap
    by design - the sentence must not add them into a bogus total."""
    with dbm.get_db() as con:
        con.execute("DELETE FROM entries WHERE metric_id=1 AND week_start=?",
                    (due_week().isoformat(),))
    r = env.get("/")
    assert "RED WK 2" in r.text          # still escalating, though nothing came in
    # Whether an unreported week reads as "never came in" or "still due" depends
    # on the staleness deadline (Wed 08:00), so accept either - the point is that
    # the same metric is counted on BOTH axes at once.
    assert ("never came in" in r.text) or ("still due" in r.text)


# ---------------------------------------------------------------- grid shape
def test_month_totals_sit_outside_the_timeline(env):
    """A subtotal between two week columns is a non-time-series value inserted
    into a time series. Every week cell must come before every subtotal."""
    body = env.get("/").text
    tbody = body.split("<tbody>")[1].split("</tbody>")[0]
    rows = [r for r in tbody.split("<tr ")[1:] if 'class="cell' in r]
    assert rows
    for r in rows:      # within EACH row, every week cell precedes every total
        assert r.rindex('class="cell') < r.index('class="subtotal ')


def test_the_due_week_column_is_marked_for_emphasis(env):
    body = env.get("/").text
    assert 'class="wk due"' in body
    assert "due-week" in body


def test_rows_carry_what_the_filters_need(env):
    body = env.get("/").text
    assert 'data-state=' in body and 'data-mine=' in body and 'data-key=' in body
    # The admin owns every metric in this fixture, so all rows are "mine".
    assert 'data-mine="1"' in body and 'data-mine="0"' not in body


def test_owner_is_named_on_every_row(env):
    body = env.get("/").text
    assert 'class="mdri"' in body
    assert "no owner" in body or 'class="av"' in body
