"""The posted store against a real Postgres, because "never twice" outlives a restart.

Same deal as `tests/test_send_log.py`: CI gives these a database, and locally
they skip unless `REBE_TEST_DATABASE_URL` names a throwaway one. The in-memory
store is exercised by `tests/test_news.py`; what is proved here is that the same
three layers hold when they are three columns and an index.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import psycopg
import pytest

from rebe_agent.posted import PostgresPostedStore
from tests.support import NOON, item

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)

LAUNCH = item(
    source="openai",
    source_id="openai-1",
    title="OpenAI lanza un modelo que corre local",
    url="https://openai.com/index/local-model",
)


@pytest.fixture
async def store() -> AsyncIterator[PostgresPostedStore]:
    async with PostgresPostedStore.connect(DATABASE_URL) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM posted_items")


async def test_an_unknown_item_is_unknown(store: PostgresPostedStore) -> None:
    assert not await store.knows(LAUNCH)


async def test_what_went_out_is_remembered(store: PostgresPostedStore) -> None:
    await store.remember(LAUNCH, NOON)

    assert await store.knows(LAUNCH)


async def test_the_same_article_from_another_source_is_already_known(
    store: PostgresPostedStore,
) -> None:
    """Layer 2: HN's copy of a launch the vendor's own feed already got posted."""
    await store.remember(LAUNCH, NOON)
    from_hn = item(
        source="hackernews",
        source_id="41000001",
        title="OpenAI ships a local model",
        url="https://www.openai.com/index/local-model/?utm_source=hn",
    )

    assert await store.knows(from_hn)


async def test_the_same_story_under_another_url_is_already_known(
    store: PostgresPostedStore,
) -> None:
    """Layer 3: a syndication, where only the headline survives unchanged."""
    await store.remember(LAUNCH, NOON)
    syndicated = item(
        source="venturebeat",
        source_id="vb-9",
        title="OpenAI lanza un modelo que corre local",
        url="https://venturebeat.com/2026/07/25/openai-local",
    )

    assert await store.knows(syndicated)


async def test_a_genuinely_different_item_is_still_postable(
    store: PostgresPostedStore,
) -> None:
    """The gate has to let news through, which is the failure worth catching."""
    await store.remember(LAUNCH, NOON)
    other = item(
        source="deepmind",
        source_id="dm-4",
        title="DeepMind muestra un agente que juega ajedrez",
        url="https://deepmind.google/blog/ajedrez",
    )

    assert not await store.knows(other)


async def test_remembering_the_same_item_twice_is_not_an_error(
    store: PostgresPostedStore,
) -> None:
    """A retry after a partial failure must not take the process down with a
    unique-violation, and must not leave two rows behind either."""
    await store.remember(LAUNCH, NOON)
    await store.remember(LAUNCH, NOON)

    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM posted_items")
        row = await cursor.fetchone()
    assert row is not None and row[0] == 1


async def test_the_table_survives_the_store_being_rebuilt(
    store: PostgresPostedStore,
) -> None:
    """The whole reason this is not a set in memory."""
    await store.remember(LAUNCH, NOON)

    async with PostgresPostedStore.connect(DATABASE_URL) as after_restart:
        assert await after_restart.knows(LAUNCH)
