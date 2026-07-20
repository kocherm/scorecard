"""Archive/unarchive over the API: the admin-scope gate, backdating to the
week a client actually churned, and that the scoring path honours both."""
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm
from app import grid as gridm
from app import weeks as wk

START = "2026-01-05"  # a Monday


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app  # imported late so DB_PATH is already patched

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Client Health',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (1,'dri@example.com','x','DRI','admin')""")
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, dri_user_id, start_week)
               VALUES (1, 1, 'Acme Co', 'status', 1, ?)""", (START,))
        tokens = {s: auth.new_api_token(con, s, s, 1) for s in ("read", "read_write", "admin")}

    # TestClient is not used as a context manager on purpose: that keeps the
    # lifespan (and its APScheduler jobs) from starting during tests.
    yield TestClient(app), tokens


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def archived_at(metric_id=1):
    with dbm.get_db() as con:
        return con.execute("SELECT archived_at FROM metrics WHERE id=?",
                           (metric_id,)).fetchone()["archived_at"]


# ---------------- privilege gate

def test_read_write_token_cannot_archive(env):
    client, tokens = env
    r = client.post("/api/v1/metrics/1/archive", json={}, headers=hdr(tokens["read_write"]))
    assert r.status_code == 403
    assert "admin scope" in r.json()["detail"]
    assert archived_at() is None


def test_read_token_cannot_archive(env):
    client, tokens = env
    r = client.post("/api/v1/metrics/1/archive", json={}, headers=hdr(tokens["read"]))
    assert r.status_code == 403


def test_archive_requires_a_token_at_all(env):
    client, _ = env
    assert client.post("/api/v1/metrics/1/archive", json={}).status_code == 401


def test_admin_token_still_reads_and_writes(env):
    client, tokens = env
    assert client.get("/api/v1/scorecard", headers=hdr(tokens["admin"])).status_code == 200
    r = client.post("/api/v1/metrics/1/entries", json={"status": "G"},
                    headers=hdr(tokens["admin"]))
    assert r.status_code == 200


# ---------------- archiving

def test_admin_archives_and_metric_leaves_the_list(env):
    client, tokens = env
    r = client.post("/api/v1/metrics/1/archive", json={}, headers=hdr(tokens["admin"]))
    assert r.status_code == 200
    body = r.json()
    assert body["archived"] is True and body["was_already_archived"] is False
    assert body["name"] == "Acme Co"
    # defaults to this week
    assert body["effective_week"] == wk.current_week(datetime.now(timezone.utc)).isoformat()

    live = client.get("/api/v1/metrics", headers=hdr(tokens["admin"])).json()
    assert [m["id"] for m in live] == []
    everything = client.get("/api/v1/metrics?include_archived=true",
                            headers=hdr(tokens["admin"])).json()
    assert [m["id"] for m in everything] == [1]
    assert everything[0]["archived_at"] == body["effective_week"]


def test_archive_works_with_no_body_at_all(env):
    # "archive metric 1" with no JSON payload is the common agent call
    client, tokens = env
    r = client.post("/api/v1/metrics/1/archive", headers=hdr(tokens["admin"]))
    assert r.status_code == 200
    assert r.json()["effective_week"] == wk.current_week(
        datetime.now(timezone.utc)).isoformat()


def test_archived_row_leaves_the_board_whatever_the_effective_week(env):
    """The behaviour that actually ships: every surface filters archived_at IS
    NULL, so the archive date does not decide whether the row is displayed - it
    is gone either way. Guards against re-selling effective_week as a display
    fix."""
    client, tokens = env
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)

    def board_names():
        with dbm.get_db() as con:  # default flag = what TV/edit grid/API call
            vm = gridm.build_grid(con, now)
        return [r.name for s in vm.sections for r in s.rows]

    assert board_names() == ["Acme Co"]
    client.post("/api/v1/metrics/1/archive", json={"effective_week": "2026-06-29"},
                headers=hdr(tokens["admin"]))
    assert board_names() == []          # backdated: gone
    client.post("/api/v1/metrics/1/archive", json={"effective_week": "2026-07-13"},
                headers=hdr(tokens["admin"]))
    assert board_names() == []          # archived this week: equally gone


def test_backdated_archive_scores_na_only_in_the_include_archived_view(env):
    """effective_week drives scoring's na-tail, but ONLY through
    build_grid(include_archived=True) - a flag no shipped view passes. This pins
    the latent behaviour so a future 'show archived history' view can rely on it;
    it is not evidence of anything users see today."""
    client, tokens = env
    for w in ("2026-06-22", "2026-06-29", "2026-07-06"):
        client.post("/api/v1/metrics/1/entries", json={"week_start": w, "status": "G"},
                    headers=hdr(tokens["admin"]))

    r = client.post("/api/v1/metrics/1/archive", json={"effective_week": "2026-06-29"},
                    headers=hdr(tokens["admin"]))
    assert r.status_code == 200
    assert r.json()["effective_week"] == "2026-06-29"
    assert archived_at() == "2026-06-29"

    with dbm.get_db() as con:
        vm = gridm.build_grid(con, datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
                              include_archived=True)
    row = vm.sections[0].rows[0]
    states = {c.week: c.state.value for c in row.cells}
    assert states[date(2026, 6, 22)] == "green"   # before the churn: real history
    assert states[date(2026, 6, 29)] == "na"      # from the churn week on: gone
    assert states[date(2026, 7, 6)] == "na"


def test_rearchiving_moves_the_effective_week(env):
    client, tokens = env
    client.post("/api/v1/metrics/1/archive", json={"effective_week": "2026-07-06"},
                headers=hdr(tokens["admin"]))
    r = client.post("/api/v1/metrics/1/archive", json={"effective_week": "2026-06-29"},
                    headers=hdr(tokens["admin"]))
    assert r.json()["was_already_archived"] is True
    assert archived_at() == "2026-06-29"


def test_archive_rejects_future_week(env):
    client, tokens = env
    future = (wk.current_week(datetime.now(timezone.utc)).toordinal() + 7)
    r = client.post("/api/v1/metrics/1/archive",
                    json={"effective_week": date.fromordinal(future).isoformat()},
                    headers=hdr(tokens["admin"]))
    assert r.status_code == 422
    assert "future" in r.json()["detail"]
    assert archived_at() is None


def test_archive_rejects_week_before_metric_start(env):
    client, tokens = env
    r = client.post("/api/v1/metrics/1/archive", json={"effective_week": "2025-12-29"},
                    headers=hdr(tokens["admin"]))
    assert r.status_code == 422
    assert archived_at() is None


def test_archive_rejects_non_monday(env):
    client, tokens = env
    r = client.post("/api/v1/metrics/1/archive", json={"effective_week": "2026-07-01"},
                    headers=hdr(tokens["admin"]))
    assert r.status_code == 422
    assert archived_at() is None


def test_archive_unknown_metric_404s(env):
    client, tokens = env
    r = client.post("/api/v1/metrics/999/archive", json={}, headers=hdr(tokens["admin"]))
    assert r.status_code == 404


# ---------------- unarchiving

def test_unarchive_restores_the_metric(env):
    client, tokens = env
    client.post("/api/v1/metrics/1/archive", json={}, headers=hdr(tokens["admin"]))
    r = client.post("/api/v1/metrics/1/unarchive", headers=hdr(tokens["admin"]))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "metric_id": 1, "name": "Acme Co",
                        "archived": False, "was_archived": True}
    assert archived_at() is None
    live = client.get("/api/v1/metrics", headers=hdr(tokens["admin"])).json()
    assert [m["id"] for m in live] == [1]


def test_unarchive_needs_admin_scope(env):
    client, tokens = env
    r = client.post("/api/v1/metrics/1/unarchive", headers=hdr(tokens["read_write"]))
    assert r.status_code == 403


# ---------------- entries still refuse archived metrics

def test_entries_still_rejected_for_archived_metric(env):
    client, tokens = env
    client.post("/api/v1/metrics/1/archive", json={}, headers=hdr(tokens["admin"]))
    r = client.post("/api/v1/metrics/1/entries", json={"status": "G"},
                    headers=hdr(tokens["admin"]))
    assert r.status_code == 404
