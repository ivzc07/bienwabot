"""Time-dependent logic reads a Clock, so tests never wait on the wall clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from rebe_agent.clock import ManualClock, SystemClock

MEXICO_CITY = ZoneInfo("America/Mexico_City")


def test_the_system_clock_answers_in_its_configured_zone() -> None:
    clock = SystemClock(MEXICO_CITY)

    assert clock.now().tzinfo is MEXICO_CITY
    assert clock.zone is MEXICO_CITY


def test_the_system_clock_tracks_utc() -> None:
    clock = SystemClock(MEXICO_CITY)

    drift = abs(clock.now() - datetime.now(UTC))

    assert drift < timedelta(seconds=5)


def test_a_manual_clock_only_moves_when_told() -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=MEXICO_CITY)
    clock = ManualClock(start)

    assert clock.now() == start
    assert clock.now() == start

    clock.advance(timedelta(hours=3))

    assert clock.now() == start + timedelta(hours=3)


def test_a_manual_clock_can_be_set_to_an_exact_instant() -> None:
    clock = ManualClock(datetime(2026, 7, 25, 9, 0, tzinfo=MEXICO_CITY))

    clock.set(datetime(2026, 7, 26, 3, 30, tzinfo=MEXICO_CITY))

    assert clock.now() == datetime(2026, 7, 26, 3, 30, tzinfo=MEXICO_CITY)


def test_a_manual_clock_normalises_naive_instants_into_its_zone() -> None:
    clock = ManualClock(datetime(2026, 7, 25, 9, 0), tz=MEXICO_CITY)

    assert clock.now() == datetime(2026, 7, 25, 9, 0, tzinfo=MEXICO_CITY)


def test_a_manual_clock_refuses_to_go_backwards() -> None:
    clock = ManualClock(datetime(2026, 7, 25, 9, 0, tzinfo=MEXICO_CITY))

    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(minutes=-1))


def test_the_local_hour_is_read_in_the_clocks_zone_not_utc() -> None:
    """One instant, two zones: 09:00 UTC is 03:00 in Mexico City.

    This is what keeps quiet hours and the posting shape honest once the pacer
    lands - they ask a clock for the local hour, never `datetime.now()`.
    """
    instant = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)

    assert ManualClock(instant, tz=MEXICO_CITY).now().hour == 3
    assert ManualClock(instant, tz=UTC).now().hour == 9
