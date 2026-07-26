"""How often Rebe joins a conversation nobody invited her into, and its ceiling.

Tier two of `docs/wayfinder/reply-policy-spec.md`: when a message is clearly
about AI but was not addressed to her, she *may* chime in. Whether the message
qualifies is a judgement, and it belongs to the model gate in `rebe_agent.reply`.
Whether she is allowed to act on it is arithmetic, and it lives here.

Three rules, and they are deliberately three rather than one number:

1. **A probability.** She answers about a quarter of the messages that qualify.
   This is what makes her read as selective; answering every eligible message is
   the "lurking" tell the policy names, and it is the one this ticket exists to
   avoid.
2. **A daily ceiling**, 2-3 in both the reply policy and section 2 of
   `docs/wayfinder/anti-ban-ops-spec.md`, and absolute: it holds whatever the
   probability rolls. On an unusually chatty AI day the roll alone would let five
   or six through, and five unprompted comments in a day is a bot.
3. **A cooldown.** Two unprompted chime-ins inside the same short window read as
   one burst, however far under the daily ceiling they are.

The count is *persisted*, and per local day, for the same reason the send log is:
a crash loop that forgot how many times she had already spoken up today would
hand itself a fresh allowance every few seconds, and "three a day" would become
three a restart. One row per chime-in in the `rebe` database, which also answers
the cooldown without a second table.

Addressed replies are not counted here at all. The reply policy is explicit that
tier one has no cap, and a shared counter would mean a busy afternoon of
name-tags quietly closing tier two for the rest of the day.
"""

from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from rebe_agent.clock import Clock
from rebe_agent.db import Pool, open_pool

logger = logging.getLogger("rebe_agent.chimeins")

CHANCE = 0.25
"""How often an eligible message becomes a chime-in. "Target ~25%" in the policy."""

CHIME_INS_PER_DAY = 3
"""The top of the playbook's 2-3 band, and a hard ceiling rather than a target.

The band is what a day should *look* like, and `CHANCE` is what usually produces
it: a group with a handful of clearly-AI messages a day lands on two or three by
itself. This number is the backstop for the day that has thirty of them.
"""

COOLDOWN = timedelta(minutes=90)
"""How long after one unprompted chime-in the next is refused outright.

Long enough that two never read as a burst, and wide enough that three in a day
are spread across it rather than clustered in one hour of it.
"""


@dataclass(frozen=True, slots=True)
class ChimeIn:
    """One unprompted message she volunteered."""

    at: datetime
    day: date
    """The *local* day from the agent's `Clock`. "Three a day" is about her day."""

    chat: str


class ChimeInLog(Protocol):
    """Where the chime-ins live. One implementation is Postgres; one is a list."""

    async def record(self, chime_in: ChimeIn) -> None:
        """Write one down. Called only after the message actually went out.

        That order costs nothing and buys the guarantee that a send the envelope
        refused, or one the transport lost, never burns a slot out of three for a
        message the group never saw.
        """

    async def count_on(self, day: date) -> int:
        """How many she has volunteered on that local day."""

    async def latest(self) -> ChimeIn | None:
        """The most recent one, whichever chat it went to, or `None`."""


class InMemoryChimeInLog:
    """A log that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self._chime_ins: list[ChimeIn] = []

    async def record(self, chime_in: ChimeIn) -> None:
        self._chime_ins.append(chime_in)

    async def count_on(self, day: date) -> int:
        return sum(1 for chime_in in self._chime_ins if chime_in.day == day)

    async def latest(self) -> ChimeIn | None:
        return max(self._chime_ins, key=lambda chime_in: chime_in.at, default=None)


class ChimeInBudget:
    """Whether an eligible message may become a chime-in right now.

    The clock and the draw are both injected, so the two rules that are otherwise
    untestable - "per day in the configured timezone" and "about a quarter" - are
    assertions rather than hopes.
    """

    def __init__(
        self,
        log: ChimeInLog,
        clock: Clock,
        *,
        rng: random.Random | None = None,
        chance: float = CHANCE,
        per_day: int = CHIME_INS_PER_DAY,
        cooldown: timedelta = COOLDOWN,
    ) -> None:
        self._log = log
        self._clock = clock
        self._rng = rng or random.Random()
        self._chance = chance
        self._per_day = per_day
        self._cooldown = cooldown

    async def refuses(self) -> str | None:
        """Why she stays quiet, or `None` to go ahead.

        The ceiling and the cooldown are read before the draw so that a refusal
        says the thing that is actually true: on a day that is already full, "the
        roll said no" would be a log line that hid the ceiling doing its job.
        """
        now = self._clock.now()
        today = await self._log.count_on(self._local_day(now))
        if today >= self._per_day:
            return f"{today} unprompted chime-ins today already"

        latest = await self._log.latest()
        if latest is not None and now - latest.at < self._cooldown:
            return (
                f"too soon after the last unprompted chime-in, "
                f"which was {_minutes(now - latest.at)} ago"
            )

        if self._rng.random() >= self._chance:
            return f"the roll said no; she chimes in on about {self._chance:.0%} of these"
        return None

    async def spend(self, chat: str) -> None:
        """Count one chime-in that has already landed in `chat`."""
        now = self._clock.now()
        await self._log.record(ChimeIn(at=now, day=self._local_day(now), chat=chat))
        logger.info("chimed in unprompted in %s", chat)

    def _local_day(self, moment: datetime) -> date:
        """The day in the agent's zone, the way the pacer counts its own."""
        return moment.astimezone(self._clock.zone).date()


def _minutes(span: timedelta) -> str:
    """A duration a human reads at a glance, for a log line."""
    total = span.total_seconds()
    return f"{total:.0f}s" if total < 90 else f"{total / 60:.0f}m"


SCHEMA = """
CREATE TABLE IF NOT EXISTS chime_ins (
    id     bigserial   PRIMARY KEY,
    said_at timestamptz NOT NULL,
    day    date        NOT NULL,
    chat   text        NOT NULL
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS chime_ins_day_idx ON chime_ins (day)",
    "CREATE INDEX IF NOT EXISTS chime_ins_said_at_idx ON chime_ins (said_at DESC)",
)

COLUMNS = "said_at, day, chat"


class PostgresChimeInLog:
    """The real log: one table in the `rebe` database from the deployment spec."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str) -> AsyncIterator[PostgresChimeInLog]:
        """Open a small pool, make sure the table exists, and hand back a log."""
        async with open_pool(database_url) as pool:
            log = cls(pool)
            await log.ensure_schema()
            yield log

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
            for index in INDEXES:
                await conn.execute(index)

    async def record(self, chime_in: ChimeIn) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO chime_ins ({COLUMNS}) VALUES (%s, %s, %s)",
                (chime_in.at, chime_in.day, chime_in.chat),
            )

    async def count_on(self, day: date) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM chime_ins WHERE day = %s", (day,))
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def latest(self) -> ChimeIn | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {COLUMNS} FROM chime_ins ORDER BY said_at DESC, id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        said_at, day, chat = row
        assert isinstance(said_at, datetime) and isinstance(day, date)
        return ChimeIn(at=said_at, day=day, chat=str(chat))
