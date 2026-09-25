"""Swiss local time by arithmetic, checked against the timezone database it replaces.

`domain/localtime.py` computes Europe/Zurich from the EU daylight-saving rule so `domain/`
reads no file. This test is where the database is allowed: every hour of four years must land
on the same wall clock as `zoneinfo`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from leash.domain.localtime import swiss_time, swiss_weekday

ZURICH = ZoneInfo("Europe/Zurich")


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027])
def test_every_hour_of_the_year_matches_the_timezone_database(year: int) -> None:
    moment = datetime(year, 1, 1, tzinfo=UTC)
    while moment.year == year:
        ours, theirs = swiss_time(moment), moment.astimezone(ZURICH)
        assert ours.replace(tzinfo=None) == theirs.replace(tzinfo=None), moment
        assert ours.utcoffset() == theirs.utcoffset(), moment
        moment += timedelta(minutes=30)


def test_the_switch_happens_at_one_in_the_morning_utc() -> None:
    before = datetime(2026, 3, 29, 0, 59, tzinfo=UTC)
    after = datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert swiss_time(before).hour == 1 and swiss_time(after).hour == 3


def test_a_late_friday_in_summer_is_a_swiss_saturday() -> None:
    assert swiss_weekday(datetime(2026, 9, 4, 23, 30, tzinfo=UTC)) == "sat"
    assert swiss_weekday(datetime(2026, 9, 4, 21, 30, tzinfo=UTC)) == "fri"
