"""Passkeys (WebAuthn): register from Account, sign in from the login page.

Additive to passwords on purpose. A passkey lives in one authenticator, and
the recovery story for a lost authenticator is the password - so every user
keeps one, and a passkey is a faster front door, never the only door. That
also keeps the TV kiosk and the Slack magic links working unchanged.

Sign-in is *usernameless* (discoverable credentials): the browser shows the
accounts it holds for this site and the credential names the user, so there is
no email box to type into and no "does this account exist" oracle to probe.

Two things decide whether a ceremony verifies, and both come from the request
rather than from settings:

- **RP ID** - the registrable domain the credential is bound to.
- **Origin** - the exact scheme://host:port the browser reports.

They are derived from the live request because that is by definition what the
browser will send, so dev on localhost and prod behind Caddy both work with no
configuration. `public_base_url` is deliberately NOT used here: it is a
hand-typed setting, and a stale value would silently invalidate every existing
passkey. Uvicorn runs with --proxy-headers (see Dockerfile), so the scheme is
the browser's https, not the proxy hop's http; without that flag every
assertion would fail the origin check.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Request
from webauthn import (base64url_to_bytes, generate_authentication_options,
                      generate_registration_options, options_to_json,
                      verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import (InvalidAuthenticationResponse,
                                         InvalidRegistrationResponse)
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

log = logging.getLogger("scorecard.passkeys")

RP_NAME = "Company Scorecard"
CHALLENGE_MINUTES = 5
MAX_PER_USER = 10


class PasskeyError(Exception):
    """Ceremony failed. The message is safe to show the user."""


# ------------------------------------------------------------ relying party
def rp_from_request(request: Request) -> tuple[str, str]:
    """(rp_id, origin) for this request. rp_id is the bare hostname - no port,
    since WebAuthn RP IDs are domains and ':8096' would be rejected outright."""
    origin = str(request.base_url).rstrip("/")
    host = urlsplit(origin).hostname or "localhost"
    return host, origin


# ---------------------------------------------------------------- challenges
def _remember_challenge(con: sqlite3.Connection, challenge: bytes,
                        user_id: Optional[int], purpose: str) -> None:
    con.execute("DELETE FROM webauthn_challenges WHERE expires_at <= ?",
                (datetime.now(timezone.utc).isoformat(),))
    expires = (datetime.now(timezone.utc)
               + timedelta(minutes=CHALLENGE_MINUTES)).isoformat()
    con.execute(
        "INSERT INTO webauthn_challenges (challenge, user_id, purpose, expires_at) "
        "VALUES (?,?,?,?)",
        (bytes_to_base64url(challenge), user_id, purpose, expires))


def _take_challenge(con: sqlite3.Connection, challenge_b64: str, purpose: str,
                    user_id: Optional[int] = None) -> bytes:
    """Redeem a challenge, deleting it. Single-use by deletion: a challenge
    that could be replayed is not a challenge."""
    row = con.execute(
        "SELECT * FROM webauthn_challenges WHERE challenge = ? AND purpose = ? "
        "AND expires_at > ?",
        (challenge_b64, purpose, datetime.now(timezone.utc).isoformat())).fetchone()
    if row is None:
        raise PasskeyError("That took too long - start again.")
    con.execute("DELETE FROM webauthn_challenges WHERE challenge = ?", (challenge_b64,))
    if user_id is not None and row["user_id"] != user_id:
        raise PasskeyError("That request was not for this account.")
    return base64url_to_bytes(challenge_b64)


def _client_challenge(credential: dict) -> str:
    """The challenge the browser actually signed, out of clientDataJSON. Read
    only to find our stored row - verify_*_response re-checks it against
    expected_challenge, so a lie here fails there."""
    try:
        raw = base64url_to_bytes(credential["response"]["clientDataJSON"])
        return json.loads(raw)["challenge"]
    except (KeyError, TypeError, ValueError) as e:
        raise PasskeyError("Malformed passkey response.") from e


# ------------------------------------------------------------- registration
def credentials_for(con: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM webauthn_credentials WHERE user_id = ? ORDER BY created_at",
        (user_id,)).fetchall()


def _handle(con: sqlite3.Connection, user_id: int) -> str:
    """This user's opaque WebAuthn handle, minted on first registration."""
    row = con.execute("SELECT webauthn_handle FROM users WHERE id = ?",
                      (user_id,)).fetchone()
    if row and row["webauthn_handle"]:
        return row["webauthn_handle"]
    handle = secrets.token_urlsafe(24)
    con.execute("UPDATE users SET webauthn_handle = ? WHERE id = ?", (handle, user_id))
    return handle


def begin_registration(con: sqlite3.Connection, request: Request,
                       user: sqlite3.Row) -> str:
    """Options JSON for navigator.credentials.create()."""
    existing = credentials_for(con, user["id"])
    if len(existing) >= MAX_PER_USER:
        raise PasskeyError(f"You already have {MAX_PER_USER} passkeys. "
                           "Remove one before adding another.")
    rp_id, _ = rp_from_request(request)
    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=_handle(con, user["id"]).encode(),
        user_name=user["email"],
        user_display_name=user["display_name"],
        # Resident key so sign-in can be usernameless; user verification
        # required so a passkey is two factors on its own (device + biometric
        # or PIN) rather than mere possession of an unlocked laptop.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        # Stops the same authenticator silently becoming a second entry that
        # the user cannot tell apart from the first.
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
            for c in existing],
    )
    _remember_challenge(con, opts.challenge, user["id"], "register")
    return options_to_json(opts)


def finish_registration(con: sqlite3.Connection, request: Request,
                        user: sqlite3.Row, credential: dict, name: str) -> str:
    """Verify and store a new credential. Returns the name it was saved under."""
    rp_id, origin = rp_from_request(request)
    challenge = _take_challenge(con, _client_challenge(credential), "register",
                                user_id=user["id"])
    try:
        v = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except InvalidRegistrationResponse as e:
        log.warning("passkey registration rejected for user %s: %s", user["id"], e)
        raise PasskeyError("That passkey could not be verified.") from e

    label = (name or "").strip()[:60] or "Passkey"
    transports = credential.get("response", {}).get("transports") or []
    con.execute(
        """INSERT INTO webauthn_credentials
             (user_id, credential_id, public_key, sign_count, transports, name)
           VALUES (?,?,?,?,?,?)""",
        (user["id"], bytes_to_base64url(v.credential_id), v.credential_public_key,
         v.sign_count, json.dumps(transports), label))
    return label


def delete_credential(con: sqlite3.Connection, user_id: int, cred_pk: int) -> bool:
    cur = con.execute(
        "DELETE FROM webauthn_credentials WHERE id = ? AND user_id = ?",
        (cred_pk, user_id))
    return cur.rowcount > 0


# ------------------------------------------------------------------- sign-in
def any_registered(con: sqlite3.Connection) -> bool:
    """Whether to offer the passkey button at all. No credentials anywhere
    means the button can only ever fail, so the login page hides it."""
    return con.execute(
        "SELECT 1 FROM webauthn_credentials LIMIT 1").fetchone() is not None


def begin_login(con: sqlite3.Connection, request: Request) -> str:
    """Options JSON for navigator.credentials.get(). allow_credentials is left
    empty: the authenticator offers whatever it holds for this RP ID, so an
    anonymous caller learns nothing about which accounts exist."""
    rp_id, _ = rp_from_request(request)
    opts = generate_authentication_options(
        rp_id=rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    _remember_challenge(con, opts.challenge, None, "login")
    return options_to_json(opts)


def finish_login(con: sqlite3.Connection, request: Request,
                 credential: dict) -> int:
    """Verify an assertion and return the user_id it authenticates."""
    rp_id, origin = rp_from_request(request)
    challenge = _take_challenge(con, _client_challenge(credential), "login")

    cred_id = credential.get("id") or ""
    row = con.execute(
        """SELECT c.*, u.is_active, u.webauthn_handle FROM webauthn_credentials c
           JOIN users u ON u.id = c.user_id
           WHERE c.credential_id = ?""", (cred_id,)).fetchone()
    if row is None or not row["is_active"]:
        raise PasskeyError("That passkey is not registered here.")

    # The authenticator reports which account it signed for. Checking it stops
    # a credential from being honoured against a row it does not belong to.
    handle = credential.get("response", {}).get("userHandle")
    if handle and handle != row["webauthn_handle"]:
        raise PasskeyError("That passkey is not registered here.")

    try:
        v = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"],
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as e:
        log.warning("passkey assertion rejected for credential %s: %s", cred_id, e)
        raise PasskeyError("That passkey could not be verified.") from e

    # Clone detection, and only where it means anything: a counter that fails
    # to advance is the documented signal of a copied authenticator, but synced
    # platform passkeys (iCloud Keychain, Google Password Manager) always report
    # 0, so 0 is "this authenticator keeps no counter", not evidence.
    if row["sign_count"] > 0 and v.new_sign_count <= row["sign_count"]:
        log.warning("passkey sign counter did not advance for credential %s "
                    "(stored %s, got %s) - possible cloned authenticator",
                    cred_id, row["sign_count"], v.new_sign_count)
        raise PasskeyError("That passkey could not be verified.")

    con.execute("UPDATE webauthn_credentials SET sign_count = ?, "
                "last_used_at = datetime('now') WHERE id = ?",
                (v.new_sign_count, row["id"]))
    return row["user_id"]
