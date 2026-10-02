"""Tests for src/ingestion/dates.parse_posted_at: every source's "date posted"
format ends up as a naive UTC datetime, and bad input is None, never an error.
"""

from datetime import datetime

import pytest

from src.ingestion.dates import parse_posted_at


def test_z_suffix_is_utc():
    # Adzuna's `created` shape. datetime.fromisoformat() rejects the "Z" on
    # Python 3.10, which is why the parser doesn't rely on it.
    parsed = parse_posted_at("2026-06-21T03:12:45Z")
    assert parsed == datetime(2026, 6, 21, 3, 12, 45)
    assert parsed.tzinfo is None


def test_negative_offset_is_converted_to_utc():
    # Greenhouse's shape: 10:55 at UTC-4 is 14:55 UTC.
    parsed = parse_posted_at("2026-06-14T10:55:28-04:00")
    assert parsed == datetime(2026, 6, 14, 14, 55, 28)
    assert parsed.tzinfo is None


def test_positive_offset_is_converted_to_utc_across_midnight():
    # 03:30 at UTC+5:30 is 22:00 the previous day in UTC.
    assert parse_posted_at("2026-06-21T03:30:00+05:30") == datetime(2026, 6, 20, 22, 0, 0)


def test_fractional_seconds():
    # Ashby's `publishedAt` shape (milliseconds, explicit +00:00).
    assert parse_posted_at("2026-06-21T03:12:45.393+00:00") == datetime(
        2026, 6, 21, 3, 12, 45, 393000
    )
    assert parse_posted_at("2026-06-21T03:12:45.393Z") == datetime(
        2026, 6, 21, 3, 12, 45, 393000
    )
    # Fraction lengths fromisoformat() can't take on 3.10: padded or truncated.
    assert parse_posted_at("2026-06-21T03:12:45.5Z").microsecond == 500000
    assert parse_posted_at("2026-06-21T03:12:45.1234567Z").microsecond == 123456


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-06-21T03:12:45", datetime(2026, 6, 21, 3, 12, 45)),  # no zone: UTC
        ("2026-06-21 03:12:45", datetime(2026, 6, 21, 3, 12, 45)),  # space separator
        ("2026-06-21T03:12Z", datetime(2026, 6, 21, 3, 12, 0)),  # no seconds
        ("2026-06-21", datetime(2026, 6, 21, 0, 0, 0)),  # date only
        ("2026-06-21T03:12:45+0200", datetime(2026, 6, 21, 1, 12, 45)),  # no colon
        ("2026-06-21T03:12:45+02", datetime(2026, 6, 21, 1, 12, 45)),  # hours only
        ("  2026-06-21T03:12:45Z  ", datetime(2026, 6, 21, 3, 12, 45)),  # padding
        ("2026-06-21t03:12:45z", datetime(2026, 6, 21, 3, 12, 45)),  # lower case
    ],
)
def test_other_iso_shapes(value, expected):
    assert parse_posted_at(value) == expected


def test_epoch_milliseconds():
    # Lever's `createdAt` shape.
    parsed = parse_posted_at(1782011565000)
    assert parsed == datetime(2026, 6, 21, 3, 12, 45)
    assert parsed.tzinfo is None
    assert parse_posted_at(1782011565123) == datetime(2026, 6, 21, 3, 12, 45, 123000)
    assert parse_posted_at(1782011565000.0) == datetime(2026, 6, 21, 3, 12, 45)
    assert parse_posted_at("1782011565000") == datetime(2026, 6, 21, 3, 12, 45)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "garbage",
        "yesterday",
        "21/06/2026",
        "2026-13-45T00:00:00Z",  # right shape, impossible date
        "2026-06-21T25:00:00Z",  # impossible hour
        "2026-06-21T03:12:45Zjunk",
        "2026-06-21T03:12:45+99:99garbage",
        0,
        -5,
        1782011565,  # epoch SECONDS: would read as January 1970, so rejected
        "20260621",  # digits only, but not a plausible epoch-ms value either
        10**30,  # far beyond datetime's range
        float("nan"),
        float("inf"),
        True,
        False,
        [],
        {},
        {"date": "2026-06-21"},
        b"2026-06-21T03:12:45Z",
        object(),
    ],
    ids=repr,
)
def test_missing_or_unparseable_values_are_none_and_never_raise(value):
    assert parse_posted_at(value) is None
