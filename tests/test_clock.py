"""Time-dependent logic reads a Clock, so tests never wait on the wall clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from rebe_agent.clock import Clock, ManualClock, SystemClock, is_quiet_hours

MEXICO_CITY = ZoneInfo("America/Mexico_City")


def test_the_system_clock_answers_in_its_configured_zone() -> None:
    clock = SystemClock(MEXICO_CITY)

    assert clock.now().tzinfo is MEXICO_CITY
    assert clock.timezone is MEXICO_CITY


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


@pytest.mark.parametrize(
    ("hour", "quiet"),
    [(1, False), (2, True), (4, True), (5, True), (6, False), (14, False), (23, False)],
)
def test_quiet_hours_span_02_00_to_06_00_local(hour: int, quiet: bool) -> None:
    clock: Clock = ManualClock(datetime(2026, 7, 25, hour, 0, tzinfo=MEXICO_CITY))

    assert is_quiet_hours(clock) is quiet


def test_quiet_hours_are_judged_in_the_configured_zone_not_utc() -> None:
    """09:00 UTC is 03:00 in Mexico City - quiet there, wide awake in UTC."""
    utc_morning = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)

    assert is_quiet_hours(ManualClock(utc_morning, tz=MEXICO_CITY)) is True
    assert is_quiet_hours(ManualClock(utc_morning, tz=UTC)) is False
