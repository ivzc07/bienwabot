"""The group memory against a real Postgres, because "survives a restart" is the point.

Same deal as `tests/test_send_log.py`: CI gives these a database, and locally
they skip unless `REBE_TEST_DATABASE_URL` names a throwaway one.

Two properties are only true of the real store and are asserted here rather than
against the in-memory one: a redelivered webhook is refused by the database's own
unique index rather than by a set that a restart would empty, and the rolling
window comes back after the process is gone.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta

import psycopg
import pytest

from rebe_agent.memory import MEMORY_WINDOW, PostgresGroupMemory, Turn
from tests.support import GROUP, NOON
from tests.webhooks import ANA, REBE

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the Postgres tests"
)

OTHER_GROUP = "120363999999999999@g.us"


def said(text: str, *, message_id: str, minute: int = 0, chat: str = GROUP) -> Turn:
    return Turn(
        chat=chat,
        at=NOON + timedelta(minutes=minute),
        message_id=message_id,
        author=ANA,
        author_name="Ana",
        text=text,
    )


def answered(text: str, *, message_id: str, minute: int = 0) -> Turn:
    return Turn(
        chat=GROUP,
        at=NOON + timedelta(minutes=minute),
        message_id=message_id,
        author=REBE,
        author_name="Rebe",
        text=text,
        by_rebe=True,
        reply_to=ANA,
        topic="on_topic",
    )


@pytest.fixture
async def memory() -> AsyncIterator[PostgresGroupMemory]:
    async with PostgresGroupMemory.connect(DATABASE_URL) as opened:
        await _empty_the_table()
        yield opened


async def _empty_the_table() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DELETE FROM group_memory")


async def test_the_table_is_created_on_connect(memory: PostgresGroupMemory) -> None:
    assert list(await memory.recent(GROUP)) == []


async def test_connecting_twice_does_not_fight_over_the_schema() -> None:
    async with PostgresGroupMemory.connect(DATABASE_URL) as second:
        assert list(await second.recent(GROUP)) == []


async def test_a_turn_comes_back_with_every_field_it_went_in_with(
    memory: PostgresGroupMemory,
) -> None:
    turn = answered("Jaja no creo", message_id="REBE-1")

    assert await memory.remember(turn) is True

    assert list(await memory.recent(GROUP)) == [turn]


async def test_turns_come_back_oldest_first(memory: PostgresGroupMemory) -> None:
    """The model reads a conversation the way the group did."""
    await memory.remember(said("hola", message_id="A", minute=0))
    await memory.remember(answered("que tal", message_id="B", minute=1))
    await memory.remember(said("ya viste el modelo nuevo", message_id="C", minute=2))

    assert [turn.message_id for turn in await memory.recent(GROUP)] == ["A", "B", "C"]


async def test_a_redelivered_message_is_refused_by_the_database(
    memory: PostgresGroupMemory,
) -> None:
    """Evolution retries a webhook it thinks failed. The second delivery has to
    be a no-op, or the group gets the same answer twice."""
    turn = said("rebe que opinas", message_id="3EB0DUPLICATE")

    assert await memory.remember(turn) is True
    assert await memory.remember(turn) is False

    assert len(list(await memory.recent(GROUP))) == 1


async def test_the_same_id_in_a_different_group_is_a_different_message(
    memory: PostgresGroupMemory,
) -> None:
    assert await memory.remember(said("hola", message_id="X")) is True
    assert await memory.remember(said("hola", message_id="X", chat=OTHER_GROUP)) is True


async def test_a_send_evolution_never_named_is_still_remembered(
    memory: PostgresGroupMemory,
) -> None:
    """The message id is best-effort on the send path, and two unnamed replies
    are two replies - not one duplicate."""
    assert await memory.remember(answered("jaja", message_id="", minute=0)) is True
    assert await memory.remember(answered("va", message_id="", minute=1)) is True

    assert len(list(await memory.recent(GROUP))) == 2


async def test_the_window_is_rolling_and_keeps_the_newest(memory: PostgresGroupMemory) -> None:
    for minute in range(MEMORY_WINDOW + 4):
        await memory.remember(said(f"mensaje {minute}", message_id=f"M{minute}", minute=minute))

    window = list(await memory.recent(GROUP))

    assert len(window) == MEMORY_WINDOW
    assert window[-1].message_id == f"M{MEMORY_WINDOW + 3}"
    assert window[0].message_id == "M4"


async def test_another_group_does_not_leak_into_this_one(memory: PostgresGroupMemory) -> None:
    await memory.remember(said("aqui", message_id="A"))
    await memory.remember(said("alla", message_id="B", chat=OTHER_GROUP))

    assert [turn.text for turn in await memory.recent(GROUP)] == ["aqui"]
    assert [turn.text for turn in await memory.recent(OTHER_GROUP)] == ["alla"]


async def test_the_window_survives_the_process_that_wrote_it(
    memory: PostgresGroupMemory,
) -> None:
    """The acceptance criterion, literally: a restart is a new pool over the same
    rows, and the thread is still there."""
    await memory.remember(said("rebe que opinas", message_id="A"))
    await memory.remember(answered("pues esta cañon", message_id="REBE-1", minute=1))

    async with PostgresGroupMemory.connect(DATABASE_URL) as after_restart:
        window = list(await after_restart.recent(GROUP))

    assert [turn.text for turn in window] == ["rebe que opinas", "pues esta cañon"]
    assert [turn.by_rebe for turn in window] == [False, True]
