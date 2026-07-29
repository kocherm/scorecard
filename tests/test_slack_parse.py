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
    assert parse_reply("1: $1500") == [(1, "$1500")]
    assert parse_reply("2: 80%") == [(2, "80%")]


def test_thousands_separators_survive_the_comma_split():
    # A comma between digits is part of the number, not an item boundary -
    # people write MRR as "$1,500" and used to get an error for it.
    assert parse_reply("1: $1,500") == [(1, "$1,500")]
    assert parse_reply("1: 1,500, 2: G") == [(1, "1,500"), (2, "G")]
    assert parse_reply("1: 1,234,567") == [(1, "1,234,567")]


def test_the_shapes_people_actually_type():
    assert parse_reply("1. 12, 2. G") == [(1, "12"), (2, "G")]     # mirrors the list
    assert parse_reply("1) 12\n2) G") == [(1, "12"), (2, "G")]
    assert parse_reply("#1: 12") == [(1, "12")]
    assert parse_reply("1 - 12") == [(1, "12")]
    assert parse_reply("1 – 12") == [(1, "12")]                    # en dash from phones


def test_a_decimal_is_a_number_not_an_index():
    # "1.5" must not read as item 1 value 5 - the dot separator needs a space.
    assert parse_reply("1.5", expected=1) == [(1, "1.5")]
    assert parse_reply("1: 1.5") == [(1, "1.5")]


def test_unnumbered_values_are_taken_in_order_when_the_count_matches():
    assert parse_reply("25, 30, G", expected=3) == [(1, "25"), (2, "30"), (3, "G")]
    assert parse_reply("12", expected=1) == [(1, "12")]
    # A partial list would mean guessing which metrics were answered.
    assert isinstance(parse_reply("25, 30", expected=3), str)
    assert isinstance(parse_reply("25, 30", expected=None), str)


def test_mixing_numbered_and_bare_values_is_refused():
    out = parse_reply("1: 25, 30", expected=2)
    assert isinstance(out, str) and "30" in out


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
