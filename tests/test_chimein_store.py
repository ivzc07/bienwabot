"""The chime-in log against a real Postgres, because the day's count outlives a restart.

Same deal as `tests/test_send_log.py`: CI gives these a database, and locally
they skip unless `REBE_TEST_DATABASE_URL` names a throwaway one. The in-memory
log is exercised by `tests/test_chimeins.py` and `tests/test_reply.py`; what is
proved here is that the ceiling still holds when the count is a table.
"""

from __future__ import annotations

import os
import random
from collections.abc import AsyncIterator
from datetime import timedelta

import psycopg
import pytest

from rebe_agent.chimeins import Allowance, ChimeIn, ChimeInBudget, PostgresChimeInLog
from rebe_agent.clock import ManualClock
from tests.support import GROUP, MEXICO_CITY, NOON, TODAY

ALLOWANCE = Allowance(cooldown=timedelta(0))
"""The cooldown lifted, so these tests are about the count and nothing else."""

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)


@pytest.fixture
async def log() -> AsyncIterator[PostgresChimeInLog]:
    async with PostgresChimeInLog.connect(DATABASE_URL) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM chime_ins")


async def test_a_quiet_day_counts_nothing(log: PostgresChimeInLog) -> None:
    assert await log.count_on(TODAY) == 0
    assert await log.latest() is None


async def test_what_she_volunteered_is_counted(log: PostgresChimeInLog) -> None:
    await log.record(ChimeIn(at=NOON, day=TODAY, chat=GROUP))

    assert await log.count_on(TODAY) == 1


async def test_yesterdays_chime_ins_are_not_todays(log: PostgresChimeInLog) -> None:
    """The allowance is per day, so a day that closed takes its count with it."""
    yesterday = NOON - timedelta(days=1)
    await log.record(ChimeIn(at=yesterday, day=yesterday.date(), chat=GROUP))

    assert await log.count_on(TODAY) == 0


async def test_the_latest_one_is_what_the_cooldown_reads(log: PostgresChimeInLog) -> None:
    await log.record(ChimeIn(at=NOON - timedelta(hours=4), day=TODAY, chat=GROUP))
    await log.record(ChimeIn(at=NOON, day=TODAY, chat=GROUP))

    latest = await log.latest()
    assert latest is not None and latest.at == NOON


async def test_the_ceiling_survives_the_process_it_was_counted_in(
    log: PostgresChimeInLog,
) -> None:
    """The whole reason this is a table: a crash loop must not hand her three
    more unprompted chime-ins every time it comes back up."""
    clock = ManualClock(NOON, MEXICO_CITY)
    before = ChimeInBudget(log, clock, rng=random.Random(1), allowance=ALLOWANCE)
    for _ in range(ALLOWANCE.per_day):
        await before.spend(GROUP)

    async with PostgresChimeInLog.connect(DATABASE_URL) as after_restart:
        budget = ChimeInBudget(after_restart, clock, rng=random.Random(1), allowance=ALLOWANCE)
        refusal = await budget.refuses()
        assert refusal is not None and "today already" in refusal
