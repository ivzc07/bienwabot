"""The rolling window of recent turns per group, so Rebe has a short memory.

`group_memory` is the first of the two tables in section 2.3 of
`docs/wayfinder/deployment-architecture-spec.md`. It does three jobs at once, and
all three are reasons it has to be in the `rebe` database rather than in a dict:

1. **Context.** The last few turns are fed back into the model on every event, so
   a follow-up lands as a follow-up rather than as a fresh question. A restart
   that forgot the thread would have her answering "y eso?" with no idea what
   "eso" was.
2. **Duplicate delivery.** Evolution retries a webhook it believes failed, and
   the same message arriving twice must not become two replies. The message id is
   the key, and the database's own unique index is what refuses the second one -
   a set in memory would hand a crash loop a clean slate every few seconds.
3. **Conversation shape.** "Never twice in a row to the same person" and the
   two-or-three turn fade are both statements about what has already been said,
   which is exactly what this table holds.

Every turn is written, Rebe's own included, because a window with her half
missing is not a conversation. Her turns carry `reply_to` (whom she answered) and
`topic` (what the gate made of it), which is what the two shape rules read.

The window is *read* rolling rather than *stored* rolling: rows are kept and only
the newest `MEMORY_WINDOW` come back. A few hundred short rows a month is nothing
to this database, and a thread that can be read back in full is worth more than
the disk it costs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from rebe_agent.db import Pool, open_pool

MEMORY_WINDOW = 12
"""How many recent turns come back. Section 2 of the token budget spec sizes the
reply-generation call at ~1,000 input tokens including the thread, and a dozen
WhatsApp-short lines sits inside that."""


@dataclass(frozen=True, slots=True)
class Turn:
    """One message in a group, whoever wrote it."""

    chat: str
    at: datetime
    message_id: str
    """WhatsApp's id. Empty for a send Evolution did not name, which is allowed."""

    author: str
    author_name: str
    text: str
    by_rebe: bool = False

    reply_to: str = ""
    """On one of Rebe's turns: the JID she was answering. Empty otherwise."""

    topic: str = ""
    """On one of Rebe's turns: the gate's verdict on the message she answered.

    A plain string rather than the reply leg's `Topic`, for the same reason the
    database column is one: this is a store, and a store that imported a leg
    would be a store the other leg could not use. The values are `Topic`'s own,
    which is a `StrEnum`, so a caller compares them without converting anything.
    """


class GroupMemory(Protocol):
    """Where the turns live. One implementation is Postgres; one is a list."""

    async def remember(self, turn: Turn) -> bool:
        """Write one turn down, and say whether it was new.

        `False` means this message id has already been stored for this chat -
        a redelivered webhook - and the caller must do nothing further with it.
        A turn with no message id is always new, because two unnamed sends are
        two sends.
        """

    async def recent(self, chat: str, limit: int = MEMORY_WINDOW) -> Sequence[Turn]:
        """The last `limit` turns in `chat`, oldest first."""


class InMemoryGroupMemory:
    """A memory that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self._seen: set[tuple[str, str]] = set()

    async def remember(self, turn: Turn) -> bool:
        if turn.message_id:
            key = (turn.chat, turn.message_id)
            if key in self._seen:
                return False
            self._seen.add(key)
        self.turns.append(turn)
        return True

    async def recent(self, chat: str, limit: int = MEMORY_WINDOW) -> Sequence[Turn]:
        here = [turn for turn in self.turns if turn.chat == chat]
        return here[-limit:] if limit > 0 else []


SCHEMA = """
CREATE TABLE IF NOT EXISTS group_memory (
    id          bigserial   PRIMARY KEY,
    chat        text        NOT NULL,
    said_at     timestamptz NOT NULL,
    message_id  text        NOT NULL,
    author      text        NOT NULL,
    author_name text        NOT NULL,
    body        text        NOT NULL,
    by_rebe     boolean     NOT NULL,
    reply_to    text        NOT NULL,
    topic       text        NOT NULL
)
"""

INDEXES = (
    # Partial, because an unnamed send is not a duplicate of the next unnamed
    # send: the id is best-effort on the send path, and two empty strings are
    # two different messages. This index is what refuses a redelivered webhook.
    "CREATE UNIQUE INDEX IF NOT EXISTS group_memory_delivery_idx "
    "ON group_memory (chat, message_id) WHERE message_id <> ''",
    "CREATE INDEX IF NOT EXISTS group_memory_window_idx ON group_memory (chat, said_at DESC)",
)

COLUMNS = "chat, said_at, message_id, author, author_name, body, by_rebe, reply_to, topic"


def _row_to_turn(row: tuple[object, ...]) -> Turn:
    chat, said_at, message_id, author, author_name, body, by_rebe, reply_to, topic = row
    assert isinstance(said_at, datetime)
    return Turn(
        chat=str(chat),
        at=said_at,
        message_id=str(message_id),
        author=str(author),
        author_name=str(author_name),
        text=str(body),
        by_rebe=bool(by_rebe),
        reply_to=str(reply_to),
        topic=str(topic),
    )


class PostgresGroupMemory:
    """The real memory: one table in the `rebe` database from the deployment spec."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str) -> AsyncIterator[PostgresGroupMemory]:
        """Open a small pool, make sure the table exists, and hand back a memory."""
        async with open_pool(database_url) as pool:
            memory = cls(pool)
            await memory.ensure_schema()
            yield memory

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
            for index in INDEXES:
                await conn.execute(index)

    async def remember(self, turn: Turn) -> bool:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"INSERT INTO group_memory ({COLUMNS}) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                # The target is named, and named with the index's own predicate,
                # because the index is partial: an unnamed send must not collide
                # with the next unnamed send.
                "ON CONFLICT (chat, message_id) WHERE message_id <> '' "
                "DO NOTHING RETURNING id",
                (
                    turn.chat,
                    turn.at,
                    turn.message_id,
                    turn.author,
                    turn.author_name,
                    turn.text,
                    turn.by_rebe,
                    turn.reply_to,
                    turn.topic,
                ),
            )
            written = await cursor.fetchone()
        return written is not None

    async def recent(self, chat: str, limit: int = MEMORY_WINDOW) -> Sequence[Turn]:
        if limit <= 0:
            return []
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {COLUMNS} FROM group_memory WHERE chat = %s "
                f"ORDER BY said_at DESC, id DESC LIMIT %s",
                (chat, limit),
            )
            rows = await cursor.fetchall()
        return [_row_to_turn(row) for row in reversed(rows)]
