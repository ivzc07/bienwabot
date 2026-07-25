"""The counters against a real Postgres, because "survives a restart" is the point.

These tests need a database. CI gives them one; locally they skip unless
`REBE_TEST_DATABASE_URL` names a throwaway Postgres, for example:

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=rebe --name rebe-pg postgres:16
    REBE_TEST_DATABASE_URL=postgresql://postgres:rebe@127.0.0.1:5432/postgres pytest
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import date, timedelta

import psycopg
import pytest

from rebe_agent.usage import CallType, CallUsage, DayTotals, PostgresUsageStore

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)

DAY = date(2026, 7, 25)


@pytest.fixture
async def store() -> AsyncIterator[PostgresUsageStore]:
    async with PostgresUsageStore.connect(DATABASE_URL) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    """Each test starts from an empty table, not from whatever ran before it."""
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM deepseek_usage")


async def test_the_table_is_created_on_connect(store: PostgresUsageStore) -> None:
    assert await store.calls_on(DAY) == 0


async def test_connecting_twice_does_not_fight_over_the_schema() -> None:
    async with PostgresUsageStore.connect(DATABASE_URL) as second:
        assert await second.calls_on(DAY) >= 0


async def test_calls_accumulate_per_day_and_call_type(store: PostgresUsageStore) -> None:
    assert await store.record_call(DAY, CallType.REPLY_GATE) == 1
    assert await store.record_call(DAY, CallType.REPLY_GATE) == 2
    assert await store.record_call(DAY, CallType.NEWS_SUMMARY) == 3

    totals = await store.totals_on(DAY)
    assert totals[CallType.REPLY_GATE].calls == 2
    assert totals[CallType.NEWS_SUMMARY].calls == 1


async def test_tokens_accumulate_per_day_and_call_type(store: PostgresUsageStore) -> None:
    await store.record_call(DAY, CallType.NEWS_SUMMARY)
    await store.record_usage(
        DAY,
        CallType.NEWS_SUMMARY,
        CallUsage(cache_hit_tokens=700, cache_miss_tokens=300, completion_tokens=150),
    )
    await store.record_usage(
        DAY,
        CallType.NEWS_SUMMARY,
        CallUsage(cache_hit_tokens=650, cache_miss_tokens=350, completion_tokens=120),
    )

    assert (await store.totals_on(DAY))[CallType.NEWS_SUMMARY] == DayTotals(
        calls=1, cache_hit_tokens=1350, cache_miss_tokens=650, completion_tokens=270
    )


async def test_usage_for_a_call_type_never_seen_before_still_lands(
    store: PostgresUsageStore,
) -> None:
    """Ordering must not matter: the row is created by whichever write arrives first."""
    await store.record_usage(DAY, CallType.PROBE, CallUsage(completion_tokens=42))

    assert (await store.totals_on(DAY))[CallType.PROBE].completion_tokens == 42


async def test_the_counters_survive_a_restart(store: PostgresUsageStore) -> None:
    await store.record_call(DAY, CallType.REPLY_GATE)
    await store.record_usage(DAY, CallType.REPLY_GATE, CallUsage(cache_miss_tokens=400))

    async with PostgresUsageStore.connect(DATABASE_URL) as after_restart:
        assert await after_restart.calls_on(DAY) == 1
        assert (await after_restart.totals_on(DAY))[CallType.REPLY_GATE].cache_miss_tokens == 400


async def test_days_do_not_leak_into_each_other(store: PostgresUsageStore) -> None:
    await store.record_call(DAY, CallType.REPLY_GATE)
    await store.record_call(DAY + timedelta(days=1), CallType.REPLY_GATE)

    assert await store.calls_on(DAY) == 1
    assert await store.calls_on(DAY + timedelta(days=1)) == 1


async def test_concurrent_calls_do_not_lose_a_count(store: PostgresUsageStore) -> None:
    """The webhook leg and the news leg can reserve at the same moment."""
    await asyncio.gather(*(store.record_call(DAY, CallType.REPLY_GATE) for _ in range(25)))

    assert await store.calls_on(DAY) == 25
