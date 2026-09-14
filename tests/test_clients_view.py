"""The Clients TV view: every roster row on one wall, never folded, and the one
view that joins the rotation by itself - only while the board is folding
clients away."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import grid as gridm
from app.main import _tv_view

NOW = datetime.now(timezone.utc)


def make_db(tmp_path, monkeypatch, clients: int):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        dbm.set_setting(con, "display_token", "tok")
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (2,'Client Health',1)")
        for i in range(10):
            con.execute(
                """INSERT INTO metrics (section_id, name, metric_type, rollup, direction,
                                        start_week, sort_order)
                   VALUES (1, ?, 'numeric', 'sum', 'up', '2026-01-05', ?)""", (f"N{i}", i))
        for i in range(clients):
            con.execute(
                """INSERT INTO metrics (section_id, name, metric_type, direction,
                                        start_week, sort_order)
                   VALUES (2, ?, 'status', 'up', '2026-01-05', ?)""", (f"Client {i:02d}", i))


def views_over_a_rotation(con, tv):
    return {_tv_view(con, tv, datetime.fromtimestamp(t, timezone.utc))
            for t in range(0, 45 * 6, 45)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    def build(clients, views="board", rotate="45"):
        make_db(tmp_path, monkeypatch, clients)
        with dbm.get_db() as con:
            dbm.set_setting(con, "display_views", views)
            dbm.set_setting(con, "display_rotate_seconds", rotate)
        return dbm.get_db
    return build


def test_wall_shows_every_client_even_when_the_board_folds(env):
    get_db = env(40)
    with get_db() as con:
        tv = gridm.build_tv(con, NOW)
        assert any(sec.hidden for col in tv.columns for sec in col)
        assert len(tv.roster) == 40
        assert tv.roster_cols == gridm._roster_cols(40)
    from app.main import app
    html = TestClient(app).get("/display/body",
                               params={"token": "tok", "view": "clients"}).text
    assert html.count('class="vcl-tile') == 40


def test_clients_joins_the_rotation_only_while_the_board_folds(env):
    get_db = env(40)
    with get_db() as con:
        tv = gridm.build_tv(con, NOW)
        assert views_over_a_rotation(con, tv) == {"board", "clients"}
        assert tv.roster_rotates


def test_clients_stays_out_when_everyone_fits(env):
    get_db = env(6)
    with get_db() as con:
        tv = gridm.build_tv(con, NOW)
        assert views_over_a_rotation(con, tv) == {"board"}
        assert not tv.roster_rotates


def test_no_rotation_means_no_self_enrolment(env):
    get_db = env(40, rotate="0")
    with get_db() as con:
        tv = gridm.build_tv(con, NOW)
        assert views_over_a_rotation(con, tv) == {"board"}
        assert not tv.roster_rotates
