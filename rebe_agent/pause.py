"""The out-of-band soft pause: the everyday control, and the only one there is.

The consent spec's admin controls are two, and neither is a message in the group.
The hard stop is an admin removing Rebe's number, which looks like a person
leaving and needs no code. This is the other one: the operator flips a switch
from the ops channel and Rebe goes silent while staying in the group, which
covers "post less today" and "cool it for a bit".

Three properties make it a switch rather than a flag:

- **It gates the shared pacer**, so it stops posts and replies alike. There is
  one place every message leaves through, and this is read there.
- **It survives a restart**, because a pause that a redeploy silently undoes is
  worse than no pause: the operator has no reason to check again.
- **Nothing is held while it is on.** A paused send is refused, not queued, so
  unpausing resumes normal behaviour instead of firing a backlog at the group -
  which would be the exact burst the whole envelope exists to prevent.

`since` is when Rebe went quiet, not when the switch was last poked, so an
operator coming back to a silent group can see how long it has been silent.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from rebe_agent.clock import Clock
from rebe_agent.db import Pool, open_pool

logger = logging.getLogger("rebe_agent.pause")


@dataclass(frozen=True, slots=True)
class PauseState:
    """Where the switch stands, and since when."""

    paused: bool
    since: datetime | None = None
    reason: str = ""


NOT_PAUSED = PauseState(paused=False)
"""Where the switch stands before anybody has ever flipped it."""


class Pause(Protocol):
    """What a sender needs from the switch: whether Rebe is meant to be silent.

    The whole state rather than a bare boolean, because the refusal a caller sees
    should say *why* it is silent - an operator reading a log two hours later has
    only that line to go on.
    """

    async def state(self) -> PauseState:
        """Where the switch stands. Read on every send."""


class PauseSwitch(Pause, Protocol):
    """The whole switch: read it, and flip it."""

    async def set_paused(self, paused: bool, *, reason: str = "") -> PauseState:
        """Flip it, and answer with the state that is now in force.

        Flipping it the way it already points keeps the original `since` - the
        moment Rebe went quiet - and takes the new reason.
        """


class NeverPaused:
    """The switch a pacer gets when nobody wired one in.

    A `--say` from the command line, a dry run, or a test about the envelope has
    no ops channel behind it. Read-only on purpose: something that answers "flip
    me" by doing nothing would be a switch that lies.
    """

    async def state(self) -> PauseState:
        return NOT_PAUSED


class InMemoryPauseSwitch:
    """A switch that forgets on restart. For tests and for local dry runs."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._state = NOT_PAUSED

    async def state(self) -> PauseState:
        return self._state

    async def set_paused(self, paused: bool, *, reason: str = "") -> PauseState:
        since = self._state.since if self._state.paused == paused else None
        self._state = PauseState(
            paused=paused,
            since=since or self._clock.now(),
            reason=reason if paused else "",
        )
        return self._state


SCHEMA = """
CREATE TABLE IF NOT EXISTS soft_pause (
    id     smallint    PRIMARY KEY,
    paused boolean     NOT NULL,
    since  timestamptz NOT NULL,
    reason text        NOT NULL DEFAULT ''
)
"""

ONLY_ROW = 1
"""There is one switch. Its row is pinned to one id so there cannot be two."""

COLUMNS = "paused, since, reason"


class PostgresPauseSwitch:
    """The real switch: one row in the `rebe` database from the deployment spec.

    One row, because a pause is a property of the deployment rather than of a
    process - which is the whole point of putting it here instead of in memory.
    """

    def __init__(self, pool: Pool, clock: Clock) -> None:
        self._pool = pool
        self._clock = clock
        self._table_is_there = False

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str, clock: Clock) -> AsyncIterator[PostgresPauseSwitch]:
        """Open a small pool, make sure the table exists, and hand back a switch."""
        async with open_pool(database_url) as pool:
            switch = cls(pool, clock)
            await switch.ensure_schema()
            yield switch

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
        self._table_is_there = True

    async def _ready(self) -> None:
        """Create the table on first use, and try again next time if that failed.

        Boot creates it too, but boot is allowed to fail: the `rebe` database can
        be a few seconds behind the container, and the ops channel comes up anyway
        so that somebody can hear about it. Without this, a switch that missed its
        one chance would stay broken until the next redeploy - taking the only
        control path with it.
        """
        if not self._table_is_there:
            await self.ensure_schema()

    async def state(self) -> PauseState:
        await self._ready()
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {COLUMNS} FROM soft_pause WHERE id = %s", (ONLY_ROW,)
            )
            row = await cursor.fetchone()
        if row is None:
            return NOT_PAUSED
        paused, since, reason = row
        return PauseState(paused=bool(paused), since=since, reason=str(reason))

    async def set_paused(self, paused: bool, *, reason: str = "") -> PauseState:
        """Write the switch down, keeping `since` if it already pointed this way."""
        await self._ready()
        now = self._clock.now()
        stored = reason if paused else ""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                INSERT INTO soft_pause (id, {COLUMNS}) VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    paused = EXCLUDED.paused,
                    reason = EXCLUDED.reason,
                    -- The moment Rebe went quiet, not the moment the switch was
                    -- last poked: only a real change of direction restamps it.
                    since  = CASE
                                 WHEN soft_pause.paused = EXCLUDED.paused THEN soft_pause.since
                                 ELSE EXCLUDED.since
                             END
                RETURNING {COLUMNS}
                """,
                (ONLY_ROW, paused, now, stored),
            )
            row = await cursor.fetchone()
        assert row is not None, "an upsert with RETURNING always answers with its row"
        state = PauseState(paused=bool(row[0]), since=row[1], reason=str(row[2]))
        logger.info(
            "the soft pause is now %s%s",
            "ON" if state.paused else "off",
            f" ({state.reason})" if state.reason else "",
        )
        return state
