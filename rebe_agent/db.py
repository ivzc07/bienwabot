"""The `rebe` database, as a connection pool.

Section 2.3 of `docs/wayfinder/deployment-architecture-spec.md` puts every piece
of agent state in one small database on bien-evo's Postgres. The stores that live
there - the DeepSeek counters, the send log - all want the same tiny pool, so the
pool lives here rather than being re-declared by each of them.

Deliberately small: this is a few writes a minute from a single replica, sharing
a Postgres with Evolution itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

Pool = AsyncConnectionPool[psycopg.AsyncConnection[tuple[Any, ...]]]
"""The pool psycopg hands back with its default (tuple) row factory."""


MIN_CONNECTIONS = 1
MAX_CONNECTIONS = 2


@asynccontextmanager
async def open_pool(database_url: str) -> AsyncIterator[Pool]:
    """Open a pool against the `rebe` database and close it on the way out."""
    async with AsyncConnectionPool(
        database_url, min_size=MIN_CONNECTIONS, max_size=MAX_CONNECTIONS, open=False
    ) as pool:  # pragma: no branch
        yield pool
