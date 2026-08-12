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
    # Roll the three additions back off, leaving the pre-migration shape.
    con.execute("DROP TABLE magic_links")
    con.execute("DROP TABLE webauthn_credentials")
    con.execute("DROP TABLE webauthn_challenges")
    con.execute("DROP INDEX idx_users_webauthn_handle")   # before the column
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
