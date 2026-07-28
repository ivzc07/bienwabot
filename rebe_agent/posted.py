"""Every item Rebe has already posted, so nothing is ever posted twice.

A repost is the most visible bot tell there is - a human who shared a link
yesterday remembers doing it - so this store is load-bearing rather than a
nice-to-have, and it lives in the `rebe` database for the same reason the send
log does: a crash loop that forgot what it had posted would repost the same
launch every restart.

One row per posted item, carrying the three keys from `rebe_agent.items` and
nothing that would make this table a copy of the news itself. A candidate is
dropped if *any* of the three matches, and the title is kept alongside them
purely so a human reading the table can tell what a row was.

The wording Rebe actually sent is kept too, and that one is load-bearing rather
than decorative: the same prompt at the same temperature drifts to the same
opener, and "same sentence structure every post" is a bot tell the persona spec
names outright. `recent` reads the last few back so the news leg can show her
what she has already written. It is memory, not a rule - nothing here forbids a
word, it only lets the model see itself.

Written only after a send succeeds. That order costs the occasional re-fetch of
an item whose send failed, and buys back the guarantee that a transport blip
never permanently burns an item nobody ever saw.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from rebe_agent.db import Pool, open_pool
from rebe_agent.items import NewsItem, SeenItems


@dataclass(frozen=True, slots=True)
class PostedItem:
    """One item that made it into the group."""

    posted_at: datetime
    source: str
    source_id: str
    canonical_url: str
    title_hash: str
    title: str
    text: str = ""
    """Her words, without the link - the address is already in `canonical_url`,
    and a model that is shown a link is a model that can write one. Empty for a
    row written before the column existed."""

    @classmethod
    def of(cls, item: NewsItem, at: datetime, text: str = "") -> PostedItem:
        """Freeze a candidate's three keys at the moment it was posted."""
        return cls(
            posted_at=at,
            source=item.source,
            source_id=item.source_id,
            canonical_url=item.canonical_url,
            title_hash=item.title_hash,
            title=item.title,
            text=text,
        )


class PostedStore(Protocol):
    """Where the posted items live. One implementation is Postgres; one is a list."""

    async def knows(self, item: NewsItem) -> bool:
        """Has this item, or this article, or this story already gone out?

        True if the stable source ID, the canonical URL *or* the title hash
        matches something posted before. Three layers, any one of which is enough
        to drop the candidate.
        """

    async def remember(self, item: NewsItem, at: datetime, text: str = "") -> None:
        """Write the item down. Called only after the send succeeded."""

    async def recent(self, limit: int) -> list[str]:
        """The wording of the last `limit` posts, newest first.

        Only what she wrote: rows from before the column existed have nothing to
        show and are left out rather than handed back as blanks.
        """


class InMemoryPostedStore:
    """A store that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self.items: list[PostedItem] = []
        self._seen = SeenItems()

    async def knows(self, item: NewsItem) -> bool:
        return self._seen.knows(item)

    async def remember(self, item: NewsItem, at: datetime, text: str = "") -> None:
        self.items.append(PostedItem.of(item, at, text))
        self._seen.remember(item)

    async def recent(self, limit: int) -> list[str]:
        written = [posted.text for posted in self.items if posted.text]
        return written[::-1][:limit]


SCHEMA = """
CREATE TABLE IF NOT EXISTS posted_items (
    id            bigserial   PRIMARY KEY,
    posted_at     timestamptz NOT NULL,
    source        text        NOT NULL,
    source_id     text        NOT NULL,
    canonical_url text        NOT NULL,
    title_hash    text        NOT NULL,
    title         text        NOT NULL
)
"""

MIGRATIONS = (
    # The table predates the wording column, and there are live rows in it. A
    # default of empty is what `recent` reads as "this row was written before we
    # kept the words", which is true and is not the same as "she wrote nothing".
    "ALTER TABLE posted_items ADD COLUMN IF NOT EXISTS text text NOT NULL DEFAULT ''",
)

INDEXES = (
    # Unique, so the same source item cannot end up as two rows. It is not what
    # stops a double post - a single replica sending through one pacer is - but
    # it keeps this table honest if a run is ever started twice by hand.
    "CREATE UNIQUE INDEX IF NOT EXISTS posted_items_source_idx ON posted_items (source, source_id)",
    # Not unique: two sources legitimately produce one row each here before the
    # first of them is ever posted, and the read below is what does the dropping.
    "CREATE INDEX IF NOT EXISTS posted_items_url_idx ON posted_items (canonical_url)",
    "CREATE INDEX IF NOT EXISTS posted_items_title_idx ON posted_items (title_hash)",
)

KNOWS = """
SELECT 1 FROM posted_items
WHERE (source = %s AND source_id = %s) OR canonical_url = %s OR title_hash = %s
LIMIT 1
"""

RECENT = """
SELECT text FROM posted_items
WHERE text <> ''
ORDER BY posted_at DESC, id DESC
LIMIT %s
"""


class PostgresPostedStore:
    """The real store: one table in the `rebe` database from the deployment spec."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str) -> AsyncIterator[PostgresPostedStore]:
        """Open a small pool, make sure the table exists, and hand back a store."""
        async with open_pool(database_url) as pool:
            store = cls(pool)
            await store.ensure_schema()
            yield store

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
            for migration in MIGRATIONS:
                await conn.execute(migration)
            for index in INDEXES:
                await conn.execute(index)

    async def knows(self, item: NewsItem) -> bool:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                KNOWS, (item.source, item.source_id, item.canonical_url, item.title_hash)
            )
            row = await cursor.fetchone()
        return row is not None

    async def remember(self, item: NewsItem, at: datetime, text: str = "") -> None:
        posted = PostedItem.of(item, at, text)
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO posted_items
                    (posted_at, source, source_id, canonical_url, title_hash, title, text)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, source_id) DO NOTHING
                """,
                (
                    posted.posted_at,
                    posted.source,
                    posted.source_id,
                    posted.canonical_url,
                    posted.title_hash,
                    posted.title,
                    posted.text,
                ),
            )

    async def recent(self, limit: int) -> list[str]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(RECENT, (limit,))
            rows = await cursor.fetchall()
        return [str(row[0]) for row in rows]
