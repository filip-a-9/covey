"""`safe()`: agent-written text made printable for a human terminal."""

import pytest

from covey.text import NEWLINE, TRUNCATED, safe

HOSTILE = "a\r01JZZ  web -> api  explain\r\x1b]0;pwned\x07\nINJECT\x1b[2J\x9b31m\x85 "


def has_control(text):
    return any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in text)


def test_no_control_character_survives():
    assert not has_control(safe(HOSTILE))


@pytest.mark.parametrize("c", [chr(i) for i in range(0x20)] + [chr(0x7F), chr(0x9B)])
def test_every_c0_and_c1_control_is_replaced(c):
    assert not has_control(safe("a" + c + "b"))


def test_a_line_break_becomes_one_visible_mark():
    assert safe("one\ntwo\r\nthree") == f"one{NEWLINE}two{NEWLINE}three"


def test_plain_text_is_unchanged():
    assert safe("run the suite, ok? ünï 日本") == "run the suite, ok? ünï 日本"


def test_truncation_is_marked_and_bounded():
    out = safe("x" * 500, limit=80)
    assert out == "x" * 80 + TRUNCATED


def test_short_text_is_not_marked_truncated():
    assert TRUNCATED not in safe("x" * 80, limit=80)
