"""What every DeepSeek call cost, accumulated per day and per call type.

Section 7 of `docs/wayfinder/token-budget-spec.md` asks for two things from one
counter: real usage totals, so every estimate in that document becomes checkable
against reality within a week of launch, and a call count, which is the runaway-
loop detector the guard reads.

Both live in one row per `(day, call_type)` in the `rebe` database, so a restart
loses nothing and the day's count is a single `SUM`.

The day is the *local* day from the agent's `Clock` (America/Mexico_City), not
UTC: "2,000 calls in a day" is a statement about the group's day.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

import psycopg
from psycopg_pool import AsyncConnectionPool

Pool = AsyncConnectionPool[psycopg.AsyncConnection[tuple[Any, ...]]]
"""The pool psycopg hands back with its default (tuple) row factory."""


class CallType(StrEnum):
    """The four call shapes in section 2 of the token budget spec, plus a probe.

    The value is what lands in the database, so these strings are stable.
    """

    NEWS_SUMMARY = "news_summary"
    """A: one accepted news item becomes a `NewsPost`."""

    REPLY_GATE = "reply_gate"
    """B: reply-or-ignore verdict on one inbound group message."""

    REPLY_GENERATION = "reply_generation"
    """C: the reply Rebe actually sends."""

    RELEVANCE_GATE = "relevance_gate"
    """D: borderline news candidate, keep or drop."""

    PROBE = "probe"
    """The `--ask` smoke test. Its own row, so it never muddies the four real
    call types, but counted toward the day's ceiling like everything else - a
    loop of probes is still a loop."""


@dataclass(frozen=True, slots=True)
class CallUsage:
    """The three numbers DeepSeek reports for one call.

    Named after the fields in DeepSeek's `usage` block: `prompt_cache_hit_tokens`
    and `prompt_cache_miss_tokens` are billed 50x apart, so they are never summed
    into a single "input" figure here.
    """

    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True, slots=True)
class DayTotals:
    """One `(day, call_type)` row."""

    calls: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0

    def plus_call(self) -> DayTotals:
        return DayTotals(
            calls=self.calls + 1,
            cache_hit_tokens=self.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens,
            completion_tokens=self.completion_tokens,
        )

    def plus_usage(self, usage: CallUsage) -> DayTotals:
        return DayTotals(
            calls=self.calls,
            cache_hit_tokens=self.cache_hit_tokens + usage.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens + usage.cache_miss_tokens,
            completion_tokens=self.completion_tokens + usage.completion_tokens,
        )


class UsageStore(Protocol):
    """Where the counters live. One implementation is Postgres; one is a dict."""

    async def record_call(self, day: date, call_type: CallType) -> int:
        """Count one call, and answer how many calls that day now stands at.

        Counting happens *before* the request goes out, so a call that fails,
        times out, or is retried in a loop still shows up - a retry storm against
        a failing endpoint is exactly what the ceiling exists to stop.
        """

    async def record_usage(self, day: date, call_type: CallType, usage: CallUsage) -> None:
        """Add one response's reported tokens to that day's totals."""

    async def calls_on(self, day: date) -> int:
        """Calls made that day, across every call type."""

    async def totals_on(self, day: date) -> Mapping[CallType, DayTotals]:
        """That day's rows, keyed by call type. Absent call types are absent."""


class InMemoryUsageStore:
    """A store that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self._rows: dict[tuple[date, CallType], DayTotals] = {}

    async def record_call(self, day: date, call_type: CallType) -> int:
        key = (day, call_type)
        self._rows[key] = self._rows.get(key, DayTotals()).plus_call()
        return await self.calls_on(day)

    async def record_usage(self, day: date, call_type: CallType, usage: CallUsage) -> None:
        key = (day, call_type)
        self._rows[key] = self._rows.get(key, DayTotals()).plus_usage(usage)

    async def calls_on(self, day: date) -> int:
        return sum(row.calls for (row_day, _), row in self._rows.items() if row_day == day)

    async def totals_on(self, day: date) -> Mapping[CallType, DayTotals]:
        return {
            call_type: row for (row_day, call_type), row in self._rows.items() if row_day == day
        }

    def seed(self, day: date, call_type: CallType, totals: DayTotals) -> None:
        """Place a day's counters directly, so a test can stand at 1,999 calls."""
        self._rows[(day, call_type)] = totals


SCHEMA = """
CREATE TABLE IF NOT EXISTS deepseek_usage (
    day               date   NOT NULL,
    call_type         text   NOT NULL,
    calls             bigint NOT NULL DEFAULT 0,
    cache_hit_tokens  bigint NOT NULL DEFAULT 0,
    cache_miss_tokens bigint NOT NULL DEFAULT 0,
    completion_tokens bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (day, call_type)
)
"""

CALLS_ON_DAY = "SELECT COALESCE(SUM(calls), 0) FROM deepseek_usage WHERE day = %s"
"""The day's total across every call type - what the ceiling is measured against."""


class PostgresUsageStore:
    """The real store: one table in the `rebe` database from the deployment spec.

    Every write is a single upsert, so concurrent legs cannot lose a count, and
    the row is created on first use rather than by a migration step.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str) -> AsyncIterator[PostgresUsageStore]:
        """Open a small pool, make sure the table exists, and hand back a store.

        The pool is deliberately tiny: this is a few writes a minute from one
        replica, and it shares Postgres with Evolution.
        """
        async with AsyncConnectionPool(
            database_url, min_size=1, max_size=2, open=False
        ) as pool:  # pragma: no branch
            store = cls(pool)
            await store.ensure_schema()
            yield store

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def record_call(self, day: date, call_type: CallType) -> int:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO deepseek_usage (day, call_type, calls) VALUES (%s, %s, 1)
                ON CONFLICT (day, call_type)
                DO UPDATE SET calls = deepseek_usage.calls + 1
                """,
                (day, str(call_type)),
            )
            cursor = await conn.execute(CALLS_ON_DAY, (day,))
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def record_usage(self, day: date, call_type: CallType, usage: CallUsage) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO deepseek_usage
                    (day, call_type, cache_hit_tokens, cache_miss_tokens, completion_tokens)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (day, call_type) DO UPDATE SET
                    cache_hit_tokens  = deepseek_usage.cache_hit_tokens + EXCLUDED.cache_hit_tokens,
                    cache_miss_tokens = deepseek_usage.cache_miss_tokens
                                        + EXCLUDED.cache_miss_tokens,
                    completion_tokens = deepseek_usage.completion_tokens
                                        + EXCLUDED.completion_tokens
                """,
                (
                    day,
                    str(call_type),
                    usage.cache_hit_tokens,
                    usage.cache_miss_tokens,
                    usage.completion_tokens,
                ),
            )

    async def calls_on(self, day: date) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(CALLS_ON_DAY, (day,))
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def totals_on(self, day: date) -> Mapping[CallType, DayTotals]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT call_type, calls, cache_hit_tokens, cache_miss_tokens, completion_tokens
                FROM deepseek_usage WHERE day = %s
                """,
                (day,),
            )
            rows = await cursor.fetchall()
        return {
            CallType(str(row[0])): DayTotals(
                calls=int(row[1]),
                cache_hit_tokens=int(row[2]),
                cache_miss_tokens=int(row[3]),
                completion_tokens=int(row[4]),
            )
            for row in rows
        }
