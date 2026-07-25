"""The call-rate guard: the alert at 600, the hard stop at 2,000, and the day roll.

None of this waits on 2,000 real API calls. The counter is a persisted number, so
a test can simply stand the day at 1,999 and take one more step - which is also
how the guard survives a restart mid-storm.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from rebe_agent.clock import ManualClock
from rebe_agent.guard import (
    ALERT_THRESHOLD,
    STOP_THRESHOLD,
    CallRateGuard,
    DailyCallCeilingError,
)
from rebe_agent.usage import CallType, CallUsage, DayTotals, InMemoryUsageStore

MEXICO_CITY = ZoneInfo("America/Mexico_City")
NOON = datetime(2026, 7, 25, 12, 0, tzinfo=MEXICO_CITY)
TODAY = NOON.date()


class RecordingAlerter:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def alert(self, message: str) -> None:
        self.messages.append(message)


@pytest.fixture
def store() -> InMemoryUsageStore:
    return InMemoryUsageStore()


@pytest.fixture
def alerter() -> RecordingAlerter:
    return RecordingAlerter()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
def guard(
    store: InMemoryUsageStore, clock: ManualClock, alerter: RecordingAlerter
) -> CallRateGuard:
    return CallRateGuard(store, clock, alerter)


def stand_at(store: InMemoryUsageStore, calls: int) -> None:
    """Place the day's counter, as a restart would find it."""
    store.seed(TODAY, CallType.REPLY_GATE, DayTotals(calls=calls))


async def test_the_expected_day_is_quiet(
    guard: CallRateGuard, alerter: RecordingAlerter, store: InMemoryUsageStore
) -> None:
    """Steady state is about 175 calls a day; nobody should hear from the guard."""
    stand_at(store, 174)

    await guard.reserve(CallType.NEWS_SUMMARY)

    assert alerter.messages == []


async def test_crossing_six_hundred_alerts_and_keeps_calling(
    guard: CallRateGuard, alerter: RecordingAlerter, store: InMemoryUsageStore
) -> None:
    stand_at(store, ALERT_THRESHOLD - 1)

    reservation = await guard.reserve(CallType.REPLY_GATE)

    assert reservation.calls_today == ALERT_THRESHOLD
    assert len(alerter.messages) == 1
    assert str(ALERT_THRESHOLD) in alerter.messages[0]
    await guard.reserve(CallType.REPLY_GATE)  # and the next call still goes through


async def test_the_six_hundred_alert_fires_once_a_day(
    guard: CallRateGuard, alerter: RecordingAlerter, store: InMemoryUsageStore
) -> None:
    """An alert per call past 600 is an alert storm, which is the same as none."""
    stand_at(store, ALERT_THRESHOLD - 1)

    for _ in range(5):
        await guard.reserve(CallType.REPLY_GATE)

    assert len(alerter.messages) == 1


async def test_a_restart_above_the_alert_line_does_not_re_alert(
    store: InMemoryUsageStore, clock: ManualClock, alerter: RecordingAlerter
) -> None:
    stand_at(store, ALERT_THRESHOLD + 50)

    await CallRateGuard(store, clock, alerter).reserve(CallType.REPLY_GATE)

    assert alerter.messages == []


async def test_reaching_two_thousand_alerts_and_then_stops_the_day(
    guard: CallRateGuard, alerter: RecordingAlerter, store: InMemoryUsageStore
) -> None:
    stand_at(store, STOP_THRESHOLD - 1)

    reservation = await guard.reserve(CallType.REPLY_GATE)
    assert reservation.calls_today == STOP_THRESHOLD
    assert any(str(STOP_THRESHOLD) in message for message in alerter.messages)

    with pytest.raises(DailyCallCeilingError) as caught:
        await guard.reserve(CallType.REPLY_GATE)

    assert caught.value.ceiling == STOP_THRESHOLD
    assert caught.value.day == TODAY


async def test_a_stopped_day_stays_stopped_without_further_counting(
    guard: CallRateGuard, store: InMemoryUsageStore
) -> None:
    """Refused calls are not counted: `calls` stays the number of real requests."""
    stand_at(store, STOP_THRESHOLD)

    for _ in range(3):
        with pytest.raises(DailyCallCeilingError):
            await guard.reserve(CallType.NEWS_SUMMARY)

    assert await store.calls_on(TODAY) == STOP_THRESHOLD


async def test_a_restart_does_not_hand_a_runaway_loop_a_fresh_allowance(
    store: InMemoryUsageStore, clock: ManualClock, alerter: RecordingAlerter
) -> None:
    stand_at(store, STOP_THRESHOLD)

    with pytest.raises(DailyCallCeilingError):
        await CallRateGuard(store, clock, alerter).reserve(CallType.REPLY_GATE)


async def test_the_next_day_starts_clean(
    guard: CallRateGuard, clock: ManualClock, store: InMemoryUsageStore
) -> None:
    stand_at(store, STOP_THRESHOLD)
    with pytest.raises(DailyCallCeilingError):
        await guard.reserve(CallType.REPLY_GATE)

    clock.advance(timedelta(days=1))
    reservation = await guard.reserve(CallType.REPLY_GATE)

    assert reservation.calls_today == 1
    assert reservation.day == TODAY + timedelta(days=1)


async def test_the_day_is_local_not_utc(
    guard: CallRateGuard, clock: ManualClock, store: InMemoryUsageStore
) -> None:
    """23:00 in Mexico City is already tomorrow in UTC; the group's day wins."""
    clock.set(datetime(2026, 7, 25, 23, 0, tzinfo=MEXICO_CITY))

    reservation = await guard.reserve(CallType.NEWS_SUMMARY)

    assert reservation.day == TODAY


async def test_the_ceiling_counts_every_call_type_together(
    guard: CallRateGuard, store: InMemoryUsageStore
) -> None:
    """A loop in one leg must not be masked by the other leg being quiet."""
    store.seed(TODAY, CallType.REPLY_GATE, DayTotals(calls=STOP_THRESHOLD - 1))
    store.seed(TODAY, CallType.NEWS_SUMMARY, DayTotals(calls=1))

    with pytest.raises(DailyCallCeilingError):
        await guard.reserve(CallType.NEWS_SUMMARY)


async def test_usage_is_booked_against_the_day_the_call_was_counted_on(
    guard: CallRateGuard, clock: ManualClock, store: InMemoryUsageStore
) -> None:
    """A call that straddles midnight must not split its count from its tokens."""
    clock.set(datetime(2026, 7, 25, 23, 59, 59, tzinfo=MEXICO_CITY))
    reservation = await guard.reserve(CallType.NEWS_SUMMARY)

    clock.advance(timedelta(seconds=2))
    await guard.record_usage(
        reservation,
        CallUsage(cache_hit_tokens=700, cache_miss_tokens=300, completion_tokens=150),
    )

    totals = (await store.totals_on(TODAY))[CallType.NEWS_SUMMARY]
    assert totals == DayTotals(
        calls=1, cache_hit_tokens=700, cache_miss_tokens=300, completion_tokens=150
    )
    assert await store.totals_on(TODAY + timedelta(days=1)) == {}


async def test_nonsense_thresholds_are_refused(
    store: InMemoryUsageStore, clock: ManualClock, alerter: RecordingAlerter
) -> None:
    with pytest.raises(ValueError, match="alert <= stop"):
        CallRateGuard(store, clock, alerter, alert_threshold=2000, stop_threshold=600)
