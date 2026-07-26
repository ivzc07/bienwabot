"""What broke while Rebe was asleep, kept until the morning window opens.

Section 6 of `docs/wayfinder/posting-cadence-spec.md`: important news breaking
between 23:00 and 08:00 is queued, not posted. A person who reliably posts AI
links at 03:00 is not a person, and one such post is enough to give the game away
to anybody scrolling back through the group history. The item being a few hours
old by morning is something nobody in a Mexican WhatsApp group will notice.

**The queue is in the database for the same reason the send log is.** A restart
at 04:00 is normal - the platform redeploys, the container falls over, the host
reboots - and a queue that lived in memory would drop exactly the story the whole
rule exists to save.

**A row is the whole candidate, not a pointer to one.** The item is written down
in full rather than re-fetched at dawn, because a feed that has rolled the story
off its front page by 08:00 would otherwise take the queue's contents with it.

**No score column.** Which queued item is strongest is decided in the morning by
the ranker, on the morning's clock: a score frozen at 23:40 and one frozen at
04:10 are not comparable, since the recency term has moved under them. What is
stored is what the ranker needs to answer the question later.

**A row outlives the night that wrote it, and that is the point.** The morning
gives the slot to the strongest item and *demotes* the rest, which section 6 says
plainly: they fall back to normal tier and compete for the later windows. A queue
that deleted them would have them classified high tier again by the next look and
posted as overrides - which is the opposite of falling back. So nothing is
deleted at the morning; the rows stay, marked, and `held` is what the override
leg reads to know the night already took charge of an item. They are cleared out
after `RETENTION`, by which time the curator's own freshness window has long since
dropped the items anyway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from rebe_agent.db import Pool, open_pool
from rebe_agent.items import NewsItem

RETENTION = timedelta(days=3)
"""How long a settled row is kept before it is swept up.

Comfortably longer than the curator's 36-hour freshness window, so a row is only
ever deleted after the item on it had stopped being postable regardless.
"""


class Holding(StrEnum):
    """What the night decided about one item. The value is what lands in the table."""

    WAITING = "waiting"
    """Still holding the morning slot."""

    DEMOTED = "demoted"
    """The morning came and went to something else. Normal tier from here."""


@dataclass(frozen=True, slots=True)
class Held:
    """One item the night took charge of, when, and what became of it."""

    item: NewsItem
    queued_at: datetime
    holding: Holding


class OvernightQueue(Protocol):
    """Where the night's high-tier items wait. One is Postgres; one is a list."""

    async def queue(self, item: NewsItem, at: datetime) -> None:
        """Hold one item for the morning. The same item twice stays one item."""

    async def waiting(self) -> Sequence[NewsItem]:
        """The items still holding the morning slot, oldest first.

        Which one wins is not decided here: the morning ranks them on its own
        clock, because a score frozen overnight is a score about a different hour.
        """

    async def held(self) -> Sequence[NewsItem]:
        """Everything the night took charge of, waiting or demoted alike.

        What the override leg reads to know an item is not breaking news any
        more - either because the morning slot is already reserved for it, or
        because the morning went to something stronger and this one fell back to
        normal tier.
        """

    async def demote(self) -> None:
        """The morning is over: nothing is holding a slot any longer.

        Marks rather than deletes, so the items that lost stay recognisable as
        news the night already accounted for instead of being read as breaking
        all over again by the next look.
        """


class InMemoryOvernightQueue:
    """A queue that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self._held: list[Held] = []

    async def queue(self, item: NewsItem, at: datetime) -> None:
        self._held = [one for one in self._held if one.queued_at >= at - RETENTION]
        if any(one.item.source_key == item.source_key for one in self._held):
            return
        self._held.append(Held(item=item, queued_at=at, holding=Holding.WAITING))

    async def waiting(self) -> Sequence[NewsItem]:
        return [one.item for one in self._held if one.holding is Holding.WAITING]

    async def held(self) -> Sequence[NewsItem]:
        return [one.item for one in self._held]

    async def demote(self) -> None:
        self._held = [
            Held(item=one.item, queued_at=one.queued_at, holding=Holding.DEMOTED)
            for one in self._held
        ]


def source_keys(items: Iterable[NewsItem]) -> set[tuple[str, str]]:
    """The stable source IDs of a handful of items, for an `in` test.

    Only layer one of the three dedup keys, and deliberately: this answers "is
    this the row the night wrote", not "is this the same story". The other two
    layers belong to the posted store, which is what decides whether the group has
    already seen a story.
    """
    return {item.source_key for item in items}


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
    summary      text        NOT NULL,
    holding      text        NOT NULL DEFAULT 'waiting'
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
"""What one insert writes. `holding` is left to its default, which is the point
of queueing something in the first place."""

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
            # Swept here rather than on a timer: the queue is only ever written to
            # at night, so this is the one moment the table is known to be in use
            # and nothing needs a job of its own to keep it small.
            await conn.execute(
                "DELETE FROM overnight_items WHERE queued_at < %s", (at - RETENTION,)
            )
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
        return await self._read("WHERE holding = %s", (str(Holding.WAITING),))

    async def held(self) -> Sequence[NewsItem]:
        return await self._read("", ())

    async def demote(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE overnight_items SET holding = %s WHERE holding = %s",
                (str(Holding.DEMOTED), str(Holding.WAITING)),
            )

    async def _read(self, where: str, values: tuple[object, ...]) -> list[NewsItem]:
        """Rows in the order they arrived. `where` is written here, never by a caller."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {ITEM_COLUMNS} FROM overnight_items {where} ORDER BY queued_at, id", values
            )
            rows = await cursor.fetchall()
        return [_row_to_item(row) for row in rows]


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
