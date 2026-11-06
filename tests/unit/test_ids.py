import re

from covey.ids import new_ulid

ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def test_ulid_matches_the_schema_pattern():
    assert ULID.match(new_ulid())


def test_timestamp_part_encodes_milliseconds():
    # Example from the ULID spec: 1469918176385 ms -> "01ARYZ6S41".
    assert new_ulid(now_ms=1469918176385)[:10] == "01ARYZ6S41"


def test_later_ids_sort_after_earlier_ones():
    assert new_ulid(now_ms=1_000) < new_ulid(now_ms=1_001)


def test_ids_in_the_same_millisecond_differ():
    assert len({new_ulid(now_ms=5) for _ in range(100)}) == 100
