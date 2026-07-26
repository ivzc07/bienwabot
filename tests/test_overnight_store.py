"""The overnight queue against a real Postgres, because 04:00 is when it matters.

Same deal as `tests/test_plan_store.py`: CI gives these a database, and locally
they skip unless `REBE_TEST_DATABASE_URL` names a throwaway one. The in-memory
queue is exercised by `tests/test_breaking.py`; what is proved here is the one
promise that only a real database can keep - a story that broke at 03:00 is still
there after the process that heard about it has been restarted.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import psycopg
import pytest

from rebe_agent.overnight import RETENTION, PostgresOvernightQueue
from tests.support import MEXICO_CITY, item

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)

NIGHT = datetime(2026, 7, 26, 3, 0, tzinfo=MEXICO_CITY)

LAUNCH = item(
    source="openai",
    source_id="openai-launch",
    title="OpenAI lanza un modelo que corre local",
    url="https://openai.com/index/local-model",
    published_at=NIGHT - timedelta(hours=1),
    summary="Corre en una laptop.",
)

TOP_STORY = item(
    source="hackernews",
    source_id="42000001",
    title="Anthropic releases Claude 5, its first model that runs offline",
    url="https://www.anthropic.com/news/claude-5",
    published_at=NIGHT - timedelta(minutes=20),
    authority=0.7,
    points=1420,
    comments=620,
)


@pytest.fixture
async def queue() -> AsyncIterator[PostgresOvernightQueue]:
    async with PostgresOvernightQueue.connect(DATABASE_URL) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM overnight_items")


async def test_a_quiet_night_leaves_an_empty_queue(queue: PostgresOvernightQueue) -> None:
    """Which is how the morning slot knows it belongs to the normal pool."""
    assert await queue.waiting() == []


async def test_a_queued_item_comes_back_whole(queue: PostgresOvernightQueue) -> None:
    """Every field the ranker reads, so the morning can judge it without going
    back to a feed that may have rolled the story off its front page by then."""
    await queue.queue(TOP_STORY, NIGHT)

    held = await queue.waiting()
    assert [
        (one.source, one.source_id, one.title, one.url, one.authority, one.points, one.comments)
        for one in held
    ] == [
        (
            TOP_STORY.source,
            TOP_STORY.source_id,
            TOP_STORY.title,
            TOP_STORY.url,
            TOP_STORY.authority,
            TOP_STORY.points,
            TOP_STORY.comments,
        )
    ]
    assert held[0].published_at == TOP_STORY.published_at


async def test_a_source_with_no_popularity_signal_keeps_its_nulls(
    queue: PostgresOvernightQueue,
) -> None:
    """`None` points means "this source has no popularity signal", which is not
    the same as "nobody upvoted it" - and a zero written here would say the second."""
    await queue.queue(LAUNCH, NIGHT)

    held = await queue.waiting()
    assert (held[0].points, held[0].comments) == (None, None)
    assert held[0].summary == LAUNCH.summary


async def test_the_same_story_offered_all_night_stays_one_row(
    queue: PostgresOvernightQueue,
) -> None:
    """The watch looks at the pool every twenty to forty minutes until dawn, so
    the unique index is what stops one launch becoming a dozen rows by 08:00."""
    for minute in range(0, 120, 30):
        await queue.queue(LAUNCH, NIGHT + timedelta(minutes=minute))

    assert len(await queue.waiting()) == 1


async def test_the_queue_survives_the_process_that_filled_it(
    queue: PostgresOvernightQueue,
) -> None:
    """The whole reason this is a table. A crash at 04:00 is a normal night, and a
    queue in memory would drop exactly the story the rule exists to save."""
    await queue.queue(LAUNCH, NIGHT)
    await queue.queue(TOP_STORY, NIGHT)

    async with PostgresOvernightQueue.connect(DATABASE_URL) as after_restart:
        held = await after_restart.waiting()

    assert sorted(one.source_id for one in held) == ["42000001", "openai-launch"]


async def test_demoting_the_queue_leaves_nothing_holding_a_slot(
    queue: PostgresOvernightQueue,
) -> None:
    """The morning takes the strongest and the rest fall back to normal tier, so
    nothing is left holding a slot into a second night."""
    await queue.queue(LAUNCH, NIGHT)
    await queue.queue(TOP_STORY, NIGHT)

    await queue.demote()

    assert await queue.waiting() == []
    async with PostgresOvernightQueue.connect(DATABASE_URL) as after_restart:
        assert await after_restart.waiting() == []


async def test_a_demoted_item_is_still_remembered_as_the_nights_business(
    queue: PostgresOvernightQueue,
) -> None:
    """Marked and not deleted, and it has to survive a restart in that state: a
    demoted item deleted at 09:14 would be classified high tier by the 09:40 look
    and posted as an override, which is the opposite of falling back."""
    await queue.queue(LAUNCH, NIGHT)
    await queue.demote()

    async with PostgresOvernightQueue.connect(DATABASE_URL) as after_restart:
        held = await after_restart.held()

    assert [one.source_id for one in held] == ["openai-launch"]


async def test_rows_older_than_the_retention_window_are_swept_up(
    queue: PostgresOvernightQueue,
) -> None:
    """Nothing needs a job of its own to keep the table small: by the time a row
    is this old the curator's freshness window had dropped the item days ago."""
    await queue.queue(LAUNCH, NIGHT)

    await queue.queue(TOP_STORY, NIGHT + RETENTION + timedelta(hours=1))

    assert [one.source_id for one in await queue.held()] == ["42000001"]


async def test_the_night_comes_back_in_the_order_it_arrived(
    queue: PostgresOvernightQueue,
) -> None:
    """Which one wins is the ranker's call in the morning, not this table's; what
    it promises is a stable order rather than whatever the planner felt like."""
    await queue.queue(TOP_STORY, NIGHT + timedelta(minutes=5))
    await queue.queue(LAUNCH, NIGHT)

    assert [one.source_id for one in await queue.waiting()] == ["openai-launch", "42000001"]
