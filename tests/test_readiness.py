"""Setup readiness: the page has to be trustworthy, or it is worse than no page.

Each test here pins one of the failures that made this necessary - a token for
the wrong workspace, member IDs from that workspace that still resolved, a
switch that silenced everything, a sweep that returned before sending anything
and told only the log.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import alerts, auth
from app import db as dbm
from app import readiness

NOW = datetime.now(timezone.utc)


class FakeResp:
    def __init__(self, payload, scopes=""):
        self._payload, self.status_code = payload, 200
        # Slack reports granted scopes only in this header, never in the body.
        self.headers = {"x-oauth-scopes": scopes}

    def json(self):
        return self._payload


@pytest.fixture
def slack(monkeypatch):
    """Stand in for Slack at the network boundary, so a page load that touches
    it fails loudly instead of quietly reaching out."""
    calls, responses = [], {}
    granted = SimpleNamespace(scopes="chat:write,chat:write.customize,im:history")

    def post(url, headers=None, data=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, dict(data or {})))
        r = responses.get(method, {"ok": False, "error": "not_stubbed"})
        return FakeResp(r(dict(data or {})) if callable(r) else r, granted.scopes)

    monkeypatch.setattr(readiness.httpx, "post", post)
    return SimpleNamespace(calls=calls, responses=responses, granted=granted)


def _bare(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)


@pytest.fixture
def bare(tmp_path, monkeypatch):
    _bare(tmp_path, monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A configured instance: one admin, one editor who owns a metric, Slack
    settings filled in the way they are on a working install."""
    _bare(tmp_path, monkeypatch)
    with dbm.get_db() as con:
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute("""INSERT INTO users (id, email, password_hash, display_name,
                                          role, slack_member_id)
                       VALUES (1,'admin@example.com','x','Ada','admin','U100')""")
        con.execute("""INSERT INTO users (id, email, password_hash, display_name,
                                          role, slack_member_id)
                       VALUES (2,'dri@example.com','x','Bo','editor','U200')""")
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Calls', 'numeric', 'sum', '2026-01-05', 2)""")
        dbm.set_setting(con, "slack_bot_token", "xoxb-test")
        dbm.set_setting(con, "slack_channel_id", "C123")
        dbm.set_setting(con, "slack_signing_secret", "s3cr3t")
        dbm.set_setting(con, "alerts_enabled", "1")
        dbm.set_setting(con, "nudges_enabled", "1")
        dbm.set_setting(con, "public_base_url", "https://score.example.com")
        dbm.set_setting(con, "display_token", "tv-token")  # normally set at boot
        session = auth.create_session(con, 1)
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, session)
    return c


def states(checks):
    return {c.key: c.state for c in checks}


# --------------------------------------------------------------- local checks
def test_a_bare_database_reads_as_a_setup_list(bare):
    with dbm.get_db() as con:
        s = states(readiness.local_checks(con, NOW))
    # Nothing is configured, and every check says so rather than staying silent.
    assert s["people"] == "blocked"
    assert s["metrics"] == "blocked"
    assert s["dris"] == "blocked"
    assert s["slack_credentials"] == "blocked"
    assert s["slack_channel"] == "blocked"
    assert s["alerts_enabled"] == "blocked"
    assert s["channel_coverage"] == "blocked"
    assert s["display_token"] == "blocked"
    assert s["public_base_url"] == "warn"   # nudges are off, so nothing is lost
    assert s["signing_secret"] == "warn"
    assert s["api_tokens"] == "warn"
    assert "ok" not in s.values(), "nothing is configured; nothing should read ok"


def test_a_configured_instance_reads_green(env):
    with dbm.get_db() as con:
        s = states(readiness.local_checks(con, NOW))
    assert "blocked" not in s.values(), s
    for key in ("people", "metrics", "dris", "slack_credentials", "slack_channel",
                "alerts_enabled", "nudges_enabled", "public_base_url",
                "signing_secret", "channel_coverage"):
        assert s[key] == "ok", (key, s[key])


def test_no_public_base_url_blocks_nudges(env):
    """The exact shape of the original failure: a switch says ON, and the sweep
    returns before sending anything because a different field is empty."""
    with dbm.get_db() as con:
        dbm.set_setting(con, "public_base_url", "")
        s = states(readiness.local_checks(con, NOW))
    assert s["public_base_url"] == "blocked"
    assert s["nudges_enabled"] == "ok"  # the switch really is on; that's the trap


def test_nudges_on_with_the_master_switch_off_is_blocked(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "alerts_enabled", "0")
        s = states(readiness.local_checks(con, NOW))
    assert s["alerts_enabled"] == "blocked"
    assert s["nudges_enabled"] == "blocked"


def test_a_dri_with_no_channel_is_not_silently_skipped(env):
    with dbm.get_db() as con:
        con.execute("UPDATE users SET slack_member_id = NULL WHERE id = 2")
        checks = {c.key: c for c in readiness.local_checks(con, NOW)}
    coverage = checks["channel_coverage"]
    assert coverage.state == "blocked"
    assert "Bo" in [i["name"] for i in coverage.items]


def test_a_webhook_that_is_not_a_slack_webhook_is_blocked(env):
    """Found on the live instance: the field held the app's own URL, so every
    channel alert was POSTed to the scorecard instead of Slack. The webhook
    wins over bot+channel whenever it is set, so this field is quietly
    decisive - and Slack offers no way to ask where a webhook points."""
    with dbm.get_db() as con:
        dbm.set_setting(con, "slack_webhook_url", "https://score.example.com/x/y")
        c = {k.key: k for k in readiness.local_checks(con, NOW)}["slack_channel"]
    assert c.state == "blocked"
    assert "hooks.slack.com" in c.detail


def test_a_webhook_alongside_bot_and_channel_warns_that_it_wins(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "slack_webhook_url",
                        "https://hooks.slack.com/services/T/B/x")
        c = {k.key: k for k in readiness.local_checks(con, NOW)}["slack_channel"]
    assert c.state == "warn"
    assert "wins" in c.detail


# ------------------------------------------------------------- network checks
def test_page_load_never_calls_slack(env, slack):
    assert env.get("/admin/status").status_code == 200
    assert slack.calls == [], "an admin page must not wait on Slack"


def test_recheck_reports_which_workspace_the_token_is_for(env, slack):
    slack.responses["auth.test"] = {"ok": True, "team": "Northwind",
                                    "team_id": "T111", "user": "scorecard"}
    slack.responses["conversations.info"] = {
        "ok": True, "channel": {"name": "scorecard", "is_member": True}}
    slack.responses["users.info"] = lambda d: {
        "ok": True, "user": {"id": d["user"], "team_id": "T111", "real_name": "Bo"}}

    page = env.post("/admin/status/recheck", follow_redirects=True).text
    assert "Workspace Northwind, bot @scorecard." in page
    assert "#scorecard, bot is a member." in page
    assert "member IDs are accounts in this workspace" in page


def test_a_default_avatar_is_named_but_does_not_fail_the_token(env, slack):
    """An install that predates the icon still sends; it just looks generic.
    Saying so beats an admin wondering why the mark never showed up."""
    slack.granted.scopes = "chat:write,im:history"
    slack.responses["auth.test"] = {"ok": True, "team": "Northwind",
                                    "team_id": "T111", "user": "scorecard"}
    with dbm.get_db() as con:
        c = readiness.verify_token(con, NOW)
    assert c["state"] == "ok"
    assert "chat:write.customize" in c["detail"]


def test_a_token_for_the_wrong_workspace_is_blocked_with_the_reason(env, slack):
    slack.responses["auth.test"] = {"ok": False, "error": "invalid_auth"}
    with dbm.get_db() as con:
        readiness.verify_token(con, NOW)
        c = readiness.cached(con, "slack_token")
    assert c["state"] == "blocked"
    assert "invalid_auth" in c["detail"]


def test_a_member_id_from_another_workspace_warns_instead_of_passing(env, slack):
    """The one that cost a whole setup session. Slack Connect means an ID from
    a different workspace resolves cleanly, so "users.info said ok" proves
    nothing - the team_id is the only thing that separates a colleague from a
    stranger who happens to share a channel."""
    slack.responses["users.info"] = lambda d: {
        "ok": True, "user": {"id": d["user"], "team_id": "T-OTHER",
                             "real_name": "Someone Else"}}
    with dbm.get_db() as con:
        result = readiness.verify_members(con, NOW, team_id="T111")
    assert result["state"] == "warn"
    assert "different workspace" in result["detail"]
    assert all("another workspace" in i["detail"] for i in result["items"])


def test_the_same_member_id_in_our_workspace_passes(env, slack):
    slack.responses["users.info"] = lambda d: {
        "ok": True, "user": {"id": d["user"], "team_id": "T111", "real_name": "Bo"}}
    with dbm.get_db() as con:
        result = readiness.verify_members(con, NOW, team_id="T111")
    assert result["state"] == "ok"


def test_ids_that_resolve_with_no_workspace_to_compare_are_not_called_ok(env, slack):
    """A bad token means no team_id, and "users.info said ok" on its own is the
    same false green this whole check exists to kill."""
    slack.responses["auth.test"] = {"ok": False, "error": "invalid_auth"}
    slack.responses["users.info"] = lambda d: {
        "ok": True, "user": {"id": d["user"], "team_id": "T111", "real_name": "Bo"}}
    with dbm.get_db() as con:
        readiness.run_network_checks(con, NOW)
        result = readiness.cached(con, "slack_members")
    assert result["state"] == "warn"
    assert "not be confirmed" in result["detail"]


def test_a_bot_outside_the_channel_is_blocked_not_ok(env, slack):
    slack.responses["conversations.info"] = {
        "ok": True, "channel": {"name": "scorecard", "is_member": False}}
    with dbm.get_db() as con:
        result = readiness.verify_channel(con, NOW)
    assert result["state"] == "blocked"
    assert "/invite" in result["detail"]


def test_cached_results_state_their_own_age(env, slack):
    earlier = NOW - timedelta(hours=3)
    with dbm.get_db() as con:
        readiness._store(con, "slack_token", "ok", "Workspace Northwind.", earlier)
    page = env.get("/admin/status").text
    assert "Checked 3 hours ago." in page
    assert slack.calls == []


def test_an_unchecked_instance_says_so_rather_than_looking_fine(env):
    with dbm.get_db() as con:
        net = states(readiness.network_checks(con, NOW))
    assert set(net.values()) == {"unchecked"}


# ------------------------------------------------------------------ nav badge
def test_the_nav_badge_counts_exactly_the_blocked_rows(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "alerts_enabled", "0")
        dbm.set_setting(con, "public_base_url", "")
        checks = readiness.local_checks(con, NOW) + readiness.network_checks(con, NOW)
        blocked = [c for c in checks if c.state == "blocked"]
        assert readiness.blocked_count(con, NOW) == len(blocked)
    assert blocked, "this fixture is supposed to be broken"
    page = env.get("/admin/status").text
    assert f'<span class="nav-badge alert">{len(blocked)}</span>' in page


def test_a_healthy_instance_shows_no_badge(env):
    page = env.get("/admin/status").text
    assert 'class="nav-badge alert"' not in page


# ----------------------------------------------------------------- sweep runs
def test_a_sweep_that_sends_nothing_still_says_why(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "public_base_url", "")
    assert alerts.nudge_sweep("nudge1", NOW) == 0
    page = env.get("/admin/status").text
    assert "No public base URL set" in page
    assert "skipped" in page


def test_every_kill_switch_leaves_a_record(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "alerts_enabled", "0")
    alerts.nudge_sweep("nudge1", NOW)
    alerts.stale_sweep(NOW)
    alerts.red_sweep(NOW)
    with dbm.get_db() as con:
        rows = {r["kind"]: r["detail"] for r in con.execute(
            "SELECT kind, detail FROM sweep_runs WHERE outcome = 'skipped'")}
    assert set(rows) == {"nudge1", "stale", "red"}
    assert all("master switch" in d for d in rows.values())


def test_a_sweep_that_ran_records_what_it_sent(env, monkeypatch):
    monkeypatch.setattr(alerts, "post_dm", lambda *a, **kw: True)
    assert alerts.nudge_sweep("nudge1", NOW) == 1
    with dbm.get_db() as con:
        r = con.execute("SELECT * FROM sweep_runs WHERE kind='nudge1'").fetchone()
    assert (r["outcome"], r["sent_count"]) == ("sent", 1)
    assert r["detail"] == "Messaged 1 person."


def test_sweeps_with_no_run_yet_are_not_mistaken_for_healthy(bare):
    with dbm.get_db() as con:
        runs = readiness.sweep_runs(con, NOW)
    assert {r["kind"] for r in runs} == {"nudge1", "nudge2", "stale", "red"}
    assert all(r["state"] == "unchecked" for r in runs)


def test_old_runs_are_pruned(env):
    with dbm.get_db() as con:
        con.execute("""INSERT INTO sweep_runs (kind, ran_at, outcome, detail)
                       VALUES ('stale', datetime('now','-200 days'), 'nothing','old')""")
        con.execute("""INSERT INTO sweep_runs (kind, outcome, detail)
                       VALUES ('stale', 'nothing', 'recent')""")
        readiness.prune_sweep_runs(con)
        kept = [r["detail"] for r in con.execute("SELECT detail FROM sweep_runs")]
    assert kept == ["recent"]


# ------------------------------------------------------------ verify on save
def test_saving_a_bot_token_says_which_workspace_it_reached(env, slack):
    slack.responses["auth.test"] = {"ok": True, "team": "Northwind",
                                    "team_id": "T111", "user": "scorecard"}
    env.post("/admin/settings/slack",
             data={"slack_bot_token": "xoxb-new", "slack_channel_id": "C123",
                   "slack_signing_secret": "s3cr3t"})
    page = env.get("/admin/settings?saved=slack").text
    assert "Workspace Northwind, bot @scorecard." in page


def test_a_rejected_token_does_not_look_like_a_successful_save(env, slack):
    slack.responses["auth.test"] = {"ok": False, "error": "invalid_auth"}
    env.post("/admin/settings/slack", data={"slack_bot_token": "xoxb-wrong"})
    page = env.get("/admin/settings?saved=slack").text
    assert "Saved, but Slack did not accept it." in page
    assert "invalid_auth" in page
    # A failure must not fade off the screen the way a success does.
    assert "flash err" in page


# ----------------------------------------------------- match Slack IDs by email
def test_matching_proposes_and_never_writes_on_its_own(env, slack):
    slack.responses["users.list"] = {
        "ok": True,
        "members": [{"id": "U777", "real_name": "Bo",
                     "profile": {"email": "dri@example.com"}},
                    {"id": "UBOT", "is_bot": True,
                     "profile": {"email": "bot@example.com"}}]}
    page = env.post("/admin/status/match-ids").text
    assert "U777" in page
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "verify_slack_members") is None
        row = con.execute("SELECT slack_member_id FROM users WHERE id=2").fetchone()
    assert row["slack_member_id"] == "U200", "the proposal must not have applied"

    env.post("/admin/status/match-ids/apply", data={"pair": "2:U777"})
    with dbm.get_db() as con:
        row = con.execute("SELECT slack_member_id FROM users WHERE id=2").fetchone()
    assert row["slack_member_id"] == "U777"


def test_apply_rejects_anything_that_is_not_a_slack_id(env):
    with dbm.get_db() as con:
        n = readiness.apply_member_ids(con, ["2:'; DROP TABLE users; --", "x:U1",
                                             "2:not-an-id"])
        row = con.execute("SELECT slack_member_id FROM users WHERE id=2").fetchone()
    assert n == 0
    assert row["slack_member_id"] == "U200"


def test_a_missing_scope_is_reported_with_the_scope_to_add(env, slack):
    slack.responses["users.list"] = {"ok": False, "error": "missing_scope"}
    page = env.post("/admin/status/match-ids").text
    assert "users:read.email" in page
