"""The alert sweeps' delivery guarantees, one section per fix:

1. only a closed week that is itself red escalates (no re-post on a pending week)
2. one red DM per PERSON, over their own private channel, with 1-3-1 links
3. a failed send releases its claim and is not counted
4. no write transaction is open while a message is on the wire
5. hidden sections are off the Wednesday chase
6. shared-space channels never get a magic link
7. scheduler: misfire grace, boot catch-up, crashes recorded as 'error'

Every clock is fixed - nothing here depends on the real calendar.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import alerts
from app import db as dbm
from app import grid as gridm
from app import weeks as wk
from app.auth import hash_password

# Tuesday 25 Aug 2026, 08:10 Chicago (CDT, UTC-5): after the red sweep's slot.
TUE = datetime(2026, 8, 25, 13, 10, tzinfo=timezone.utc)
# Wednesday 26 Aug 2026, 09:00 Chicago: the stale grace period has passed.
WED = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
DUE = wk.last_closed_week(TUE)
assert DUE == wk.last_closed_week(WED) == datetime(2026, 8, 17).date()
BASE = "https://score.example.com"


class Net:
    """Every outbound HTTP call, answered as Slack/Telegram/webhooks would.
    `fail` makes every call fail; `during` runs inside each call, which is
    how the tests look at the database while a message is on the wire."""

    def __init__(self):
        self.calls, self.fail, self.during = [], False, None
        self._ts = iter(f"1700000000.{i:06d}" for i in range(1, 999))

    def post(self, url, headers=None, json=None, data=None, auth=None,
             timeout=None):
        self.calls.append(SimpleNamespace(url=url, json=json or {}, data=data))
        if self.during:
            self.during(url, json or {})
        if self.fail:
            return SimpleNamespace(status_code=500, text="boom",
                                   json=lambda: {"ok": False, "error": "boom"})
        return SimpleNamespace(status_code=200, text="ok",
                               json=lambda: {"ok": True, "ts": next(self._ts)})

    def slack(self, channel=None):
        return [c.json for c in self.calls if "slack.com/api" in c.url
                and (channel is None or c.json.get("channel") == channel)]

    def dms(self):
        return [j for j in self.slack() if j["channel"].startswith("U")]

    def channel_posts(self):
        return self.slack("C1")


@pytest.fixture
def net(monkeypatch):
    n = Net()
    # alerts.httpx IS the httpx module, so this also covers app.channels.
    monkeypatch.setattr(alerts.httpx, "post", n.post)
    return n


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Dana (Slack) owns metrics 1-3, Sam (Slack) 4, Tia (Telegram) 5,
    Tom (Teams, a shared space) 6. Section 2 is hidden and holds metric 7,
    owned by Hal."""
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(dbm, "DB_PATH", path)
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute("INSERT INTO sections (id, name, sort_order, is_enabled) "
                    "VALUES (2,'Parked',1,0)")
        for uid, name, slack_id, ch, addr in (
                (2, "Dana", "U2", None, None),
                (3, "Sam", "U3", None, None),
                (4, "Tia", None, "telegram", "424242"),
                (5, "Tom", None, "teams", None),
                (6, "Hal", "U6", None, None)):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                       slack_member_id, notify_channel, notify_address)
                   VALUES (?,?,?,?,'editor',?,?,?)""",
                (uid, f"u{uid}@x.co", hash_password("pw-pw-pw-pw"), name,
                 slack_id, ch, addr))
        for mid, section, dri in ((1, 1, 2), (2, 1, 2), (3, 1, 2), (4, 1, 3),
                                  (5, 1, 4), (6, 1, 5), (7, 2, 6)):
            con.execute(
                """INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                        start_week, dri_user_id)
                   VALUES (?, ?, ?, 'numeric', 'sum', '2026-01-05', ?)""",
                (mid, section, f"Metric {mid}", dri))
        dbm.set_setting(con, "alerts_enabled", "1")
        dbm.set_setting(con, "nudges_enabled", "1")
        dbm.set_setting(con, "slack_bot_token", "xoxb-test")
        dbm.set_setting(con, "slack_channel_id", "C1")
        dbm.set_setting(con, "telegram_bot_token", "tg-token")
        dbm.set_setting(con, "teams_webhook_url", "https://teams.example.com/hook")
        dbm.set_setting(con, "public_base_url", BASE)
    return SimpleNamespace(path=path)


def _enter(con, metric_id, week, value):
    y, q = wk.quarter_of(week)
    con.execute("""INSERT OR IGNORE INTO targets (metric_id, year, quarter,
                   baseline_value, stretch_value) VALUES (?,?,?,10,12)""",
                (metric_id, y, q))
    con.execute("""INSERT INTO entries (metric_id, week_start, value_numeric,
                   source, entered_by_user_id) VALUES (?,?,?,'manual',2)""",
                (metric_id, week.isoformat(), value))


def _red(con, metric_id, *weeks_back):
    """A miss (2 against a target of 10) in each of the given closed weeks,
    0 = the due week."""
    for i in weeks_back:
        _enter(con, metric_id, DUE - timedelta(days=7 * i), 2)


def _fill_all(con, *, except_ids=()):
    """A green number for every metric in the due week, so only what a test
    sets up is red or missing."""
    for mid in range(1, 8):
        if mid not in except_ids:
            _enter(con, mid, DUE, 11)


def _claims(alert_type=None):
    with dbm.get_db() as con:
        q = "SELECT metric_id, alert_type FROM alerts_sent"
        rows = con.execute(q).fetchall()
    return {(r["metric_id"], r["alert_type"]) for r in rows
            if alert_type is None or r["alert_type"] == alert_type}


def _last_run(kind):
    with dbm.get_db() as con:
        return con.execute("SELECT * FROM sweep_runs WHERE kind=? ORDER BY id DESC",
                           (kind,)).fetchone()


# ------------------------------------------------- 1. no re-escalation on pending
def test_a_pending_week_does_not_re_escalate_the_reds_before_it(env, net):
    """Red the two weeks before the due week, nothing entered for the due week
    yet. The board still counts the streak; the sweep must not claim a rung
    for a week that is not red."""
    with dbm.get_db() as con:
        _red(con, 1, 1, 2)
        _fill_all(con, except_ids=(1,))
        row = next(r for s in gridm.build_grid(con, TUE).sections
                   for r in s.rows if r.metric_id == 1)
    assert row.red_streak == 2 and row.last_state != "red", "display unchanged"

    assert alerts.red_sweep(TUE) == 0
    assert net.calls == [], "nothing re-posted for a week with no number"
    assert _claims() == set(), "and no rung burned for it"

    # The number arrives red: now it is week 3, and week 3 is what fires.
    with dbm.get_db() as con:
        _red(con, 1, 0)
    assert alerts.red_sweep(TUE) == 1
    assert _claims() == {(1, "red_week3")}
    assert "week 3 in a row" in net.dms()[0]["text"]


# ---------------------------------------------------- 2. one red DM per person
def test_three_reds_are_one_dm_with_a_1_3_1_link_each(env, net):
    with dbm.get_db() as con:
        _red(con, 1, 0)
        _red(con, 2, 0, 1)
        _red(con, 3, 0)
        _fill_all(con, except_ids=(1, 2, 3))
    assert alerts.red_sweep(TUE) == 3
    dms = net.dms()
    assert [d["channel"] for d in dms] == ["U2"], "one message, not three"
    text = dms[0]["text"]
    assert "3 metrics you own were red" in text
    assert '"Metric 1" - week 1 in a row' in text
    assert '"Metric 2" - week 2 in a row' in text
    for mid in (1, 2, 3):
        assert f"<{BASE}/131/{mid}/{DUE.isoformat()}|File the 1-3-1>" in text


def test_no_public_base_url_means_no_links_rather_than_broken_ones(env, net):
    with dbm.get_db() as con:
        dbm.set_setting(con, "public_base_url", "")
        _red(con, 1, 0)
    assert alerts.red_sweep(TUE) == 1
    text = net.dms()[0]["text"]
    assert "/131/" not in text and "1-3-1" in text


def test_red_dms_go_over_the_owners_own_private_channel(env, net):
    """Telegram gets it; Teams is a shared space, and week 1 is DM-only."""
    with dbm.get_db() as con:
        _red(con, 5, 0)   # Tia, Telegram
        _red(con, 6, 0)   # Tom, Teams
    alerts.red_sweep(TUE)
    tg = [c for c in net.calls if "api.telegram.org" in c.url]
    assert len(tg) == 1 and tg[0].json["chat_id"] == "424242"
    assert f"{BASE}/131/5/{DUE.isoformat()}" in tg[0].json["text"]
    assert not [c for c in net.calls if "teams.example.com" in c.url]
    # Tom's week-1 red reached nobody, so it is not marked sent.
    assert _claims() == {(5, "red_week1")}
    assert "no private message channel" in _last_run("red")["detail"]


# ------------------------------------------------- 3. failed sends release claims
def test_a_failed_red_dm_is_retried_not_lost(env, net):
    with dbm.get_db() as con:
        _red(con, 1, 0)
    net.fail = True
    assert alerts.red_sweep(TUE) == 0, "a failed send is not counted"
    assert _claims() == set(), "the claim was handed back"
    run = _last_run("red")
    assert run["outcome"] == "error" and "failed to send" in run["detail"]

    net.fail = False
    assert alerts.red_sweep(TUE) == 1
    assert _claims() == {(1, "red_week1")}


def test_a_week_two_red_stays_claimed_if_the_channel_got_it(env, net):
    """Delivered anywhere = sent. Releasing it would re-post the channel."""
    with dbm.get_db() as con:
        _red(con, 1, 0, 1)

    def dm_fails(url, payload):
        net.fail = payload.get("channel", "").startswith("U")
    net.during = dm_fails
    assert alerts.red_sweep(TUE) == 1
    assert _claims() == {(1, "red_week2")}


def test_a_failed_nudge_is_retried_not_lost(env, net):
    net.fail = True
    assert alerts.nudge_sweep("nudge1", TUE) == 0
    assert _claims("nudge1") == set()
    run = _last_run("nudge1")
    assert run["outcome"] == "error" and "failed to send" in run["detail"]

    net.fail = False
    assert alerts.nudge_sweep("nudge1", TUE) > 0
    assert (1, "nudge1") in _claims("nudge1")


# --------------------------------------- 4. no write lock held across the network
def _assert_unlocked_during_sends(env, net):
    """Inside every outbound call: another connection can WRITE with no
    waiting at all (a logged-in request's session touch), and the claims
    are already committed (visible from outside)."""
    problems = []

    def probe(url, payload):
        other = sqlite3.connect(env.path, timeout=0)
        try:
            other.execute("INSERT OR REPLACE INTO settings (key, value) "
                          "VALUES ('probe', 'x')")
            other.commit()
        except sqlite3.OperationalError as e:
            problems.append(f"{url}: {e}")
        finally:
            other.close()
    net.during = probe
    return problems


def test_red_sweep_holds_no_write_lock_while_sending(env, net):
    with dbm.get_db() as con:
        _red(con, 1, 0, 1)   # week 2: a DM and a channel reply
        _red(con, 4, 0)
    problems = _assert_unlocked_during_sends(env, net)
    seen = []
    inner = net.during

    def also_check_claims(url, payload):
        inner(url, payload)
        seen.append(len(_claims()))
    net.during = also_check_claims
    alerts.summary_sweep(TUE)
    alerts.red_sweep(TUE)
    assert net.calls and problems == []
    assert seen[-1] == 2, "claims were committed before the sends"


def test_nudge_and_stale_sweeps_hold_no_write_lock_while_sending(env, net):
    problems = _assert_unlocked_during_sends(env, net)
    alerts.nudge_sweep("nudge1", TUE)
    alerts.stale_sweep(WED)
    assert len(net.calls) > 3 and problems == []


# ----------------------------------------------------- 5. hidden sections
def test_the_stale_chase_skips_hidden_sections(env, net):
    """Hal's metric is in a hidden section: never nudged, so never named."""
    with dbm.get_db() as con:
        _fill_all(con, except_ids=(1, 7))
    assert alerts.stale_sweep(WED) == 1
    channel = "\n".join(p["text"] for p in net.channel_posts())
    assert "1 of 6 metrics" in channel
    assert "Hal" not in channel and "Metric 7" not in channel
    assert "U6" not in [d["channel"] for d in net.dms()]
    assert (7, "stale") not in _claims()


# ------------------------------------------- 6. no magic link into a shared space
def test_a_shared_channel_gets_the_plain_checkin_link(env, net):
    alerts.nudge_sweep("nudge1", TUE)
    teams = [c for c in net.calls if "teams.example.com" in c.url]
    assert len(teams) == 1
    text = teams[0].json["text"]
    assert f"[Update your numbers here]({BASE}/checkin)" in text
    assert "?t=" not in text
    slack = [d for d in net.dms() if d["channel"] == "U2"][0]["text"]
    assert f"<{BASE}/checkin?t=" in slack, "a private DM keeps its magic link"
    with dbm.get_db() as con:
        owners = {r["user_id"] for r in con.execute("SELECT user_id FROM magic_links")}
    assert 5 not in owners, "no credential is even minted for Tom"


# ------------------------------------------------------------ 7. scheduler
def test_the_scheduler_tolerates_a_late_run():
    from app.main import scheduler
    assert scheduler._job_defaults["misfire_grace_time"] >= 3600
    assert scheduler._job_defaults["coalesce"] is True


def test_scheduled_slots_are_in_the_business_timezone():
    assert alerts.scheduled_at("summary", TUE) == datetime(
        2026, 8, 25, 13, 0, tzinfo=timezone.utc)
    assert alerts.scheduled_at("stale", TUE) == datetime(
        2026, 8, 26, 13, 0, tzinfo=timezone.utc)


def _run_at(kind, when, outcome="sent"):
    with dbm.get_db() as con:
        con.execute("INSERT INTO sweep_runs (kind, ran_at, outcome, detail) "
                    "VALUES (?,?,?,'x')",
                    (kind, when.strftime("%Y-%m-%d %H:%M:%S"), outcome))


def test_missed_sweeps_are_the_passed_slots_with_no_run(env):
    monday = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
    assert alerts.missed_sweeps(monday) == []
    tue_0803 = datetime(2026, 8, 25, 13, 3, tzinfo=timezone.utc)
    assert alerts.missed_sweeps(tue_0803) == ["summary"]
    assert alerts.missed_sweeps(WED) == ["summary", "red", "stale"]

    _run_at("summary", datetime(2026, 8, 18, 13, 0))    # LAST week's: no help
    _run_at("red", datetime(2026, 8, 25, 13, 5), "error")  # crashed: retry
    _run_at("stale", datetime(2026, 8, 26, 13, 0))       # ran this week
    assert alerts.missed_sweeps(WED) == ["summary", "red"]


def test_catch_up_runs_summary_before_red(env, monkeypatch):
    order = []
    for kind in ("summary", "red", "stale"):
        monkeypatch.setattr(alerts, f"{kind}_sweep",
                            lambda now=None, k=kind: order.append(k) or 0)
    assert alerts.catch_up_sweeps(WED) == ["summary", "red", "stale"]
    assert order == ["summary", "red", "stale"]


def test_catch_up_end_to_end_threads_the_escalation(env, net):
    with dbm.get_db() as con:
        _red(con, 1, 0, 1, 2)
    alerts.catch_up_sweeps(WED)
    with dbm.get_db() as con:
        parent = alerts._thread_ts(con, DUE, "summary")
    escalation = [p for p in net.channel_posts() if "Escalating" in p["text"]]
    assert parent and escalation[0]["thread_ts"] == parent
    assert alerts.missed_sweeps(WED) == [], "and a second boot does nothing"


def test_a_crash_is_recorded_even_though_the_sweep_rolled_back(env, net,
                                                                 monkeypatch):
    def boom(con, now):
        con.execute("INSERT INTO sweep_runs (kind, outcome, detail) "
                    "VALUES ('red','sent','half-done')")   # rolls back
        raise RuntimeError("https://hooks.slack.com/services/SECRET")
    monkeypatch.setattr(alerts.gridm, "build_grid", boom)
    assert alerts.guarded("red", alerts.red_sweep)(TUE) == 0
    with dbm.get_db() as con:
        rows = con.execute("SELECT * FROM sweep_runs WHERE kind='red'").fetchall()
    assert [(r["outcome"]) for r in rows] == ["error"]
    assert "RuntimeError" in rows[0]["detail"]
    assert "SECRET" not in rows[0]["detail"], "the message can carry a webhook URL"


def test_the_error_outcome_migration_keeps_history(tmp_path, monkeypatch):
    from migrate import sweep_error
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "old.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("DROP TABLE sweep_runs")
        con.execute("""CREATE TABLE sweep_runs (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN
                ('nudge1','nudge2','summary','stale','red')),
            ran_at TEXT NOT NULL DEFAULT (datetime('now')),
            outcome TEXT NOT NULL CHECK (outcome IN ('sent','nothing','skipped')),
            detail TEXT NOT NULL,
            sent_count INTEGER NOT NULL DEFAULT 0)""")
        con.execute("INSERT INTO sweep_runs (kind, outcome, detail) "
                    "VALUES ('red','sent','kept')")
    with dbm.get_db() as con:
        assert sweep_error.needs_migration(con)
        sweep_error.migrate(con)
        assert not sweep_error.needs_migration(con)
        con.execute("INSERT INTO sweep_runs (kind, outcome, detail) "
                    "VALUES ('red','error','x')")
        assert [r["detail"] for r in con.execute(
            "SELECT detail FROM sweep_runs ORDER BY id")] == ["kept", "x"]
