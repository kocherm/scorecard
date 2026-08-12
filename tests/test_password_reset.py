"""Self-serve password reset: delivery over a PRIVATE channel only, purpose-
scoped tokens, and a reset that actually ends the sessions it is meant to end."""
import pytest
from fastapi.testclient import TestClient

from app import alerts
from app import auth
from app import db as dbm
from app import readiness
from app.auth import create_magic_link, create_reset_link, hash_password

PW = "a-fine-password-123"
NEW = "a-brand-new-password-456"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app
    from app import main as mainmod

    mainmod._login_failures.clear()  # in-process throttle outlives a tmp_path DB

    with dbm.get_db() as con:
        dbm.init_db(con)
        dbm.set_setting(con, "public_base_url", "https://board.example.com")
        dbm.set_setting(con, "slack_bot_token", "xoxb-test")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  slack_member_id, notify_channel)
               VALUES (2,'ed@x.co',?,'Eddie','editor','U123','slack')""",
            (hash_password(PW),))
        # Same workspace, reachable only through a shared Google Chat space.
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  notify_channel)
               VALUES (3,'sh@x.co',?,'Shared','editor','gchat')""",
            (hash_password(PW),))
        dbm.set_setting(con, "gchat_webhook_url", "https://chat.example/hook")
    yield app


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have gone out, instead of calling Slack."""
    out = []
    monkeypatch.setattr(alerts, "send_direct",
                        lambda con, u, text: out.append((u["id"], text)) or True)
    return out


def link_token(text):
    return text.split("/reset?t=")[1].split("|")[0].split(">")[0].split(" ")[0]


def arrive(client, token):
    """Follow a reset link the way a browser does: the token is exchanged for a
    path-scoped cookie and the URL comes back clean."""
    return client.get("/reset", params={"t": token})


# ------------------------------------------------------------------ delivery
def test_forgot_dms_a_reset_link_to_a_private_channel(env, sent):
    r = TestClient(env).post("/forgot", data={"email": "ed@x.co"})
    assert r.status_code == 200
    assert len(sent) == 1 and sent[0][0] == 2
    assert "https://board.example.com/reset?t=" in sent[0][1]


def test_shared_channel_user_is_never_sent_a_reset_link(env, sent):
    """A Google Chat webhook posts to a space the whole team reads, so a reset
    link there is an account takeover for anyone who clicks first."""
    r = TestClient(env).post("/forgot", data={"email": "sh@x.co"})
    assert r.status_code == 200
    assert sent == []


def test_forgot_says_the_same_thing_whoever_you_ask_about(env, sent):
    c = TestClient(env)
    bodies = [c.post("/forgot", data={"email": e}).text
              for e in ("ed@x.co", "sh@x.co", "nobody@x.co")]
    assert bodies[0] == bodies[1] == bodies[2]


def test_no_public_base_url_means_no_token_is_minted(env, sent):
    with dbm.get_db() as con:
        dbm.set_setting(con, "public_base_url", "")
    TestClient(env).post("/forgot", data={"email": "ed@x.co"})
    assert sent == []
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM magic_links "
                           "WHERE purpose='reset'").fetchone()["c"] == 0


def test_forgot_does_not_deliver_inside_the_request(env, monkeypatch):
    """Delivery is an HTTP call to Slack/Telegram/Twilio. Done inline, a known
    address answered hundreds of milliseconds slower than an unknown one, so
    the identical wording said nothing and the latency said everything. Asserted
    structurally rather than by wall-clock, which would be flaky."""
    from app import main as mainmod

    scheduled = []
    monkeypatch.setattr(mainmod, "_deliver_reset", scheduled.append)
    monkeypatch.setattr(alerts, "send_direct",
                        lambda *a: pytest.fail("sent inside the request"))
    c = TestClient(env)
    c.post("/forgot", data={"email": "ed@x.co"})
    assert scheduled == [2]
    scheduled.clear()
    c.post("/forgot", data={"email": "nobody@x.co"})
    assert scheduled == []


def test_forgot_is_throttled(env, sent):
    c = TestClient(env)
    for _ in range(5):
        c.post("/forgot", data={"email": "ed@x.co"})
    r = c.post("/forgot", data={"email": "ed@x.co"})
    assert "Too many requests" in r.text
    assert len(sent) == 5


# ------------------------------------------------------------------- purpose
def test_a_checkin_link_cannot_set_a_password(env):
    """Check-in links are DM'd weekly and live for seven days. If one could be
    redeemed here, the weakest token in the system would be the strongest."""
    with dbm.get_db() as con:
        token = create_magic_link(con, 2)
    c = TestClient(env)
    assert "expired" in arrive(c, token).text
    assert "expired" in c.post("/reset", data={"new": NEW}).text
    with dbm.get_db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=2").fetchone()
        assert auth.verify_password(PW, row["password_hash"])


def test_a_reset_link_cannot_sign_you_into_checkin(env):
    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    r = TestClient(env).get("/checkin", params={"t": token})
    assert str(r.url).endswith("/login")


def test_a_second_request_invalidates_the_first_link(env):
    with dbm.get_db() as con:
        first = create_reset_link(con, 2)
        second = create_reset_link(con, 2)
    assert "expired" in arrive(TestClient(env), first).text
    assert "expired" not in arrive(TestClient(env), second).text


# -------------------------------------------------- the token leaves the URL
def test_arriving_exchanges_the_token_for_a_cookie(env):
    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    c = TestClient(env)
    r = arrive(c, token)
    assert "t=" not in str(r.url) and str(r.url).endswith("/reset")
    assert token not in r.text          # not echoed into a hidden field either
    assert c.cookies.get("scorecard_reset") == token


def test_the_form_is_useless_without_the_cookie(env):
    with dbm.get_db() as con:
        create_reset_link(con, 2)
    r = TestClient(env).post("/reset", data={"new": NEW})   # no cookie
    assert "expired" in r.text
    with dbm.get_db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=2").fetchone()
        assert auth.verify_password(PW, row["password_hash"])


def test_the_cookie_is_cleared_once_it_is_spent(env):
    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    c = TestClient(env)
    arrive(c, token)
    c.post("/reset", data={"new": NEW})
    assert not c.cookies.get("scorecard_reset")


# -------------------------------------------------------------- the reset
def test_reset_sets_the_password_and_signs_in(env):
    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    c = TestClient(env)
    arrive(c, token)
    r = c.post("/reset", data={"new": NEW})
    assert r.status_code == 200 and not str(r.url).endswith("/login")
    with dbm.get_db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=2").fetchone()
        assert auth.verify_password(NEW, row["password_hash"])


def test_reset_ends_every_other_session(env):
    """The reason you reset is that someone else may have your password. If
    their existing session survives, the reset changed nothing for them."""
    attacker = TestClient(env)
    attacker.post("/login", data={"email": "ed@x.co", "password": PW})
    assert attacker.get("/account").status_code == 200

    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    victim = TestClient(env)
    arrive(victim, token)
    victim.post("/reset", data={"new": NEW})

    assert str(attacker.get("/account").url).endswith("/login")


def test_reset_token_dies_with_use(env):
    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    c = TestClient(env)
    arrive(c, token)
    c.post("/reset", data={"new": NEW})
    # Even replaying the original link cannot reopen it.
    assert "expired" in arrive(TestClient(env), token).text


def test_short_password_is_refused_and_the_link_survives(env):
    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    c = TestClient(env)
    arrive(c, token)
    assert "10+ characters" in c.post("/reset", data={"new": "short"}).text
    c.post("/reset", data={"new": NEW})
    with dbm.get_db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=2").fetchone()
        assert auth.verify_password(NEW, row["password_hash"])


# --------------------------------------------------------------- admin side
def admin(env):
    with dbm.get_db() as con:
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (9,'a@x.co',?,'Ada','admin')""", (hash_password(PW),))
    c = TestClient(env)
    c.post("/login", data={"email": "a@x.co", "password": PW})
    return c


def test_admin_reset_link_leaves_the_current_password_working(env, sent):
    """Unlike the temp-password button, sending a link changes nothing yet: a
    message that never arrives must not become a lockout."""
    c = admin(env)
    r = c.post("/admin/users/2/reset-link")
    assert "Reset link sent to Eddie" in r.text
    assert len(sent) == 1
    with dbm.get_db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=2").fetchone()
        assert auth.verify_password(PW, row["password_hash"])


def test_admin_reset_link_refused_for_a_shared_channel_user(env, sent):
    r = admin(env).post("/admin/users/3/reset-link")
    assert "no private message channel" in r.text
    assert sent == []


# ------------------------------------------------------ throttle and reset
def burn_the_attempt_limit(client):
    """What forgetting your password looks like from the server."""
    for _ in range(6):
        client.post("/login", data={"email": "ed@x.co", "password": "wrong-guess"})
    r = client.post("/login", data={"email": "ed@x.co", "password": PW})
    assert "Too many attempts" in r.text     # locked out even when correct


def test_reset_clears_the_login_throttle(env):
    """The bug this exists to prevent: the reset appears to do nothing.

    _throttled runs before the password is ever verified, so a counter left
    over from the forgotten-password attempts rejects the NEW password too -
    while passkey sign-in keeps working, because that path has no throttle."""
    c = TestClient(env)
    burn_the_attempt_limit(c)

    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    arrive(c, token)
    c.post("/reset", data={"new": NEW})

    fresh = TestClient(env)
    r = fresh.post("/login", data={"email": "ed@x.co", "password": NEW})
    assert "Too many attempts" not in r.text and "Wrong email" not in r.text
    assert fresh.get("/account").status_code == 200


def test_the_admin_temp_password_is_not_refused_by_the_throttle(env):
    """Same trap on the other reset path: the admin hands over a temp password
    that the throttle then rejects."""
    c = TestClient(env)
    burn_the_attempt_limit(c)
    r = admin(env).post("/admin/users/2/reset")
    temp = r.text.split('class="mono">')[1].split("</span>")[0]

    fresh = TestClient(env)
    assert "Too many attempts" not in fresh.post(
        "/login", data={"email": "ed@x.co", "password": temp}).text


def test_clearing_is_scoped_to_the_one_identity(env):
    """A reset must not hand everyone else a fresh set of guesses."""
    c = TestClient(env)
    for _ in range(6):
        c.post("/login", data={"email": "sh@x.co", "password": "wrong-guess"})
    burn_the_attempt_limit(c)

    with dbm.get_db() as con:
        token = create_reset_link(con, 2)
    arrive(c, token)
    c.post("/reset", data={"new": NEW})

    assert "Too many attempts" in TestClient(env).post(
        "/login", data={"email": "sh@x.co", "password": PW}).text


# --------------------------------------------------------------- readiness
def check(con):
    return next(c for c in readiness.local_checks(con, None)
                if c.key == "reset_delivery")


def test_readiness_counts_only_privately_reachable_users(env):
    with dbm.get_db() as con:
        c = check(con)
    assert c.state == readiness.WARN
    assert "1 of the 2 active users" in c.detail


def test_readiness_is_ok_when_everyone_is_reachable(env):
    with dbm.get_db() as con:
        con.execute("DELETE FROM users WHERE id = 3")  # the Google Chat user
        assert check(con).state == readiness.OK


def test_readiness_never_blocks_because_the_admin_path_always_works(env):
    """Blocking would claim setup is broken. It is not - it just means an admin
    has to hand out temp passwords by hand."""
    with dbm.get_db() as con:
        dbm.set_setting(con, "public_base_url", "")
        con.execute("UPDATE users SET notify_channel = 'gchat'")
        assert check(con).state == readiness.WARN
