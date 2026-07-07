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


def user_from_request(request: Request, con: sqlite3.Connection) -> Optional[sqlite3.Row]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = con.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1""",
        (_sha256(token), datetime.now(timezone.utc).isoformat()),
    ).fetchone()
    if row:
        con.execute(
            "UPDATE sessions SET last_seen_at = datetime('now') WHERE token_hash = ?",
            (_sha256(token),),
        )
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


def api_token_from_request(request: Request, con: sqlite3.Connection,
                           need_write: bool = False) -> sqlite3.Row:
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
