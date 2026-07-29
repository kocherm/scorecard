"""The Scorecard mark on outgoing Slack messages.

Slack has no API for an app's own icon, so per-message icon_url is the only
half of the avatar the app can set. It rides on a scope the workspace may not
have granted yet, which is the whole risk here: a picture must never be able to
cost a nudge.
"""
from types import SimpleNamespace

import pytest

from app import alerts
from app import db as dbm


class FakeResp:
    def __init__(self, payload):
        self._payload, self.status_code = payload, 200
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as c:
        dbm.init_db(c)
        yield c


@pytest.fixture
def slack(monkeypatch):
    """Record what we send Slack and script what it says back, in order."""
    sent, replies = [], [{"ok": True}]

    def post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return FakeResp(replies[min(len(sent) - 1, len(replies) - 1)])

    monkeypatch.setattr(alerts.httpx, "post", post)
    return SimpleNamespace(sent=sent, replies=replies)


def test_the_icon_url_is_absolute_and_needs_the_public_base_url(con):
    assert alerts.bot_icon_url(con) is None, "Slack cannot fetch a relative path"
    dbm.set_setting(con, "public_base_url", "https://score.example.com/")
    assert alerts.bot_icon_url(con) == "https://score.example.com/static/icon-512.png"


def test_a_dm_carries_the_icon(slack):
    assert alerts.post_dm("xoxb-t", "U1", "hi", icon="https://x.co/i.png")
    assert slack.sent[0]["icon_url"] == "https://x.co/i.png"


def test_no_icon_configured_sends_a_plain_message(slack):
    assert alerts.post_dm("xoxb-t", "U1", "hi")
    assert "icon_url" not in slack.sent[0]


def test_a_missing_scope_costs_the_icon_not_the_message(slack):
    """The regression this guards: an app installed before the icon shipped has
    chat:write but not chat:write.customize, so every nudge would come back
    missing_scope and die in a log line nobody reads."""
    slack.replies[:] = [{"ok": False, "error": "missing_scope"}, {"ok": True}]

    assert alerts.post_dm("xoxb-t", "U1", "your numbers are missing",
                          icon="https://x.co/i.png") is True
    assert len(slack.sent) == 2, "the message should have been resent"
    assert "icon_url" not in slack.sent[1]
    assert slack.sent[1]["text"] == "your numbers are missing"


def test_a_real_failure_still_fails_once(slack):
    """Only scope errors are retried - a bad channel is not a costume problem."""
    slack.replies[:] = [{"ok": False, "error": "channel_not_found"}]
    assert alerts.post_channel_bot("xoxb-t", "C1", "hi",
                                   icon="https://x.co/i.png") is False
    assert len(slack.sent) == 1
