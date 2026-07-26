"""The day's drawn slots, written down as they are drawn.

A plan that lived only in memory would be a plan the process loses every time the
platform restarts it - and a restart at 13:00 would either lose the afternoon or
redraw it, which is worse: a second roll would put fresh times on a day that
already spent some of its gaps. So the roll writes one row per slot into the
`rebe` database from section 2.3 of the deployment spec, and the loop reads its
work back from there.

The three promises this store makes, all of them for the restart case:

- **A day is rolled once.** `register` inserts on a unique `(day, window)` and
  hands back the plan of record, so a second roll on the same day cannot
  double-register it or move a time that was already committed to.
- **What happened is remembered.** A slot leaves `planned` exactly once - posted,
  skipped for want of anything worth posting, or dropped - so a restart never
  reposts a slot that already went out.
- **The times come back in the agent's zone.** A `timestamptz` returns in the
  session's zone, and every window edge, log line and quiet-hour decision
  downstream is a statement about Mexico City.

The state lives in a column rather than in the `Slot` the scheduler holds, so the
one place that can disagree with the database is not held in a variable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, tzinfo
from typing import Protocol

from rebe_agent.cadence import DayPlan, Slot, SlotState
from rebe_agent.db import Pool, open_pool


class PlanStore(Protocol):
    """Where the day's plan lives. One implementation is Postgres; one is a dict."""

    async def register(self, plan: DayPlan) -> DayPlan:
        """Write a freshly drawn plan down and hand back the plan of record.

        A day that already has one keeps it, times and all. The returned plan is
        what the loop must work from, which is not always what was passed in.
        """

    async def plan_on(self, day: date) -> DayPlan | None:
        """The day's plan, or `None` if that day was never rolled."""

    async def settle(self, day: date, window: str, state: SlotState) -> None:
        """Record what became of one slot. A slot that is not there is ignored."""


class InMemoryPlanStore:
    """A store that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self._days: dict[date, list[Slot]] = {}

    async def register(self, plan: DayPlan) -> DayPlan:
        stored = self._days.setdefault(plan.day, list(plan.slots))
        return DayPlan(day=plan.day, slots=tuple(stored))

    async def plan_on(self, day: date) -> DayPlan | None:
        slots = self._days.get(day)
        if slots is None:
            return None
        return DayPlan(day=day, slots=tuple(slots))

    async def settle(self, day: date, window: str, state: SlotState) -> None:
        slots = self._days.get(day, [])
        for index, slot in enumerate(slots):
            if slot.window == window:
                slots[index] = Slot(window=slot.window, at=slot.at, closes=slot.closes, state=state)


SCHEMA = """
CREATE TABLE IF NOT EXISTS planned_slots (
    id          bigserial   PRIMARY KEY,
    day         date        NOT NULL,
    window_name text        NOT NULL,
    due_at      timestamptz NOT NULL,
    closes_at   timestamptz NOT NULL,
    state       text        NOT NULL
)
"""

INDEXES = (
    # Unique, and load-bearing rather than tidy: it is what makes a second roll of
    # the same day a no-op instead of a second set of times.
    "CREATE UNIQUE INDEX IF NOT EXISTS planned_slots_day_idx ON planned_slots (day, window_name)",
)

COLUMNS = "day, window_name, due_at, closes_at, state"


class PostgresPlanStore:
    """The real store: one table in the `rebe` database from the deployment spec."""

    def __init__(self, pool: Pool, zone: tzinfo) -> None:
        self._pool = pool
        self._zone = zone

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str, zone: tzinfo) -> AsyncIterator[PostgresPlanStore]:
        """Open a small pool, make sure the table exists, and hand back a store."""
        async with open_pool(database_url) as pool:
            store = cls(pool, zone)
            await store.ensure_schema()
            yield store

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
            for index in INDEXES:
                await conn.execute(index)

    async def register(self, plan: DayPlan) -> DayPlan:
        async with self._pool.connection() as conn:
            for slot in plan.slots:
                await conn.execute(
                    f"INSERT INTO planned_slots ({COLUMNS}) VALUES (%s, %s, %s, %s, %s) "
                    f"ON CONFLICT (day, window_name) DO NOTHING",
                    (plan.day, slot.window, slot.at, slot.closes, str(slot.state)),
                )
        return await self.plan_on(plan.day) or plan

    async def plan_on(self, day: date) -> DayPlan | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT window_name, due_at, closes_at, state FROM planned_slots "
                "WHERE day = %s ORDER BY due_at",
                (day,),
            )
            rows = await cursor.fetchall()
        if not rows:
            return None
        return DayPlan(day=day, slots=tuple(self._row_to_slot(row) for row in rows))

    async def settle(self, day: date, window: str, state: SlotState) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE planned_slots SET state = %s WHERE day = %s AND window_name = %s",
                (str(state), day, window),
            )

    def _row_to_slot(self, row: tuple[object, ...]) -> Slot:
        window, due_at, closes_at, state = row
        assert isinstance(due_at, datetime) and isinstance(closes_at, datetime)
        return Slot(
            window=str(window),
            at=due_at.astimezone(self._zone),
            closes=closes_at.astimezone(self._zone),
            state=SlotState(str(state)),
        )
