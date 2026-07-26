"""The soft pause against a real Postgres, because "survives a restart" is the point.

A pause a redeploy silently undoes is worse than no pause at all: the operator has
no reason to check again, and Rebe starts talking in the middle of whatever made
them ask for quiet.

These tests need a database. CI gives them one; locally they skip unless
`REBE_TEST_DATABASE_URL` names a throwaway Postgres, for example:

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=rebe --name rebe-pg postgres:16
    REBE_TEST_DATABASE_URL=postgresql://postgres:rebe@127.0.0.1:5432/postgres pytest
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta

import psycopg
import pytest

from rebe_agent.clock import ManualClock
from rebe_agent.db import open_pool
from rebe_agent.pause import PostgresPauseSwitch
from tests.support import NOON

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
async def switch(clock: ManualClock) -> AsyncIterator[PostgresPauseSwitch]:
    async with PostgresPauseSwitch.connect(DATABASE_URL, clock) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    """Each test starts from a deployment nobody has ever paused."""
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM soft_pause")


async def test_a_database_nobody_has_touched_is_not_paused(
    switch: PostgresPauseSwitch,
) -> None:
    assert (await switch.state()).paused is False


async def test_connecting_twice_does_not_fight_over_the_schema(clock: ManualClock) -> None:
    async with PostgresPauseSwitch.connect(DATABASE_URL, clock) as second:
        assert (await second.state()).paused in (True, False)


async def test_the_pause_is_still_on_after_a_restart(
    switch: PostgresPauseSwitch, clock: ManualClock
) -> None:
    """The switch is the row, not the object: a second switch over the same
    database is what a redeploy looks like."""
    await switch.set_paused(True, reason="cool it for a bit")

    async with open_pool(DATABASE_URL) as pool:
        after_restart = await PostgresPauseSwitch(pool, clock).state()

    assert after_restart.paused is True
    assert after_restart.reason == "cool it for a bit"
    assert after_restart.since == clock.now()


async def test_resuming_survives_a_restart_the_same_way(
    switch: PostgresPauseSwitch, clock: ManualClock
) -> None:
    await switch.set_paused(True, reason="cool it")
    clock.advance(timedelta(hours=3))
    await switch.set_paused(False)

    async with open_pool(DATABASE_URL) as pool:
        after_restart = await PostgresPauseSwitch(pool, clock).state()

    assert after_restart.paused is False
    assert after_restart.reason == ""


async def test_pausing_an_already_paused_rebe_keeps_the_moment_she_went_quiet(
    switch: PostgresPauseSwitch, clock: ManualClock
) -> None:
    """ "Paused since 14:00" has to mean the silence started then, or an operator
    cannot tell an hour of quiet from a week of it."""
    first = await switch.set_paused(True, reason="cool it")
    clock.advance(timedelta(hours=2))

    again = await switch.set_paused(True, reason="still too much")

    assert again.since == first.since
    assert again.reason == "still too much"


async def test_there_is_only_ever_one_switch(
    switch: PostgresPauseSwitch, clock: ManualClock
) -> None:
    """Two rows would be two answers to "is she paused", and the pacer reads one."""
    await switch.set_paused(True, reason="uno")
    async with open_pool(DATABASE_URL) as pool:
        await PostgresPauseSwitch(pool, clock).set_paused(False)

    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM soft_pause")
        row = await cursor.fetchone()

    assert row is not None and row[0] == 1
    assert (await switch.state()).paused is False
