"""Parse the "date posted" values the job APIs return into naive UTC datetimes.

The sources disagree on format: Adzuna, Greenhouse and Ashby send ISO 8601
strings (with a trailing "Z" or a UTC offset), Lever sends an integer of
milliseconds since the Unix epoch. parse_posted_at() takes either and returns a
naive UTC datetime, matching every other datetime column on JobPost.

It never raises. A posting date is a nice-to-have, so a missing, malformed or
unexpected value becomes None rather than failing the scrape that carried it.

ISO strings are matched with a regex and built field by field instead of going
through datetime.fromisoformat(): on Python 3.10 that function rejects a
trailing "Z", offsets without a colon, and fractions that aren't exactly 3 or
6 digits, so its behaviour would differ by interpreter version.
"""

import re
from datetime import datetime, timedelta
from typing import Any, Optional

_EPOCH = datetime(1970, 1, 1)
# An epoch value that lands before this is not a real posting date: it is an
# unset 0, or seconds sent where milliseconds were expected (which would read
# as January 1970). Better None than a confidently wrong date.
_EARLIEST_PLAUSIBLE = datetime(2000, 1, 1)

# YYYY-MM-DD, optionally followed by a time (T or space separated; seconds and
# fraction optional) and an optional zone: "Z" or +HH:MM / +HHMM / +HH.
_ISO_8601 = re.compile(
    r"""
    ^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})
    (?:
        [Tt ]
        (?P<hour>\d{2}):(?P<minute>\d{2})
        (?: :(?P<second>\d{2}) (?:[.,](?P<fraction>\d+))? )?
        \s*
        (?P<zone> [Zz] | (?P<sign>[+-])(?P<zone_hours>\d{2})(?::?(?P<zone_minutes>\d{2}))? )?
    )?$
    """,
    re.VERBOSE,
)


def parse_posted_at(value: Any) -> Optional[datetime]:
    """A source's "posted" value -> naive UTC datetime, or None.

    - str: ISO 8601. An offset ("-04:00") or "Z" is converted to UTC and then
      dropped; a value with no zone is taken to be UTC already. A string of
      digits only is treated as epoch milliseconds.
    - int / float: milliseconds since the Unix epoch. Values that would land
      before 2000 (0, or seconds mistaken for milliseconds) give None.
    - anything else, or anything that doesn't parse: None.
    """
    try:
        if isinstance(value, bool):  # bool is an int subclass; True is not a date
            return None
        if isinstance(value, (int, float)):
            return _from_epoch_ms(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return _from_epoch_ms(int(text))
            return _from_iso(text)
    except (ValueError, OverflowError, TypeError):
        # Out-of-range fields (month 13), absurd epochs, NaN/inf.
        return None
    return None


def _from_epoch_ms(milliseconds: float) -> Optional[datetime]:
    # Epoch + timedelta rather than datetime.fromtimestamp(): no dependence on
    # the local timezone or the platform's time_t range.
    parsed = _EPOCH + timedelta(milliseconds=milliseconds)
    return parsed if parsed >= _EARLIEST_PLAUSIBLE else None


def _from_iso(text: str) -> Optional[datetime]:
    match = _ISO_8601.match(text)
    if match is None:
        return None
    part = match.groupdict()

    # Fraction of a second to microseconds: pad or truncate to six digits.
    microsecond = int((part["fraction"] or "").ljust(6, "0")[:6])
    parsed = datetime(
        int(part["year"]),
        int(part["month"]),
        int(part["day"]),
        int(part["hour"] or 0),
        int(part["minute"] or 0),
        int(part["second"] or 0),
        microsecond,
    )

    if part["sign"]:
        offset = timedelta(
            hours=int(part["zone_hours"]), minutes=int(part["zone_minutes"] or 0)
        )
        # Local time = UTC + offset, so UTC = local time - offset.
        parsed = parsed - offset if part["sign"] == "+" else parsed + offset
    return parsed
