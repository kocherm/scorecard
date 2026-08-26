"""What #scorecard gets, and what it no longer gets.

The bug being fixed: fourteen top-level posts on one Wednesday morning, one per
missing metric, plus seven DMs to the one person who owned seven of them. The
rules pinned here are that the channel gets ONE message per sweep with its
detail in a thread, that each person gets ONE DM, and that a week-1 red never
reaches the channel at all.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import alerts
from app import db as dbm
from app import weeks as wk
from app.auth import hash_password

# Wednesday 09:00 Chicago of the week AFTER the due week, so the stale sweep's
# grace period has passed and last_closed_week is the week being chased.
NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
DUE = wk.last_closed_week(NOW)
LABEL = wk.quarter_label(DUE)


@pytest.fixture
def slack(monkeypatch):
    """Record every Slack call. chat.postMessage answers with a ts so threads
    are possible; the webhook fixture below removes that."""
    posts, webhooks = [], []
    ts = iter(f"1700000000.0000{i:02d}" for i in range(1, 99))

    def post(url, headers=None, json=None, timeout=None):
        if "hooks.slack.com" in url:
            webhooks.append(json["text"])
            return SimpleNamespace(status_code=200, text="ok",
                                   json=lambda: {"ok": True})
        posts.append(json)
        return SimpleNamespace(status_code=200, text="ok",
                               json=lambda: {"ok": True, "ts": next(ts)})

    monkeypatch.setattr(alerts.httpx, "post", post)
    return SimpleNamespace(posts=posts, webhooks=webhooks)


def _channel_posts(slack, channel="C1"):
    return [p for p in slack.posts if p["channel"] == channel]


def _dms(slack):
    return [p for p in slack.posts if p["channel"].startswith("U")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Three people, seven metrics, nothing entered for the due week - the
    shape that produced the wall."""
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        for uid, email, name, slack_id in ((2, "d@x.co", "Dana", "U2"),
                                           (3, "s@x.co", "Sam", "U3")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      slack_member_id) VALUES (?,?,?,?,'editor',?)""",
                (uid, email, hash_password("pw-pw-pw-pw"), name, slack_id))
        owners = [2, 2, 2, 2, 3, 3, None]  # Dana 4, Sam 2, unassigned 1
        for i, dri in enumerate(owners, start=1):
            con.execute(
                """INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                        start_week, dri_user_id)
                   VALUES (?, 1, ?, 'numeric', 'sum', '2026-01-05', ?)""",
                (i, f"Metric {i}", dri))
            y, q = wk.quarter_of(DUE)
            con.execute("""INSERT INTO targets (metric_id, year, quarter,
                           baseline_value, stretch_value) VALUES (?,?,?,10,12)""",
                        (i, y, q))
        dbm.set_setting(con, "alerts_enabled", "1")
        dbm.set_setting(con, "slack_bot_token", "xoxb-test")
        dbm.set_setting(con, "slack_channel_id", "C1")
        dbm.set_setting(con, "public_base_url", "https://score.example.com")
    yield


# ------------------------------------------------------------- stale roll-up
def test_seven_missing_metrics_are_one_channel_message(env, slack):
    """The regression this file exists for: one top-level post, not seven."""
    assert alerts.stale_sweep(NOW) == 7
    top = [p for p in _channel_posts(slack) if "thread_ts" not in p]
    assert len(top) == 1, f"expected one top-level post, got {len(top)}"
    head = top[0]["text"]
    assert "7 of 7 metrics still have no number" in head
    # Who owes how many is the line people actually read; names go in the thread.
    assert "Owed by: Dana 4, Sam 2, unassigned 1" in head
    assert "Metric 1" not in head


def test_the_metric_names_land_in_the_thread(env, slack):
    alerts.stale_sweep(NOW)
    replies = [p for p in _channel_posts(slack) if p.get("thread_ts")]
    assert len(replies) == 1
    body = replies[0]["text"]
    assert all(f"- Metric {i}" in body for i in range(1, 8))
    assert "(unassigned)" in body


def test_replies_hang_off_the_headline_that_was_posted(env, slack):
    alerts.stale_sweep(NOW)
    with dbm.get_db() as con:
        parent = alerts._thread_ts(con, DUE, "stale")
    replies = [p for p in _channel_posts(slack) if p.get("thread_ts")]
    assert parent and all(r["thread_ts"] == parent for r in replies)


def test_one_dm_per_person_not_one_per_metric(env, slack):
    """Dana owns four of the seven. Four separate DMs is the same wall, moved."""
    alerts.stale_sweep(NOW)
    dms = _dms(slack)
    assert [d["channel"] for d in dms] == ["U2", "U3"]
    dana = dms[0]["text"]
    assert dana.count("Metric") == 4, "Dana's four metrics belong in one message"
    assert "Update your numbers here" in dana, "the DM carries the magic link"


def test_a_rerun_appends_to_the_thread_instead_of_announcing_twice(env, slack):
    alerts.stale_sweep(NOW)
    first = len(_channel_posts(slack))
    assert alerts.stale_sweep(NOW) == 0, "already-flagged metrics are not re-flagged"
    assert len(_channel_posts(slack)) == first, "nothing new to say, nothing posted"

    # A metric that starts later goes stale after the first run; its line must
    # append to the existing thread rather than start a second headline.
    with dbm.get_db() as con:
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                            rollup, start_week, dri_user_id)
                       VALUES (99, 1, 'Late arrival', 'numeric', 'sum', ?, 3)""",
                    (DUE.isoformat(),))
    assert alerts.stale_sweep(NOW) == 1
    new = _channel_posts(slack)[first:]
    assert [p for p in new if "thread_ts" not in p] == [], "no second headline"
    assert "Late arrival" in new[0]["text"]


def test_a_webhook_cannot_thread_so_it_gets_one_folded_message(env, slack):
    """The volume fix must not depend on anyone reconfiguring Slack first."""
    with dbm.get_db() as con:
        dbm.set_setting(con, "slack_webhook_url",
                        "https://hooks.slack.com/services/T/B/x")
    alerts.stale_sweep(NOW)
    assert len(slack.webhooks) == 1, "one message, detail folded in"
    assert "Owed by: Dana 4" in slack.webhooks[0]
    assert "- Metric 1" in slack.webhooks[0]
    assert _channel_posts(slack) == []


def test_a_webhook_run_is_still_only_announced_once(env, slack):
    with dbm.get_db() as con:
        dbm.set_setting(con, "slack_webhook_url",
                        "https://hooks.slack.com/services/T/B/x")
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                            rollup, start_week, dri_user_id)
                       VALUES (99, 1, 'Late arrival', 'numeric', 'sum', ?, 3)""",
                    (DUE.isoformat(),))
    alerts.stale_sweep(NOW)
    alerts.stale_sweep(NOW)
    assert len(slack.webhooks) == 1


# -------------------------------------------------------- week-closed summary
def test_the_summary_is_counts_first_names_in_the_thread(env, slack):
    assert alerts.summary_sweep(NOW) == 1
    posts = _channel_posts(slack)
    top = [p for p in posts if "thread_ts" not in p]
    assert len(top) == 1
    head = top[0]["text"]
    assert f"{LABEL}" in head and "is closed" in head
    assert "7 with no number yet" in head
    assert "Open the board" in head
    assert "Metric 1" not in head
    assert any("Metric 1" in p["text"] for p in posts if p.get("thread_ts"))


def test_the_summary_is_posted_once_a_week(env, slack):
    alerts.summary_sweep(NOW)
    n = len(_channel_posts(slack))
    assert alerts.summary_sweep(NOW) == 0
    assert len(_channel_posts(slack)) == n
    with dbm.get_db() as con:
        r = con.execute("SELECT * FROM sweep_runs WHERE kind='summary' "
                        "ORDER BY id DESC").fetchone()
    assert r["outcome"] == "nothing" and "already summarised" in r["detail"]


# --------------------------------------------------------- escalation ladder
def _go_red(con, metric_id, weeks_back):
    """Enter a miss (2 against a target of 10) for the last N closed weeks."""
    for i in range(weeks_back):
        w = DUE - timedelta(days=7 * i)
        y, q = wk.quarter_of(w)
        con.execute("""INSERT OR IGNORE INTO targets (metric_id, year, quarter,
                       baseline_value, stretch_value) VALUES (?,?,?,10,12)""",
                    (metric_id, y, q))
        con.execute("""INSERT INTO entries (metric_id, week_start, value_numeric,
                       source, entered_by_user_id) VALUES (?,?,2,'manual',2)""",
                    (metric_id, w.isoformat()))


def test_a_week_one_red_stays_in_the_dm(env, slack):
    """The most common rung is one person's homework, not channel business."""
    with dbm.get_db() as con:
        _go_red(con, 1, 1)
    assert alerts.red_sweep(NOW) == 1
    assert _channel_posts(slack) == [], "a first red must not reach the channel"
    assert [d["channel"] for d in _dms(slack)] == ["U2"]
    assert "1-3-1" in _dms(slack)[0]["text"]


def test_week_two_and_three_reds_reach_the_channel_in_one_reply(env, slack):
    with dbm.get_db() as con:
        _go_red(con, 1, 2)   # Dana, week 2
        _go_red(con, 5, 3)   # Sam, week 3
        _go_red(con, 2, 1)   # Dana, week 1 - DM only
    alerts.summary_sweep(NOW)
    before = len(_channel_posts(slack))
    assert alerts.red_sweep(NOW) == 3

    new = _channel_posts(slack)[before:]
    assert len(new) == 1, "every escalation in one reply, not one post each"
    body = new[0]["text"]
    assert "Metric 1" in body and "Metric 5" in body
    assert "Metric 2" not in body, "the week-1 red stayed private"
    assert len(_dms(slack)) == 3, "all three DRIs still hear about it privately"


def test_escalations_thread_under_that_mornings_summary(env, slack):
    with dbm.get_db() as con:
        _go_red(con, 1, 2)
    alerts.summary_sweep(NOW)
    alerts.red_sweep(NOW)
    with dbm.get_db() as con:
        parent = alerts._thread_ts(con, DUE, "summary")
    escalation = _channel_posts(slack)[-1]
    assert escalation["thread_ts"] == parent


def test_a_missing_summary_costs_the_thread_not_the_escalation(env, slack):
    """A tidy channel is never worth a swallowed week-3 red."""
    with dbm.get_db() as con:
        _go_red(con, 1, 3)
    assert alerts.red_sweep(NOW) == 1  # no summary_sweep ran
    top = [p for p in _channel_posts(slack) if "thread_ts" not in p]
    assert len(top) == 1 and "Metric 1" in top[0]["text"]


# ----------------------------------------------------------------- wording
def test_the_wednesday_dm_reads_as_the_third_ask(env, slack):
    """Same list, different sentence. Sending the third ask in the second
    ask's words is how an escalation stops reading like one."""
    alerts.stale_sweep(NOW)
    text = _dms(slack)[0]["text"]
    assert "overdue" in text and "due Monday" in text
    assert "check-in: your numbers" not in text


def test_an_unowned_metric_is_named_unassigned_everywhere(env, slack):
    """grid.Row spells it "-", which reads as a missing value rather than the
    finding it is."""
    alerts.summary_sweep(NOW)
    alerts.stale_sweep(NOW)
    bodies = "\n".join(p["text"] for p in _channel_posts(slack))
    assert "Metric 7 (unassigned)" in bodies
    assert "(-)" not in bodies


def test_an_escalation_follows_the_roll_up_to_the_same_channel(env, slack):
    """With a webhook and a bot both set the roll-up goes through the webhook;
    replying through the bot would land in a different channel."""
    with dbm.get_db() as con:
        dbm.set_setting(con, "slack_webhook_url",
                        "https://hooks.slack.com/services/T/B/x")
        _go_red(con, 1, 3)
    alerts.summary_sweep(NOW)
    alerts.red_sweep(NOW)
    assert _channel_posts(slack) == []
    assert any("Metric 1" in w and "RED" in w for w in slack.webhooks)
