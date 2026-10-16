"""Sessions, passwords, role guards, API bearer tokens."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param

from .db import db_dep

SESSION_COOKIE = "scorecard_session"
SESSION_DAYS = 30
# A session minted from a check-in link (scope 'checkin') is LIMITED: half a
# day, never extended, and refused everywhere outside the check-in allowlist
# below. The link is DM'd weekly and lives seven days; if redeeming it gave the
# owner's full role for 30 days, an admin's nudge would be an admin session -
# and, through require_self, a passkey that outlives everything - for whoever
# opened it first.
LINK_SESSION_HOURS = 12
FULL_SCOPE, CHECKIN_SCOPE = "full", "checkin"

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


def session_ttl(scope: str = FULL_SCOPE) -> timedelta:
    """Lifetime of a session of this scope - also the cookie's max_age."""
    if scope == FULL_SCOPE:
        return timedelta(days=SESSION_DAYS)
    return timedelta(hours=LINK_SESSION_HOURS)


def create_session(con: sqlite3.Connection, user_id: int,
                   scope: str = FULL_SCOPE) -> str:
    """expires_at is fixed here and never moved: activity does not extend a
    session, so a limited one really does end LINK_SESSION_HOURS later."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + session_ttl(scope)).isoformat()
    con.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, scope) "
        "VALUES (?,?,?,?)",
        (_sha256(token), user_id, expires, scope),
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
        """SELECT u.*, s.impersonate_user_id, s.scope AS session_scope
           FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1""",
        (_sha256(token), datetime.now(timezone.utc).isoformat()),
    ).fetchone()
    if row:
        con.execute(
            "UPDATE sessions SET last_seen_at = datetime('now') WHERE token_hash = ?",
            (_sha256(token),),
        )
        # Anything but 'full' is limited - fail closed on an unknown value.
        request.state.limited_session = row["session_scope"] != FULL_SCOPE
        if request.state.limited_session:
            # No view-as from a limited session: starting one needs
            # require_admin, which refuses it, but never honour a stale target.
            return _with_badges(request, con, row)
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
    return _with_badges(request, con, row)


def _with_badges(request: Request, con: sqlite3.Connection,
                 row: Optional[sqlite3.Row]) -> Optional[sqlite3.Row]:
    if row is not None and row["role"] != "viewer":
        # Nav badge: how many of the (effective) user's numbers are still
        # missing for the due week. Real DB by design, even in demo mode.
        from . import entry_ops
        request.state.checkin_missing = len(entry_ops.missing_due_metrics(
            con, row["id"], datetime.now(timezone.utc)))
    if row is not None and row["role"] == "admin":
        # Nav badge: how many setup checks are blocking. Local checks and the
        # cached verification results only - all DB reads, no network, or every
        # admin page would wait on Slack.
        from . import readiness
        request.state.setup_blocked = readiness.blocked_count(
            con, datetime.now(timezone.utc))
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
        if is_limited(request) and (self.role == "admin"
                                    or not link_scope_allows(request)):
            raise full_login_required(request)
        return user


def is_limited(request: Request) -> bool:
    """True when this request rides a check-in-link session (scope 'checkin')."""
    return bool(getattr(request.state, "limited_session", False))


# Everything a LIMITED session may reach - deny by default, so a route added
# tomorrow is refused to a check-in link until someone decides otherwise. The
# check-in pages and their POSTs, 1-3-1s (the routes additionally insist the
# metric is yours), and READING the board, the CEO page and metric pages.
# Never: require_admin (refused outright, whatever this list says),
# require_self (credentials), /account (password), board edits, view-as.
_LINK_ANY_METHOD = ("/checkin/", "/131/")
_LINK_READ_EXACT = ("/", "/ceo")
_LINK_READ_PREFIX = ("/m/",)


def link_scope_allows(request: Request) -> bool:
    path = request.url.path
    if path == "/checkin" or path.startswith(_LINK_ANY_METHOD):
        return True
    if request.method in ("GET", "HEAD"):
        return path in _LINK_READ_EXACT or path.startswith(_LINK_READ_PREFIX)
    return False


FULL_LOGIN_MESSAGE = "Sign in with your password to do that."


def full_login_required(request: Request) -> HTTPException:
    """Refusal for a limited session: off to the login form, which says why
    (login.html keys the message off why=full - the text is never taken from
    the URL), rather than a bare 403. A person who clicked a nudge and then
    wants Admin just signs in, and the password session replaces this one.
    next= is offered only for a GET - a POST target is not a page to land on."""
    params = {"why": "full"}
    if request.method in ("GET", "HEAD"):
        params["next"] = request.url.path + (
            f"?{request.url.query}" if request.url.query else "")
    loc = "/login?" + urlencode(params)
    if request.headers.get("HX-Request"):
        # htmx does not follow a 303 into a page; it would swap the login form
        # into a table cell. HX-Redirect makes it navigate instead.
        return HTTPException(status_code=403, detail=FULL_LOGIN_MESSAGE,
                             headers={"HX-Redirect": loc})
    return HTTPException(status_code=303, detail=FULL_LOGIN_MESSAGE,
                         headers={"Location": loc})


require_viewer = RequireRole("viewer")
require_editor = RequireRole("editor")
require_admin = RequireRole("admin")


def require_self(request: Request, user: sqlite3.Row = Depends(require_viewer)):
    """The current user, refusing to act while an admin is viewing as them.

    For routes that mint or remove a CREDENTIAL, which must always belong to
    the account actually driving the browser. View-as deliberately resolves to
    the target everywhere else - that is the whole point of the feature - but a
    passkey registered under it would belong to the target, and would be the
    one thing an admin can leave behind that nothing takes away: it outlives
    the impersonation, survives the target's next password reset (which keeps
    passkeys on purpose), and keeps working after the admin is demoted. The
    banner promises anything saved is recorded as the admin; this is what makes
    that true for credentials rather than merely claimed.

    A check-in-link session is refused too (already, by require_viewer's
    allowlist; again here so widening that list can never reach a credential):
    a link that sat in a DM for a week must not become a passkey."""
    if is_limited(request):
        raise full_login_required(request)
    if getattr(request.state, "impersonator", None) is not None:
        raise HTTPException(status_code=403,
                            detail="Exit view-as before changing passkeys")
    return user


# ---------------------------------------------------------------- magic links
MAGIC_LINK_DAYS = 7
RESET_LINK_HOURS = 2


def _create_link(con: sqlite3.Connection, user_id: int, ttl: timedelta,
                 purpose: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + ttl).isoformat()
    con.execute(
        "INSERT INTO magic_links (token_hash, user_id, purpose, expires_at) "
        "VALUES (?,?,?,?)", (_sha256(token), user_id, purpose, expires))
    return token


def create_magic_link(con: sqlite3.Connection, user_id: int,
                      days: int = MAGIC_LINK_DAYS) -> str:
    """Pre-authenticated check-in link token (delivered over Slack DM).
    Multi-use until expiry: Slack's link crawler may GET the URL before the
    human does, so single-use tokens would be burned on arrival."""
    return _create_link(con, user_id, timedelta(days=days), "checkin")


def consume_magic_link(con: sqlite3.Connection, token: str,
                       purpose: str = "checkin") -> Optional[int]:
    """user_id for a valid unexpired link of this purpose belonging to an active
    user, else None. Purpose is matched, never assumed: a check-in link is DM'd
    every week and lives for seven days, so letting one be redeemed for a
    password change would make the weakest token in the system the strongest."""
    row = con.execute(
        """SELECT ml.user_id FROM magic_links ml
           JOIN users u ON u.id = ml.user_id
           WHERE ml.token_hash = ? AND ml.purpose = ? AND ml.expires_at > ?
             AND u.is_active = 1""",
        (_sha256(token), purpose,
         datetime.now(timezone.utc).isoformat())).fetchone()
    if row is None:
        return None
    con.execute("UPDATE magic_links SET last_used_at = datetime('now') "
                "WHERE token_hash = ?", (_sha256(token),))
    return row["user_id"]


# ------------------------------------------------------------ password resets
def create_reset_link(con: sqlite3.Connection, user_id: int) -> str:
    """Short-lived token authorising a password change, delivered over the
    user's private message channel.

    Multi-use within its window for the same reason check-in links are - Slack
    unfurls the URL before the human clicks it - so the window does the work
    instead: two hours, and any earlier outstanding reset for this user is
    dropped, so a second 'forgot password' invalidates the first mail."""
    con.execute("DELETE FROM magic_links WHERE user_id = ? AND purpose = 'reset'",
                (user_id,))
    return _create_link(con, user_id, timedelta(hours=RESET_LINK_HOURS), "reset")


def complete_password_reset(con: sqlite3.Connection, user_id: int,
                            new_password: str) -> None:
    """Set the password and end every other way in.

    A reset is what you do when you think someone else may have your password,
    so it revokes the outstanding reset tokens AND every existing session for
    the user - otherwise an attacker who is already signed in simply stays
    signed in, and the reset achieves nothing. Passkeys are deliberately kept:
    they are bound to hardware the attacker does not have, and dropping them
    would lock the user out of the one credential a stolen password cannot
    reach. The caller issues a fresh session afterwards."""
    con.execute("UPDATE users SET password_hash = ?, must_change_password = 0 "
                "WHERE id = ?", (hash_password(new_password), user_id))
    con.execute("DELETE FROM magic_links WHERE user_id = ? AND purpose = 'reset'",
                (user_id,))
    con.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def api_token_from_request(request: Request, con: sqlite3.Connection,
                           need_write: bool = False,
                           need_admin: bool = False) -> sqlite3.Row:
    """Resolve a bearer token from the Authorization header."""
    auth = request.headers.get("Authorization", "")
    scheme, token = get_authorization_scheme_param(auth)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    return api_token_from_value(con, token, need_write, need_admin)


def api_token_from_value(con: sqlite3.Connection, token: str,
                         need_write: bool = False,
                         need_admin: bool = False) -> sqlite3.Row:
    """Resolve a raw token string. 'admin' implies write; structural changes
    (archiving a metric off the board) require it explicitly.

    Split out from the header path so a caller that carries the token
    somewhere other than a header - the MCP endpoint's URL form, for
    connector UIs that accept only a URL - still lands on one scope check."""
    row = con.execute(
        """SELECT * FROM api_tokens
           WHERE token_hash = ? AND revoked_at IS NULL
             AND (expires_at IS NULL OR expires_at > datetime('now'))""",
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
                  created_by: Optional[int],
                  rotated_from_id: Optional[int] = None) -> str:
    token = "sc_" + secrets.token_urlsafe(32)
    con.execute(
        """INSERT INTO api_tokens (name, token_hash, scope, created_by, rotated_from_id)
           VALUES (?,?,?,?,?)""",
        (name, _sha256(token), scope, created_by, rotated_from_id),
    )
    return token


ROTATE_GRACE_DAYS = 7
ROTATE_GRACE_MAX = 90


def rotate_api_token(con: sqlite3.Connection, tid: int, grace_days: int,
                     created_by: Optional[int]) -> str:
    """Mint a replacement for token `tid` keeping its name and scope, and put
    the old secret on a grace clock instead of killing it outright - the
    integration keeps working until you have pasted the new value in.

    Rotating an already-rotated token is a no-op error rather than a chain of
    overlapping secrets: revoke or wait out the grace window first."""
    old = con.execute("SELECT * FROM api_tokens WHERE id = ?", (tid,)).fetchone()
    if old is None:
        raise ValueError("No such token")
    if old["revoked_at"]:
        raise ValueError("Token is revoked - create a new one instead")
    if old["expires_at"]:
        raise ValueError("Token is already rotated and expiring")
    grace = max(0, min(int(grace_days), ROTATE_GRACE_MAX))
    raw = new_api_token(con, old["name"], old["scope"], created_by, rotated_from_id=tid)
    # grace=0 means cut over immediately: expires_at lands in the past, so the
    # old secret stops authenticating on the next request.
    con.execute(
        "UPDATE api_tokens SET expires_at = datetime('now', ?) WHERE id = ?",
        (f"{grace} days", tid),
    )
    return raw
