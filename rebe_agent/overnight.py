"""What broke while Rebe was asleep, kept until the morning window opens.

Section 6 of `docs/wayfinder/posting-cadence-spec.md`: important news breaking
between 23:00 and 08:00 is queued, not posted. A person who reliably posts AI
links at 03:00 is not a person, and one such post is enough to give the game away
to anybody scrolling back through the group history. The item being a few hours
old by morning is something nobody in a Mexican WhatsApp group will notice.

**The queue is in the database for the same reason the send log is.** A restart
at 04:00 is normal - the platform redeploys, the container falls over, the host
reboots - and a queue that lived in memory would drop exactly the story the whole
rule exists to save. So one row per queued item, and the morning reads its work
back from there.

**A row is the whole candidate, not a pointer to one.** The item is written down
in full rather than re-fetched at dawn, because a feed that has rolled the story
off its front page by 08:00 would otherwise take the queue's contents with it.

**No score column.** Which queued item is strongest is decided in the morning by
the ranker, on the morning's clock: a score frozen at 23:40 and one frozen at
04:10 are not comparable, since the recency term has moved under them. What is
stored is what the ranker needs to answer the question later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Protocol

from rebe_agent.db import Pool, open_pool
from rebe_agent.items import NewsItem


class OvernightQueue(Protocol):
    """Where the night's high-tier items wait. One is Postgres; one is a list."""

    async def queue(self, item: NewsItem, at: datetime) -> None:
        """Hold one item for the morning. The same item twice stays one item."""

    async def waiting(self) -> Sequence[NewsItem]:
        """Everything still held, oldest first. Which one wins is not decided here."""

    async def clear(self) -> None:
        """Empty the queue.

        Called once the morning slot has been taken, and it empties *everything*:
        section 6 gives the morning to the strongest item alone, and the rest fall
        back to normal tier - which is what happens to a high-tier item nobody is
        holding any more. They are still in the pool, so they compete for the
        later windows on the ranker's terms like every other candidate.
        """


class InMemoryOvernightQueue:
    """A queue that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self._items: list[NewsItem] = []

    async def queue(self, item: NewsItem, at: datetime) -> None:
        if any(held.source_key == item.source_key for held in self._items):
            return
        self._items.append(item)

    async def waiting(self) -> Sequence[NewsItem]:
        return list(self._items)

    async def clear(self) -> None:
        self._items.clear()


SCHEMA = """
CREATE TABLE IF NOT EXISTS overnight_items (
    id           bigserial   PRIMARY KEY,
    queued_at    timestamptz NOT NULL,
    source       text        NOT NULL,
    source_id    text        NOT NULL,
    title        text        NOT NULL,
    url          text        NOT NULL,
    published_at timestamptz NOT NULL,
    authority    double precision NOT NULL,
    points       integer,
    comments     integer,
    summary      text        NOT NULL
)
"""

INDEXES = (
    # Unique, and load-bearing: the watch looks at the pool every twenty to forty
    # minutes all night, so the same story is offered to the queue a dozen times
    # before dawn and must stay one row.
    "CREATE UNIQUE INDEX IF NOT EXISTS overnight_items_source_idx "
    "ON overnight_items (source, source_id)",
)

COLUMNS = (
    "queued_at, source, source_id, title, url, published_at, authority, points, comments, summary"
)

ITEM_COLUMNS = "source, source_id, title, url, published_at, authority, points, comments, summary"
"""Everything a `NewsItem` is made of. `queued_at` is the queue's, not the item's."""


class PostgresOvernightQueue:
    """The real queue: one table in the `rebe` database from the deployment spec."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str) -> AsyncIterator[PostgresOvernightQueue]:
        """Open a small pool, make sure the table exists, and hand back a queue."""
        async with open_pool(database_url) as pool:
            queue = cls(pool)
            await queue.ensure_schema()
            yield queue

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
            for index in INDEXES:
                await conn.execute(index)

    async def queue(self, item: NewsItem, at: datetime) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO overnight_items ({COLUMNS}) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (source, source_id) DO NOTHING",
                (
                    at,
                    item.source,
                    item.source_id,
                    item.title,
                    item.url,
                    item.published_at,
                    item.authority,
                    item.points,
                    item.comments,
                    item.summary,
                ),
            )

    async def waiting(self) -> Sequence[NewsItem]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {ITEM_COLUMNS} FROM overnight_items ORDER BY queued_at, id"
            )
            rows = await cursor.fetchall()
        return [_row_to_item(row) for row in rows]

    async def clear(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM overnight_items")


def _row_to_item(row: tuple[object, ...]) -> NewsItem:
    source, source_id, title, url, published_at, authority, points, comments, summary = row
    assert isinstance(published_at, datetime) and isinstance(authority, float)
    assert points is None or isinstance(points, int)
    assert comments is None or isinstance(comments, int)
    return NewsItem(
        source=str(source),
        source_id=str(source_id),
        title=str(title),
        url=str(url),
        published_at=published_at,
        authority=authority,
        points=points,
        comments=comments,
        summary=str(summary),
    )
