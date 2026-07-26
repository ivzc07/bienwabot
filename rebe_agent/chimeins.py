"""How often Rebe joins a conversation nobody invited her into, and its ceiling.

Tier two of `docs/wayfinder/reply-policy-spec.md`: when a message is clearly
about AI but was not addressed to her, she *may* chime in. Whether the message
qualifies is a judgement, and it belongs to the model gate in `rebe_agent.reply`.
Whether she is allowed to act on it is arithmetic, and it lives here.

Four rules, and they are deliberately four rather than one number:

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
4. **The near-silent band.** Section 2 of the playbook calls 02:00-06:00 local
   "replies only if directly addressed", so this tier is closed right through it.
   The pacer already spreads *everything* four to six times further apart in
   those hours; what it cannot say is that one kind of message should not be sent
   at all, because it has no idea which of its callers was invited to speak.

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
from datetime import date, datetime, time, timedelta
from typing import Protocol

from rebe_agent.clock import Clock
from rebe_agent.db import Pool, open_pool
from rebe_agent.pacer import Window

logger = logging.getLogger("rebe_agent.chimeins")

NEAR_SILENT = Window(time(2, 0), time(6, 0))
"""The hours section 2 of the playbook answers with "replies only if directly
addressed". The same band the pacer's `Envelope.night_hush` slows every send
down in, read here for the stronger rule that band also carries."""


@dataclass(frozen=True, slots=True)
class Allowance:
    """How much unprompted talking a day has room for.

    Parameters rather than constants for the same reason the pacer's `Envelope`
    is: the post-pairing ramp in section 1 of the playbook exists to move numbers
    like these, and a test that wants to exercise one rule needs the others out
    of its way.
    """

    chance: float = 0.25
    """How often an eligible message becomes a chime-in: "target ~25%"."""

    per_day: int = 3
    """The top of the playbook's 2-3 band, and a ceiling rather than a target.

    The band is what a day should *look* like, and `chance` is what usually
    produces it: a group with a handful of clearly-AI messages a day lands on two
    or three by itself. This number is the backstop for the day that has thirty.
    """

    cooldown: timedelta = timedelta(minutes=90)
    """How long after one chime-in the next is refused however the roll lands.

    Long enough that two never read as a burst, and wide enough that a day's
    worth is spread across the day rather than clustered in one hour of it.
    """

    near_silent: Window = NEAR_SILENT
    """When she volunteers nothing at all, however the other three rules land."""


@dataclass(frozen=True, slots=True)
class ChimeIn:
    """One unprompted message she volunteered."""

    at: datetime
    day: date
    """The *local* day from the agent's `Clock`. "Three a day" is about her day."""

    chat: str
    """Which group it went to. Nothing reads it - the ceiling and the cooldown are
    both about Rebe rather than about a room - but a human reading the table can
    tell what a row was, which is the same reason `posted_items` keeps a title."""


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
        allowance: Allowance | None = None,
    ) -> None:
        self._log = log
        self._clock = clock
        self._rng = rng or random.Random()
        self._allowance = allowance or Allowance()

    async def refuses(self) -> str | None:
        """Why she stays quiet, or `None` to go ahead.

        A sentence rather than a reason code, because nothing branches on it:
        every answer but `None` ends in the same log line and the same silence.

        The three rules that are *about her* are read before the draw, so that
        the line says the thing that is actually true. On a day that is already
        full, "the roll said no" would be a log entry hiding the ceiling doing
        its job.
        """
        local = self._local(self._clock.now())
        band = self._allowance.near_silent
        if band.contains(local.time()):
            return f"she is near-silent {band} local time unless somebody addresses her"

        today = await self._log.count_on(local.date())
        if today >= self._allowance.per_day:
            return f"{today} unprompted chime-ins today already"

        latest = await self._log.latest()
        if latest is not None and local - latest.at < self._allowance.cooldown:
            since = (local - latest.at).total_seconds() / 60
            return f"the last unprompted chime-in was {since:.0f}m ago, which is too close"

        if self._rng.random() >= self._allowance.chance:
            return f"the roll said no; she chimes in on about {self._allowance.chance:.0%} of these"
        return None

    async def spend(self, chat: str) -> None:
        """Count one chime-in that has already landed in `chat`."""
        now = self._local(self._clock.now())
        await self._log.record(ChimeIn(at=now, day=now.date(), chat=chat))
        logger.info("chimed in unprompted in %s", chat)

    def _local(self, moment: datetime) -> datetime:
        """`moment` in the agent's zone, which is the one the day and the band
        are both statements about."""
        return moment.astimezone(self._clock.zone)


SCHEMA = """
CREATE TABLE IF NOT EXISTS chime_ins (
    id      bigserial   PRIMARY KEY,
    said_at timestamptz NOT NULL,
    day     date        NOT NULL,
    chat    text        NOT NULL
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
