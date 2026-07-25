"""The send log against a real Postgres, because "survives a restart" is the point.

Same deal as `tests/test_usage_store.py`: CI gives these a database, and locally
they skip unless `REBE_TEST_DATABASE_URL` names a throwaway one.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import psycopg
import pytest

from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.evolution import EvolutionClient
from rebe_agent.pacer import Envelope, Pacer, RefusalReason, SendRefusedError
from rebe_agent.sends import PostgresSendLog, SendKind, SendRecord, fingerprint
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, MEXICO_CITY, NOON

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)

OTHER_GROUP = "120363999999999999@g.us"


def record(
    at: datetime,
    *,
    kind: SendKind = SendKind.POST,
    chat: str = GROUP,
    text: str = "hola",
) -> SendRecord:
    return SendRecord(
        sent_at=at,
        day=at.astimezone(MEXICO_CITY).date(),
        kind=kind,
        chat=chat,
        fingerprint=fingerprint(text),
    )


@pytest.fixture
async def log() -> AsyncIterator[PostgresSendLog]:
    async with PostgresSendLog.connect(DATABASE_URL) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM sends")


async def test_the_table_is_created_on_connect(log: PostgresSendLog) -> None:
    assert await log.count_on(NOON.date()) == 0
    assert await log.latest() is None


async def test_connecting_twice_does_not_fight_over_the_schema() -> None:
    async with PostgresSendLog.connect(DATABASE_URL) as second:
        assert await second.count_on(NOON.date()) >= 0


async def test_the_rolling_window_answers_with_the_rows_oldest_first(
    log: PostgresSendLog,
) -> None:
    """The pacer needs *when* the window frees up, not only that it is full."""
    await log.record(record(NOON - timedelta(minutes=90), text="viejo"))
    await log.record(record(NOON - timedelta(seconds=40), text="uno"))
    await log.record(record(NOON - timedelta(seconds=20), text="dos"))

    window = await log.since(NOON - timedelta(minutes=1))

    assert [send.fingerprint for send in window] == [fingerprint("uno"), fingerprint("dos")]


async def test_a_day_counts_both_legs_together(log: PostgresSendLog) -> None:
    await log.record(record(NOON, kind=SendKind.POST, text="uno"))
    await log.record(record(NOON, kind=SendKind.REPLY, text="dos"))
    await log.record(record(NOON + timedelta(days=1), kind=SendKind.REPLY, text="tres"))

    assert await log.count_on(NOON.date()) == 2
    assert await log.count_on((NOON + timedelta(days=1)).date()) == 1


async def test_the_latest_send_can_be_narrowed_to_a_leg_or_a_chat(
    log: PostgresSendLog,
) -> None:
    await log.record(record(NOON - timedelta(minutes=30), kind=SendKind.POST, text="post"))
    await log.record(record(NOON - timedelta(minutes=10), kind=SendKind.REPLY, text="reply"))
    await log.record(
        record(NOON - timedelta(minutes=5), kind=SendKind.REPLY, chat=OTHER_GROUP, text="otro")
    )

    latest = await log.latest()
    assert latest is not None and latest.chat == OTHER_GROUP

    last_post = await log.latest(kind=SendKind.POST)
    assert last_post is not None and last_post.fingerprint == fingerprint("post")

    last_here = await log.latest(chat=GROUP)
    assert last_here is not None and last_here.fingerprint == fingerprint("reply")


async def test_the_log_survives_a_restart(log: PostgresSendLog) -> None:
    """The crash-loop case: a fresh process must not find a fresh allowance."""
    await log.record(record(NOON, kind=SendKind.REPLY, text="ya salio"))

    async with PostgresSendLog.connect(DATABASE_URL) as after_restart:
        assert await after_restart.count_on(NOON.date()) == 1
        latest = await after_restart.latest()
        assert latest is not None
        assert latest.fingerprint == fingerprint("ya salio")
        assert latest.kind is SendKind.REPLY


async def test_the_wording_itself_is_never_stored(log: PostgresSendLog) -> None:
    """The repeat rule needs a comparison, not a second copy of the conversation."""
    await log.record(record(NOON, text="un secreto del grupo"))

    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        cursor = await conn.execute("SELECT * FROM sends")
        rows = await cursor.fetchall()

    assert rows
    assert "secreto" not in str(rows)


async def test_a_restarted_pacer_reads_the_days_counts_back_out_of_postgres(
    log: PostgresSendLog,
) -> None:
    """The whole reason the log is not a list in memory.

    A crash loop is a new process every few seconds. If each one started from
    zero, the component whose job is "never burst" would be the burst.
    """
    envelope = Envelope(sends_per_hour=1000, sends_per_day=3)
    clock = ManualClock(NOON)

    def fresh_process() -> Pacer:
        client = EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=FakeEvolution().client())
        return Pacer(
            client,
            log,
            clock,
            envelope=envelope,
            sleeper=ManualSleeper(clock),
            rng=random.Random(1),
        )

    for index in range(3):
        await fresh_process().send(SendKind.REPLY, GROUP, f"mensaje {index}")
        clock.advance(timedelta(minutes=5))

    with pytest.raises(SendRefusedError) as refused:
        await fresh_process().send(SendKind.REPLY, GROUP, "uno mas")

    assert refused.value.reason is RefusalReason.DAILY_CEILING


async def test_concurrent_sends_do_not_lose_a_row(log: PostgresSendLog) -> None:
    await asyncio.gather(
        *(log.record(record(NOON + timedelta(seconds=n), text=f"m{n}")) for n in range(25))
    )

    assert await log.count_on(NOON.date()) == 25
