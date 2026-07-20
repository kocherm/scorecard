"""Sessions, passwords, role guards, API bearer tokens."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param

from .db import db_dep

SESSION_COOKIE = "scorecard_session"
SESSION_DAYS = 30

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except VerifyMismatchError:
        return False


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def create_session(con: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    con.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?,?,?)",
        (_sha256(token), user_id, expires),
    )
    return token


def destroy_session(con: sqlite3.Connection, token: str) -> None:
    con.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))


def session_hash(request: Request) -> Optional[str]:
    """Hash of the current session cookie, for targeting this session's row."""
    token = request.cookies.get(SESSION_COOKIE)
    return _sha256(token) if token else None


def user_from_request(request: Request, con: sqlite3.Connection) -> Optional[sqlite3.Row]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = con.execute(
        """SELECT u.*, s.impersonate_user_id FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1""",
        (_sha256(token), datetime.now(timezone.utc).isoformat()),
    ).fetchone()
    if row:
        con.execute(
            "UPDATE sessions SET last_seen_at = datetime('now') WHERE token_hash = ?",
            (_sha256(token),),
        )
    # View-as-user: an admin's session may carry an impersonation target. The
    # effective user is returned (so role checks, nav, and every surface match
    # what the target sees); the real admin rides along on request.state for
    # the banner and for audit attribution. Fails safe: a demoted admin or a
    # deactivated/deleted target falls back to the real identity.
    if row and row["impersonate_user_id"] and row["role"] == "admin":
        target = con.execute(
            "SELECT * FROM users WHERE id = ? AND is_active = 1",
            (row["impersonate_user_id"],)).fetchone()
        if target is not None:
            request.state.impersonator = row
            row = target
    if row is not None and row["role"] != "viewer":
        # Nav badge: how many of the (effective) user's numbers are still
        # missing for the due week. Real DB by design, even in demo mode.
        from . import entry_ops
        request.state.checkin_missing = len(entry_ops.missing_due_metrics(
            con, row["id"], datetime.now(timezone.utc)))
    return row


class RequireRole:
    """Dependency: current user with at least the given role."""

    ORDER = {"viewer": 0, "editor": 1, "admin": 2}

    def __init__(self, role: str):
        self.role = role

    def __call__(self, request: Request, con: sqlite3.Connection = Depends(db_dep)):
        user = user_from_request(request, con)
        if user is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        if self.ORDER[user["role"]] < self.ORDER[self.role]:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user


require_viewer = RequireRole("viewer")
require_editor = RequireRole("editor")
require_admin = RequireRole("admin")


# ---------------------------------------------------------------- magic links
MAGIC_LINK_DAYS = 7


def create_magic_link(con: sqlite3.Connection, user_id: int,
                      days: int = MAGIC_LINK_DAYS) -> str:
    """Pre-authenticated check-in link token (delivered over Slack DM).
    Multi-use until expiry: Slack's link crawler may GET the URL before the
    human does, so single-use tokens would be burned on arrival."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    con.execute(
        "INSERT INTO magic_links (token_hash, user_id, expires_at) VALUES (?,?,?)",
        (_sha256(token), user_id, expires))
    return token


def consume_magic_link(con: sqlite3.Connection, token: str) -> Optional[int]:
    """user_id for a valid unexpired link belonging to an active user, else None."""
    row = con.execute(
        """SELECT ml.user_id FROM magic_links ml
           JOIN users u ON u.id = ml.user_id
           WHERE ml.token_hash = ? AND ml.expires_at > ? AND u.is_active = 1""",
        (_sha256(token), datetime.now(timezone.utc).isoformat())).fetchone()
    if row is None:
        return None
    con.execute("UPDATE magic_links SET last_used_at = datetime('now') "
                "WHERE token_hash = ?", (_sha256(token),))
    return row["user_id"]


def api_token_from_request(request: Request, con: sqlite3.Connection,
                           need_write: bool = False,
                           need_admin: bool = False) -> sqlite3.Row:
    """Resolve a bearer token. 'admin' implies write; structural changes
    (archiving a metric off the board) require it explicitly."""
    auth = request.headers.get("Authorization", "")
    scheme, token = get_authorization_scheme_param(auth)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    row = con.execute(
        "SELECT * FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
        (_sha256(token),),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    if need_admin and row["scope"] != "admin":
        raise HTTPException(status_code=403, detail="Token lacks admin scope")
    if need_write and row["scope"] == "read":
        raise HTTPException(status_code=403, detail="Token is read-only")
    con.execute("UPDATE api_tokens SET last_used_at = datetime('now') WHERE id = ?", (row["id"],))
    return row


def new_api_token(con: sqlite3.Connection, name: str, scope: str,
                  created_by: Optional[int]) -> str:
    token = "sc_" + secrets.token_urlsafe(32)
    con.execute(
        "INSERT INTO api_tokens (name, token_hash, scope, created_by) VALUES (?,?,?,?)",
        (name, _sha256(token), scope, created_by),
    )
    return token
