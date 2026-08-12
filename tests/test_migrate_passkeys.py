"""migrate.passkeys against a DB shaped like production before reset links and
passkeys existed: columns are added, outstanding links keep their meaning."""
import sqlite3

import pytest

from app import db as dbm
from app.auth import hash_password
from migrate import passkeys as mp

OLD_MAGIC_LINKS = """
CREATE TABLE magic_links (
    id           INTEGER PRIMARY KEY,
    token_hash   TEXT    NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT    NOT NULL,
    last_used_at TEXT
)
"""


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "old.db"))
    con = dbm.connect()
    script = dbm.SCHEMA_PATH.read_text()
    con.executescript(script)
    # Roll the additions back off, leaving the genuine pre-migration shape:
    # no webauthn tables, no handle column, and the old magic_links without
    # purpose. Tests then run init_db/migrate against THIS, which is what
    # production actually upgrades from.
    con.execute("DROP TABLE magic_links")
    con.execute("DROP TABLE webauthn_credentials")
    con.execute("DROP TABLE webauthn_challenges")
    con.execute("ALTER TABLE users DROP COLUMN webauthn_handle")
    con.executescript(OLD_MAGIC_LINKS)
    con.execute(
        """INSERT INTO users (id, email, password_hash, display_name, role)
           VALUES (2,'ed@x.co',?,'Eddie','editor')""", (hash_password("x" * 12),))
    con.execute("INSERT INTO magic_links (token_hash, user_id, expires_at) "
                "VALUES ('abc', 2, '2099-01-01T00:00:00+00:00')")
    con.commit()
    con.close()
    yield


def test_init_db_survives_a_pre_migration_database(old_db):
    """The upgrade path, which is the one that actually broke.

    lifespan calls init_db BEFORE any migration, and schema.sql runs against
    live databases as well as empty ones. On an existing DB every CREATE TABLE
    IF NOT EXISTS is a no-op, so users still has no webauthn_handle - and an
    index over that column in schema.sql took the whole script down, so the app
    could not start to run the migration that would have added it. Building the
    fixture from the new schema and then removing pieces hid this; only calling
    init_db on the old shape reproduces it."""
    with dbm.get_db() as con:
        dbm.init_db(con)                    # must not raise
        assert mp.needs_handle(con)         # and must not have faked the column
        mp.migrate(con)
        assert not mp.needs_handle(con)


def test_the_handle_index_exists_after_a_fresh_create(tmp_path, monkeypatch):
    """Fresh databases must end up with the same constraints as upgraded ones,
    even though the index now lives in the migration."""
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "fresh.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        mp.migrate(con)
        idx = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_users_webauthn_handle'")]
    assert idx == ["idx_users_webauthn_handle"]


def test_migration_adds_the_columns_and_tables(old_db):
    with dbm.get_db() as con:
        assert mp.needs_purpose(con) and mp.needs_handle(con)
        done = mp.migrate(con)
    assert len(done) == 3
    with dbm.get_db() as con:
        assert not mp.needs_purpose(con) and not mp.needs_handle(con)
        assert con.execute("SELECT COUNT(*) c FROM webauthn_credentials"
                           ).fetchone()["c"] == 0


def test_existing_links_stay_checkin_links(old_db):
    """The default is the point: an outstanding link was a check-in link, and
    must not silently become one that can set a password."""
    with dbm.get_db() as con:
        mp.migrate(con)
        row = con.execute("SELECT purpose FROM magic_links "
                          "WHERE token_hash='abc'").fetchone()
    assert row["purpose"] == "checkin"


def test_the_widened_column_still_rejects_a_bad_purpose(old_db):
    with dbm.get_db() as con:
        mp.migrate(con)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO magic_links (token_hash, user_id, purpose, "
                        "expires_at) VALUES ('z', 2, 'nonsense', '2099-01-01')")


def test_migration_is_idempotent(old_db):
    with dbm.get_db() as con:
        mp.migrate(con)
    with dbm.get_db() as con:
        assert mp.migrate(con) == []


def test_handles_are_unique(old_db):
    with dbm.get_db() as con:
        mp.migrate(con)
        con.execute("INSERT INTO users (id, email, password_hash, display_name, "
                    "role, webauthn_handle) VALUES (3,'b@x.co','h','B','editor','H1')")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO users (id, email, password_hash, "
                        "display_name, role, webauthn_handle) "
                        "VALUES (4,'c@x.co','h','C','editor','H1')")
        # ...but NULL is not a value, so any number of users may have no passkey.
        con.execute("INSERT INTO users (id, email, password_hash, display_name, "
                    "role) VALUES (5,'d@x.co','h','D','editor')")
        con.execute("INSERT INTO users (id, email, password_hash, display_name, "
                    "role) VALUES (6,'e@x.co','h','E','editor')")
