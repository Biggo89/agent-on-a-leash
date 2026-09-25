"""The cardholder's own clock: Swiss local time, computed, never looked up.

Purchase timestamps are UTC (data_dictionary.md). A customer who writes *"never at the
weekend"* means their own Saturday. A Friday 23:30 UTC order in September is Saturday 01:30
in Zurich. specs/check-spending-hours.md §"Days".

Pure arithmetic over the EU daylight-saving rule, which Switzerland follows: summer time
(UTC+2) from the last Sunday of March 01:00 UTC to the last Sunday of October 01:00 UTC,
otherwise UTC+1. No timezone database: `domain/` reads no file (AGENTS.md §3.2), and the rule
is two lines in any language a port might use. `tests/unit/test_localtime.py` checks it hour by
hour against `zoneinfo`'s Europe/Zurich.

ASSUMPTION: every cardholder is in Swiss time. The pack states no timezone. All thirty
customers, repo and live, live in a Swiss region (`customers.csv` `home_region`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

ZONE = "Europe/Zurich"
_WINTER = timezone(timedelta(hours=1), "CET")
_SUMMER = timezone(timedelta(hours=2), "CEST")

#: Day names as the facet writes them, Monday first like `datetime.weekday()`.
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES: dict[str, str] = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}


def _last_sunday(year: int, month: int) -> date:
    """The last Sunday of a 31-day month (March and October both are)."""
    last = date(year, month, 31)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def swiss_time(moment: datetime) -> datetime:
    """`moment` on the clock in Zurich, as an aware datetime (CET or CEST)."""
    utc = moment.astimezone(UTC)
    start = datetime.combine(_last_sunday(utc.year, 3), datetime.min.time(), UTC) + timedelta(
        hours=1
    )
    end = datetime.combine(_last_sunday(utc.year, 10), datetime.min.time(), UTC) + timedelta(
        hours=1
    )
    return utc.astimezone(_SUMMER if start <= utc < end else _WINTER)


def swiss_weekday(moment: datetime) -> str:
    """The day of the week in Zurich, as the facet writes it: `mon` … `sun`."""
    return WEEKDAYS[swiss_time(moment).weekday()]
