"""Every message Rebe has already sent, so the envelope survives a restart.

The pacer's ceilings - four sends a minute, three an hour, twelve a day - are
statements about a *number*, not about a process. Keeping that number in memory
would mean a crash loop hands itself a fresh allowance every few seconds, which
turns the one component whose whole job is "never burst" into a burst generator.
So each send is a row in the `rebe` database, and every ceiling is read back from
there.

One row per send, and only what the envelope needs to reason about:

- `sent_at`, for the rolling minute and hour windows and the post-to-post gap.
- `day`, the *local* day from the agent's `Clock`, because "twelve a day" is a
  statement about the group's day in Mexico City, not about a UTC day.
- `kind`, because a scheduled post and a webhook reply obey different quiet-hour
  rules while sharing every ceiling.
- `chat`, so "the first message into a quiet thread" is answerable.
- `fingerprint`, a hash of the wording rather than the wording itself: the rule
  is "never the same text twice in a row", and a hash answers that without this
  table becoming a second copy of the group's conversation.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from rebe_agent.db import Pool, open_pool


class SendKind(StrEnum):
    """Which leg of the process asked for the send.

    The value is what lands in the database, so these strings are stable.
    """

    POST = "post"
    """The news leg: a scheduled or override post. Held overnight."""

    REPLY = "reply"
    """The webhook leg: an answer to somebody in the group. May fire overnight."""

    ANNOUNCEMENT = "announcement"
    """The professional-register twin of a high-tier post, into the Announcements
    channel. Counts toward every raw ceiling and obeys the overnight hold, but is
    exempt from the post-to-post gap and invisible to the ramp clamp and the
    practical stop - it is the same story in another room, not a second story."""


def fingerprint(text: str) -> str:
    """A stable hash of the *wording*, ignoring whitespace and case.

    Two messages that differ only in spacing are the same message to a reader
    scrolling the group, so they are the same message to the repeat rule.
    """
    normalised = " ".join(text.split()).casefold()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SendRecord:
    """One message that left the process."""

    sent_at: datetime
    day: date
    kind: SendKind
    chat: str
    fingerprint: str


def stable_fraction(send: SendRecord) -> float:
    """A number in `[0, 1)` that is always the same for the same send.

    This is what makes a jittered gap measured from a send a gap rather than a
    lottery. Drawing fresh on each attempt would let a caller that retries every
    minute keep rolling until it got the shortest gap on offer, so "75 to 90
    minutes" would settle at 75 for everyone who asks twice. Reading the number
    off the send instead means every attempt gets the same answer, and it
    survives a restart because the fingerprint it comes from is in the database.

    Two callers want that: the pacer, spacing one post from the last one, and the
    override path, waiting out a conversation it keeps re-checking. One
    implementation, living next to the fingerprint it reads.
    """
    return int(send.fingerprint[:8], 16) / 0x1_0000_0000


class SendLog(Protocol):
    """Where the sends live. One implementation is Postgres; one is a list."""

    async def record(self, send: SendRecord) -> None:
        """Write one send down.

        Called *before* the message goes to Evolution, so a send that fails in
        transport still counts against the envelope. A retry storm against a
        failing endpoint is exactly what the ceilings exist to stop, and the
        playbook's answer to a rejected send is to back off, never to try again
        immediately.
        """

    async def since(self, instant: datetime) -> Sequence[SendRecord]:
        """Every send at or after `instant`, oldest first.

        The rolling windows are minutes and hours wide, so this is a handful of
        rows; handing back the rows rather than a count is what lets the pacer
        work out *when* the window frees up, not only that it is full.
        """

    async def count_on(self, day: date, *, kind: SendKind | None = None) -> int:
        """Sends on that local day, both legs together unless one is named.

        Both is what the envelope's daily ceiling counts. Naming a kind is what
        the practical eight-post stop counts: that number is about how much of the
        group's day is Rebe sharing links, and somebody asking her a question is
        not her filling the day up.
        """

    async def latest(
        self, *, kind: SendKind | None = None, chat: str | None = None
    ) -> SendRecord | None:
        """The most recent send, optionally narrowed to one leg or one chat."""


class InMemorySendLog:
    """A log that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self._sends: list[SendRecord] = []

    async def record(self, send: SendRecord) -> None:
        self._sends.append(send)

    async def since(self, instant: datetime) -> Sequence[SendRecord]:
        return sorted(
            (send for send in self._sends if send.sent_at >= instant),
            key=lambda send: send.sent_at,
        )

    async def count_on(self, day: date, *, kind: SendKind | None = None) -> int:
        return sum(
            1 for send in self._sends if send.day == day and (kind is None or send.kind is kind)
        )

    async def latest(
        self, *, kind: SendKind | None = None, chat: str | None = None
    ) -> SendRecord | None:
        matching = [
            send
            for send in self._sends
            if (kind is None or send.kind is kind) and (chat is None or send.chat == chat)
        ]
        return max(matching, key=lambda send: send.sent_at, default=None)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    id          bigserial   PRIMARY KEY,
    sent_at     timestamptz NOT NULL,
    day         date        NOT NULL,
    kind        text        NOT NULL,
    chat        text        NOT NULL,
    fingerprint text        NOT NULL
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS sends_sent_at_idx ON sends (sent_at DESC)",
    "CREATE INDEX IF NOT EXISTS sends_day_idx ON sends (day)",
)

COLUMNS = "sent_at, day, kind, chat, fingerprint"


def _row_to_record(row: tuple[object, ...]) -> SendRecord:
    sent_at, day, kind, chat, text_fingerprint = row
    assert isinstance(sent_at, datetime) and isinstance(day, date)
    return SendRecord(
        sent_at=sent_at,
        day=day,
        kind=SendKind(str(kind)),
        chat=str(chat),
        fingerprint=str(text_fingerprint),
    )


class PostgresSendLog:
    """The real log: one table in the `rebe` database from the deployment spec."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str) -> AsyncIterator[PostgresSendLog]:
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

    async def record(self, send: SendRecord) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO sends ({COLUMNS}) VALUES (%s, %s, %s, %s, %s)",
                (send.sent_at, send.day, str(send.kind), send.chat, send.fingerprint),
            )

    async def since(self, instant: datetime) -> Sequence[SendRecord]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {COLUMNS} FROM sends WHERE sent_at >= %s ORDER BY sent_at",
                (instant,),
            )
            rows = await cursor.fetchall()
        return [_row_to_record(row) for row in rows]

    async def count_on(self, day: date, *, kind: SendKind | None = None) -> int:
        async with self._pool.connection() as conn:
            if kind is None:
                cursor = await conn.execute("SELECT COUNT(*) FROM sends WHERE day = %s", (day,))
            else:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM sends WHERE day = %s AND kind = %s", (day, str(kind))
                )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def latest(
        self, *, kind: SendKind | None = None, chat: str | None = None
    ) -> SendRecord | None:
        # Built from a fixed set of column names, never from caller text; the
        # values themselves stay parameters.
        filters: list[str] = []
        values: list[object] = []
        if kind is not None:
            filters.append("kind = %s")
            values.append(str(kind))
        if chat is not None:
            filters.append("chat = %s")
            values.append(chat)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""

        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {COLUMNS} FROM sends {where} ORDER BY sent_at DESC, id DESC LIMIT 1",
                tuple(values),
            )
            row = await cursor.fetchone()
        return _row_to_record(row) if row else None
