"""The ramp against a real Postgres, because "survives a restart" is the point.

An acceptance criterion in its own words: the ramp start date is persisted, so a
restart does not reset or skip the ramp. A ramp a redeploy silently restarted
would hold Rebe at three posts a day forever; one a redeploy skipped would
front-load a number that is still a fortnight old.

A restart here means what it means in the container: a **new pool** and a new
store reading the same rows, with everything the restarted process does happening
while that pool is open. A pool is not reopened and a store does not outlive its
`connect` block, any more than the agent outlives its own process.

These tests need a database. CI gives them one; locally they skip unless
`REBE_TEST_DATABASE_URL` names a throwaway Postgres, for example:

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=rebe --name rebe-pg postgres:16
    REBE_TEST_DATABASE_URL=postgresql://postgres:rebe@127.0.0.1:5432/postgres pytest
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import psycopg
import pytest

from rebe_agent.clock import ManualClock
from rebe_agent.db import open_pool
from rebe_agent.ramp import (
    PostgresRampStore,
    Ramp,
    RampReason,
    RampState,
)
from rebe_agent.sends import InMemorySendLog, SendKind, SendRecord, fingerprint
from tests.support import GROUP, MEXICO_CITY, NOON

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)

WEEK = timedelta(days=7)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


async def sent_at(log: InMemorySendLog, when: datetime) -> None:
    """One post Rebe already made, so no idle gap opens across a simulated week.

    The send log is the other half of what the ramp reads, and in the container
    it is a table in this same database. A restart that came back to an empty one
    would be a number that has sent nothing for a week, which is a different
    ticket's rule doing its job rather than this one failing.
    """
    await log.record(
        SendRecord(
            sent_at=when,
            day=when.astimezone(MEXICO_CITY).date(),
            kind=SendKind.POST,
            chat=GROUP,
            fingerprint=fingerprint(f"nota del {when.isoformat()}"),
        )
    )


@pytest.fixture
async def store() -> AsyncIterator[PostgresRampStore]:
    async with PostgresRampStore.connect(DATABASE_URL) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    """Each test starts from a deployment whose number was never paired."""
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM ramp")


async def test_a_database_nobody_has_touched_has_no_ramp(store: PostgresRampStore) -> None:
    assert await store.state() is None


async def test_connecting_twice_does_not_fight_over_the_schema(
    store: PostgresRampStore, clock: ManualClock
) -> None:
    await store.save(RampState(started_at=clock.now()))

    async with PostgresRampStore.connect(DATABASE_URL) as second:
        assert await second.state() == await store.state()


async def test_the_ramp_start_is_still_there_after_a_restart(
    store: PostgresRampStore, clock: ManualClock
) -> None:
    """The ramp is the row, not the object: a second store, on its own pool, over
    the same database, is what a redeploy looks like.

    Everything the restarted process does happens while that pool is open, the
    way it does in the container: a store outlives a `connect` block no more than
    the agent outlives its own process.
    """
    sends = InMemorySendLog()
    started = (await Ramp(store, clock, sends).state()).started_at

    clock.advance(WEEK + timedelta(hours=1))
    await sent_at(sends, clock.now() - timedelta(hours=2))

    async with PostgresRampStore.connect(DATABASE_URL) as after_restart:
        ramp = Ramp(after_restart, clock, sends)
        state = await ramp.state()

        assert state.started_at == started, "a redeploy neither restarts nor skips the ramp"
        assert state.reason is RampReason.PAIRED
        assert await ramp.post_cap() == 4, "week two, not week one again and not steady state"


async def test_a_hold_survives_a_restart_too(store: PostgresRampStore, clock: ManualClock) -> None:
    """A process that came back mid-outage must not resume sending because it
    forgot the link was down."""
    await Ramp(store, clock, InMemorySendLog()).link_down(
        "Evolution reports the connection as 'close'"
    )

    async with PostgresRampStore.connect(DATABASE_URL) as after_restart:
        halt = await Ramp(after_restart, clock, InMemorySendLog()).halt()

        assert halt is not None
        assert "close" in halt.detail


async def test_a_reconnect_after_a_restart_still_knows_it_was_waiting_for_one(
    store: PostgresRampStore, clock: ManualClock
) -> None:
    """Which is the whole reason the pending-reconnect flag is a column: the
    `open` easily arrives in a different process from the `close`.

    The hold is still live here, so the `open` is what ends it. A hold that had
    already lapsed would be re-entered by the lapse itself, which is a different
    rule and is proved in `tests/test_ramp.py`.
    """
    sends = InMemorySendLog()
    await Ramp(store, clock, sends).link_down("the socket dropped")

    clock.advance(timedelta(minutes=5))
    async with PostgresRampStore.connect(DATABASE_URL) as after_restart:
        ramp = Ramp(after_restart, clock, sends)
        await ramp.link_up()

        assert (await ramp.state()).reason is RampReason.RECONNECTED
        assert await ramp.halt() is None
        assert await ramp.post_cap() == 3, "and back on the week-one clamp"


async def test_every_field_makes_the_round_trip(
    store: PostgresRampStore, clock: ManualClock
) -> None:
    """Including the two that are allowed to be absent, because a `None` that came
    back as something else would be a halt nobody asked for."""
    written = RampState(
        started_at=clock.now(),
        reason=RampReason.IDLE,
        link_down_until=clock.now() + timedelta(minutes=30),
        link_detail="the socket dropped",
        backing_off_until=None,
        back_off_detail="",
    )

    await store.save(written)
    read = await store.state()

    assert read == written


async def test_there_is_only_ever_one_ramp(store: PostgresRampStore, clock: ManualClock) -> None:
    """Two rows would be two ramps, and the number only has one."""
    await store.save(RampState(started_at=clock.now()))
    await store.save(RampState(started_at=clock.now() + WEEK, reason=RampReason.RECONNECTED))

    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM ramp")
        row = await cursor.fetchone()

    assert row is not None and row[0] == 1
    state = await store.state()
    assert state is not None and state.reason is RampReason.RECONNECTED


async def test_a_store_whose_table_was_never_prepared_makes_it_on_first_use(
    store: PostgresRampStore, clock: ManualClock
) -> None:
    """Boot is allowed to fail: the `rebe` database can be seconds behind the
    container, and a ramp that missed its one chance would leave Rebe posting as
    if she were a month old."""
    async with open_pool(DATABASE_URL) as pool:
        # A bare store rather than `connect`, which would prepare the table for
        # it: what is under test is the first write making its own table.
        never_prepared = PostgresRampStore(pool)

        written = await never_prepared.save(RampState(started_at=clock.now()))
        assert written.started_at == clock.now()

    # Read back through the fixture's pool, which is still open, because the row
    # is what outlives the pool that wrote it.
    assert await store.state() == written
