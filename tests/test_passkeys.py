"""Passkeys, driven end to end by a software authenticator.

The interesting failures here are cryptographic and cannot be reached with a
mocked verifier, so this file builds real ES256 credentials: a genuine
attestation object for registration, a genuine signature over
authenticatorData || SHA256(clientDataJSON) for sign-in. Everything the app
rejects below, it rejects for the reason a real authenticator would trip over.
"""
import hashlib
import json
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature, encode_dss_signature)
from fastapi.testclient import TestClient

from app import db as dbm
from app.auth import hash_password

PW = "a-fine-password-123"
ORIGIN = "http://testserver"
RP_ID = "testserver"

UP, UV, AT = 0x01, 0x04, 0x40


def b64u(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64u(s: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Authenticator:
    """A single-credential platform authenticator, in about forty lines."""

    def __init__(self, cred_id=b"cred-0000000001", sign_count=0):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = cred_id
        self.sign_count = sign_count

    def _cose_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: nums.x.to_bytes(32, "big"),
                            -3: nums.y.to_bytes(32, "big")})

    def _auth_data(self, flags: int, attested: bool) -> bytes:
        data = (hashlib.sha256(RP_ID.encode()).digest()
                + bytes([flags]) + struct.pack(">I", self.sign_count))
        if attested:
            data += (b"\x00" * 16 + struct.pack(">H", len(self.cred_id))
                     + self.cred_id + self._cose_key())
        return data

    @staticmethod
    def _client_data(kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge,
                           "origin": ORIGIN, "crossOrigin": False}).encode()

    def create(self, challenge: str) -> dict:
        client_data = self._client_data("webauthn.create", challenge)
        att = cbor2.dumps({"fmt": "none", "attStmt": {},
                           "authData": self._auth_data(UP | UV | AT, True)})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(client_data),
                             "attestationObject": b64u(att),
                             "transports": ["internal"]}}

    def get(self, challenge: str, handle: str, *, bump: int = 1) -> dict:
        self.sign_count += bump
        client_data = self._client_data("webauthn.get", challenge)
        auth_data = self._auth_data(UP | UV, False)
        sig = self.key.sign(auth_data + hashlib.sha256(client_data).digest(),
                            ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(client_data),
                             "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig),
                             "userHandle": handle}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (2,'ed@x.co',?,'Eddie','editor')""", (hash_password(PW),))
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (3,'other@x.co',?,'Other','editor')""", (hash_password(PW),))
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (9,'ada@x.co',?,'Ada','admin')""", (hash_password(PW),))
    yield app


def signed_in(env, email="ed@x.co"):
    c = TestClient(env)
    c.post("/login", data={"email": email, "password": PW})
    return c


def register(client, auth: Authenticator, name="Test key"):
    opts = client.post("/account/passkeys/begin").json()
    cred = auth.create(opts["challenge"])
    return client.post("/account/passkeys/finish",
                       json={"credential": cred, "name": name})


def handle_of(uid: int) -> str:
    with dbm.get_db() as con:
        return con.execute("SELECT webauthn_handle FROM users WHERE id=?",
                           (uid,)).fetchone()["webauthn_handle"]


# ---------------------------------------------------------------- happy path
def test_register_then_sign_in_with_the_passkey(env):
    auth = Authenticator()
    assert register(signed_in(env), auth).json() == {"ok": True, "name": "Test key"}

    fresh = TestClient(env)  # no session cookie
    opts = fresh.post("/login/passkey/begin").json()
    r = fresh.post("/login/passkey/finish",
                   json=auth.get(opts["challenge"], handle_of(2)))
    assert r.json() == {"ok": True, "next": "/"}
    assert fresh.get("/account").status_code == 200


def test_registration_options_are_usernameless_and_bound_to_this_host(env):
    opts = signed_in(env).post("/account/passkeys/begin").json()
    assert opts["rp"]["id"] == RP_ID
    assert opts["authenticatorSelection"]["residentKey"] == "required"
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    # The handle sent to the authenticator must not be the row id.
    assert unb64u(opts["user"]["id"]).decode() == handle_of(2)
    assert unb64u(opts["user"]["id"]).decode() != "2"


def test_sign_in_options_name_no_accounts(env):
    """allowCredentials stays empty, so an anonymous caller cannot use the
    endpoint to ask whether a given account exists."""
    register(signed_in(env), Authenticator())
    opts = TestClient(env).post("/login/passkey/begin").json()
    assert not opts.get("allowCredentials")


def test_the_button_is_hidden_until_a_passkey_exists(env):
    assert "passkey-signin" not in TestClient(env).get("/login").text
    register(signed_in(env), Authenticator())
    assert "passkey-signin" in TestClient(env).get("/login").text


# ------------------------------------------------------------------ rejection
def test_a_challenge_cannot_be_replayed(env):
    auth = Authenticator()
    register(signed_in(env), auth)
    fresh = TestClient(env)
    opts = fresh.post("/login/passkey/begin").json()
    assertion = auth.get(opts["challenge"], handle_of(2))
    assert fresh.post("/login/passkey/finish", json=assertion).status_code == 200

    replay = TestClient(env)
    assert replay.post("/login/passkey/finish", json=assertion).status_code == 400
    assert str(replay.get("/account").url).endswith("/login")


def test_a_forged_signature_is_refused(env):
    auth = Authenticator()
    register(signed_in(env), auth)
    fresh = TestClient(env)
    opts = fresh.post("/login/passkey/begin").json()
    assertion = auth.get(opts["challenge"], handle_of(2))
    # Same credential, a signature from a different key.
    other = ec.generate_private_key(ec.SECP256R1())
    assertion["response"]["signature"] = b64u(
        other.sign(b"not the real message", ec.ECDSA(hashes.SHA256())))
    assert fresh.post("/login/passkey/finish", json=assertion).status_code == 400


def test_an_unknown_credential_is_refused(env):
    register(signed_in(env), Authenticator())
    stranger = Authenticator(cred_id=b"never-registered")
    fresh = TestClient(env)
    opts = fresh.post("/login/passkey/begin").json()
    r = fresh.post("/login/passkey/finish",
                   json=stranger.get(opts["challenge"], handle_of(2)))
    assert r.status_code == 400 and "not registered" in r.json()["error"]


def test_a_credential_cannot_be_claimed_for_another_account(env):
    """The authenticator says which account it signed for. A credential offered
    against someone else's handle is not honoured."""
    auth = Authenticator()
    register(signed_in(env), auth)
    register(signed_in(env, "other@x.co"), Authenticator(cred_id=b"cred-two"))
    fresh = TestClient(env)
    opts = fresh.post("/login/passkey/begin").json()
    r = fresh.post("/login/passkey/finish",
                   json=auth.get(opts["challenge"], handle_of(3)))
    assert r.status_code == 400


def test_a_stalled_sign_counter_is_treated_as_a_clone(env):
    auth = Authenticator(sign_count=5)
    register(signed_in(env), auth)
    fresh = TestClient(env)
    opts = fresh.post("/login/passkey/begin").json()
    # bump=0: the counter does not advance, the documented cloned-key signal.
    r = fresh.post("/login/passkey/finish",
                   json=auth.get(opts["challenge"], handle_of(2), bump=0))
    assert r.status_code == 400


def test_a_synced_passkey_reporting_zero_is_still_accepted(env):
    """iCloud Keychain and Google Password Manager always report 0. Treating
    that as a clone would break every synced passkey there is."""
    auth = Authenticator(sign_count=0)
    register(signed_in(env), auth)
    fresh = TestClient(env)
    opts = fresh.post("/login/passkey/begin").json()
    r = fresh.post("/login/passkey/finish",
                   json=auth.get(opts["challenge"], handle_of(2), bump=0))
    assert r.json()["ok"] is True


def test_registration_requires_a_session(env):
    r = TestClient(env).post("/account/passkeys/begin")
    assert str(r.url).endswith("/login")
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM webauthn_challenges"
                           ).fetchone()["c"] == 0


def test_a_deactivated_user_cannot_sign_in_with_a_passkey(env):
    auth = Authenticator()
    register(signed_in(env), auth)
    with dbm.get_db() as con:
        con.execute("UPDATE users SET is_active = 0 WHERE id = 2")
    fresh = TestClient(env)
    opts = fresh.post("/login/passkey/begin").json()
    r = fresh.post("/login/passkey/finish",
                   json=auth.get(opts["challenge"], handle_of(2)))
    assert r.status_code == 400


# ----------------------------------------------------------------- view-as
def viewing_as(env, uid):
    """An admin with 'View as' active on `uid`."""
    c = signed_in(env, "ada@x.co")
    c.post(f"/admin/users/{uid}/impersonate")
    return c


def test_an_admin_viewing_as_someone_cannot_add_them_a_passkey(env):
    """The one thing an admin could otherwise leave behind that nothing takes
    away: it outlives the impersonation, survives the target's next password
    reset (which keeps passkeys on purpose), and works after a demotion."""
    c = viewing_as(env, 2)
    assert c.post("/account/passkeys/begin").status_code == 403
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM webauthn_challenges"
                           ).fetchone()["c"] == 0
        assert con.execute("SELECT COUNT(*) c FROM webauthn_credentials"
                           ).fetchone()["c"] == 0


def test_the_finish_endpoint_is_guarded_too_not_just_the_begin(env):
    """Registration is two calls. Guarding only the first would leave a
    challenge minted before view-as started still redeemable during it."""
    admin = signed_in(env, "ada@x.co")
    opts = admin.post("/account/passkeys/begin").json()   # as themselves
    cred = Authenticator().create(opts["challenge"])
    admin.post("/admin/users/2/impersonate")
    r = admin.post("/account/passkeys/finish",
                   json={"credential": cred, "name": "Planted"})
    assert r.status_code == 403
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM webauthn_credentials"
                           ).fetchone()["c"] == 0


def test_an_admin_viewing_as_someone_cannot_remove_their_passkey(env):
    register(signed_in(env), Authenticator())
    with dbm.get_db() as con:
        pk = con.execute("SELECT id FROM webauthn_credentials").fetchone()["id"]
    assert viewing_as(env, 2).post(f"/account/passkeys/{pk}/delete").status_code == 403
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM webauthn_credentials"
                           ).fetchone()["c"] == 1


def test_the_admin_can_still_manage_their_own_after_exiting(env):
    c = viewing_as(env, 2)
    c.post("/impersonate/stop")
    assert register(c, Authenticator()).json()["ok"] is True
    with dbm.get_db() as con:
        row = con.execute("SELECT user_id FROM webauthn_credentials").fetchone()
        assert row["user_id"] == 9      # the admin's own account, not the target


def test_the_account_page_hides_the_controls_while_viewing_as(env):
    register(signed_in(env), Authenticator(), name="Eddie laptop")
    page = viewing_as(env, 2).get("/account").text
    assert "Eddie laptop" in page         # view-as still shows what they see
    assert "passkey-add" not in page      # but not the add control
    assert "/delete" not in page          # nor the remove buttons


# -------------------------------------------------------------- housekeeping
def test_removing_a_passkey_stops_it_working(env):
    auth = Authenticator()
    c = signed_in(env)
    register(c, auth)
    with dbm.get_db() as con:
        pk = con.execute("SELECT id FROM webauthn_credentials").fetchone()["id"]
    c.post(f"/account/passkeys/{pk}/delete")

    fresh = TestClient(env)
    opts = fresh.post("/login/passkey/begin").json()
    assert fresh.post("/login/passkey/finish",
                      json=auth.get(opts["challenge"], handle_of(2))).status_code == 400


def test_you_cannot_delete_someone_elses_passkey(env):
    register(signed_in(env), Authenticator())
    with dbm.get_db() as con:
        pk = con.execute("SELECT id FROM webauthn_credentials").fetchone()["id"]
    signed_in(env, "other@x.co").post(f"/account/passkeys/{pk}/delete")
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM webauthn_credentials"
                           ).fetchone()["c"] == 1


def test_a_password_reset_keeps_passkeys(env):
    """A stolen password cannot reach the authenticator, so wiping passkeys on
    reset would only remove the credential the attacker never had."""
    from app.auth import complete_password_reset
    register(signed_in(env), Authenticator())
    with dbm.get_db() as con:
        complete_password_reset(con, 2, "a-brand-new-password-456")
        assert con.execute("SELECT COUNT(*) c FROM webauthn_credentials"
                           ).fetchone()["c"] == 1


def test_the_same_authenticator_is_excluded_from_re_registering(env):
    c = signed_in(env)
    auth = Authenticator()
    register(c, auth)
    opts = c.post("/account/passkeys/begin").json()
    assert [e["id"] for e in opts["excludeCredentials"]] == [b64u(auth.cred_id)]
