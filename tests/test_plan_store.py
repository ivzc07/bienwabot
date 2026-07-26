"""The plan store against a real Postgres, because a restart must not lose the day.

Same deal as `tests/test_send_log.py`: CI gives these a database, and locally they
skip unless `REBE_TEST_DATABASE_URL` names a throwaway one. The in-memory store is
exercised by `tests/test_scheduler.py`; what is proved here is that the same
promises hold when the day's plan is six rows and a unique index.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date, datetime, time

import psycopg
import pytest

from rebe_agent.cadence import DayPlan, Slot, SlotState, moment_on
from rebe_agent.plans import PostgresPlanStore
from tests.support import MEXICO_CITY

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)

WEDNESDAY = date(2026, 7, 29)


def slot(window: str, at: time, closes: time) -> Slot:
    return Slot(
        window=window,
        at=moment_on(WEDNESDAY, at, MEXICO_CITY),
        closes=moment_on(WEDNESDAY, closes, MEXICO_CITY),
    )


MORNING = slot("morning", time(9, 14), time(10, 30))
MIDDAY = slot("midday", time(13, 47), time(15, 0))
PLAN = DayPlan(day=WEDNESDAY, slots=(MORNING, MIDDAY))


@pytest.fixture
async def store() -> AsyncIterator[PostgresPlanStore]:
    async with PostgresPlanStore.connect(DATABASE_URL, MEXICO_CITY) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM planned_slots")


async def test_a_day_that_was_never_rolled_has_no_plan(store: PostgresPlanStore) -> None:
    """Which is how the scheduler knows it still has a day to draw."""
    assert await store.plan_on(WEDNESDAY) is None


async def test_a_registered_plan_comes_back_whole(store: PostgresPlanStore) -> None:
    await store.register(PLAN)

    stored = await store.plan_on(WEDNESDAY)
    assert stored is not None
    assert stored.day == WEDNESDAY
    assert [(s.window, s.at, s.closes, s.state) for s in stored.slots] == [
        (s.window, s.at, s.closes, SlotState.PLANNED) for s in PLAN.slots
    ]


async def test_the_times_come_back_in_the_agents_zone(store: PostgresPlanStore) -> None:
    """A `timestamptz` comes back in whatever zone the session is in, and every
    log line and window edge downstream is a statement about Mexico City."""
    await store.register(PLAN)

    stored = await store.plan_on(WEDNESDAY)
    assert stored is not None
    assert [s.at.strftime("%H:%M") for s in stored.slots] == ["09:14", "13:47"]


async def test_registering_the_same_day_twice_does_not_double_register_it(
    store: PostgresPlanStore,
) -> None:
    """A restart that raced the dawn roll must not end up with eight slots, and
    must not redraw the times the first roll already committed to."""
    await store.register(PLAN)
    redrawn = DayPlan(day=WEDNESDAY, slots=(slot("morning", time(10, 2), time(10, 30)), MIDDAY))

    of_record = await store.register(redrawn)

    assert [s.at for s in of_record.slots] == [s.at for s in PLAN.slots]
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM planned_slots")
        row = await cursor.fetchone()
    assert row is not None and row[0] == 2


async def test_what_happened_to_a_slot_is_written_down(store: PostgresPlanStore) -> None:
    await store.register(PLAN)

    await store.settle(WEDNESDAY, "morning", SlotState.POSTED)
    await store.settle(WEDNESDAY, "midday", SlotState.SKIPPED)

    stored = await store.plan_on(WEDNESDAY)
    assert stored is not None
    assert [(s.window, s.state) for s in stored.slots] == [
        ("morning", SlotState.POSTED),
        ("midday", SlotState.SKIPPED),
    ]
    assert stored.pending == ()


async def test_only_the_unfinished_slots_are_still_pending(store: PostgresPlanStore) -> None:
    """What a restart part-way through the day picks up."""
    await store.register(PLAN)
    await store.settle(WEDNESDAY, "morning", SlotState.POSTED)

    async with PostgresPlanStore.connect(DATABASE_URL, MEXICO_CITY) as after_restart:
        stored = await after_restart.plan_on(WEDNESDAY)

    assert stored is not None
    assert [s.window for s in stored.pending] == ["midday"]


async def test_another_days_plan_is_left_alone(store: PostgresPlanStore) -> None:
    thursday = DayPlan(day=date(2026, 7, 30), slots=(MORNING,))
    await store.register(PLAN)
    await store.register(thursday)

    await store.settle(WEDNESDAY, "morning", SlotState.DROPPED)

    stored = await store.plan_on(thursday.day)
    assert stored is not None
    assert [s.state for s in stored.slots] == [SlotState.PLANNED]


async def test_settling_a_slot_that_is_not_in_the_plan_is_not_an_error(
    store: PostgresPlanStore,
) -> None:
    """A window renamed between two deploys must not take the process down."""
    await store.register(PLAN)

    await store.settle(WEDNESDAY, "brunch", SlotState.DROPPED)


async def test_the_stored_datetime_is_the_instant_not_the_wall_clock(
    store: PostgresPlanStore,
) -> None:
    """The plan is a list of instants; the local day it belongs to is its own
    column, so nothing downstream has to reconstruct one from the other."""
    await store.register(PLAN)

    stored = await store.plan_on(WEDNESDAY)
    assert stored is not None
    assert stored.slots[0].at == datetime(2026, 7, 29, 9, 14, tzinfo=MEXICO_CITY)
