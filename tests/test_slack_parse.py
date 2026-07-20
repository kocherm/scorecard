"""The deterministic reply grammar. Pure function, no Slack, no AI."""
from app.slack import parse_reply


def test_canonical_forms():
    assert parse_reply("1: 42, 2: 7") == [(1, "42"), (2, "7")]
    assert parse_reply("1:42 ,2 :G") == [(1, "42"), (2, "G")]
    assert parse_reply("1 = 42; 2 = g") == [(1, "42"), (2, "g")]
    assert parse_reply("1 42") == [(1, "42")]


def test_newline_separated():
    assert parse_reply("1: 42\n2: G\n3: yes") == [(1, "42"), (2, "G"), (3, "yes")]


def test_currency_and_percent_values_pass_through():
    # Commas SPLIT items, so "1: $1,500" would break apart - that's why the
    # DM example never shows thousands separators. Comma-free forms work:
    assert parse_reply("1: $1500") == [(1, "$1500")]
    assert parse_reply("2: 80%") == [(2, "80%")]
    assert isinstance(parse_reply("1: $1,500"), str)  # clean error, not a misparse


def test_bare_number_run_is_not_misread():
    # "142" must not half-match as index 14 value 2.
    assert isinstance(parse_reply("142"), str)


def test_garbage_and_empty_are_errors():
    assert isinstance(parse_reply(""), str)
    assert isinstance(parse_reply("   \n "), str)
    assert isinstance(parse_reply("hello there"), str)
    assert isinstance(parse_reply("1: 42, what?"), str)


def test_value_text_is_preserved_verbatim():
    assert parse_reply("3: on track") == [(3, "on track")]
