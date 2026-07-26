"""The soft pause: the switch itself, and the silence it puts on the pacer."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.evolution import EvolutionClient, EvolutionError
from rebe_agent.pacer import Envelope, Pacer, RefusalReason, SendRefusedError
from rebe_agent.pause import InMemoryPauseSwitch, NeverPaused
from rebe_agent.sends import InMemorySendLog, SendKind
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, NOON

MESSAGE = "Nuevo modelo de OpenAI, ahora corre local. Se ve interesante."

ROOMY = Envelope(sends_per_hour=1000, sends_per_day=1000, post_gap=(timedelta(0), timedelta(0)))
"""The ceilings lifted, so the only thing that can refuse a send here is the pause."""


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
def switch(clock: ManualClock) -> InMemoryPauseSwitch:
    return InMemoryPauseSwitch(clock)


@pytest.fixture
def evolution() -> FakeEvolution:
    return FakeEvolution()


@pytest.fixture
def log() -> InMemorySendLog:
    return InMemorySendLog()


@pytest.fixture
def pacer(
    evolution: FakeEvolution,
    log: InMemorySendLog,
    clock: ManualClock,
    switch: InMemoryPauseSwitch,
) -> Pacer:
    return Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        log,
        clock,
        envelope=ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(20260725),
        pause=switch,
    )


# --- The switch --------------------------------------------------------------


async def test_a_fresh_deployment_is_not_paused(switch: InMemoryPauseSwitch) -> None:
    assert (await switch.state()).paused is False


async def test_flipping_it_records_when_and_why(
    switch: InMemoryPauseSwitch, clock: ManualClock
) -> None:
    """An operator coming back to a silent Rebe has to be able to see the reason."""
    state = await switch.set_paused(True, reason="post less today")

    assert state.paused is True
    assert state.since == clock.now()
    assert state.reason == "post less today"
    assert (await switch.state()).paused is True


async def test_unpausing_clears_the_reason_and_stamps_the_moment(
    switch: InMemoryPauseSwitch, clock: ManualClock
) -> None:
    await switch.set_paused(True, reason="cool it for a bit")
    clock.advance(timedelta(hours=2))

    state = await switch.set_paused(False)

    assert state.paused is False
    assert state.reason == ""
    assert state.since == clock.now()


async def test_flipping_it_twice_the_same_way_keeps_the_first_moment(
    switch: InMemoryPauseSwitch, clock: ManualClock
) -> None:
    """ "Paused since" is when Rebe went quiet, not when the switch was last poked."""
    first = await switch.set_paused(True, reason="cool it")
    clock.advance(timedelta(minutes=30))

    again = await switch.set_paused(True, reason="cool it some more")

    assert again.since == first.since
    assert again.reason == "cool it some more"


async def test_a_pacer_with_no_switch_wired_in_is_never_paused() -> None:
    """A dry run, a `--say`, or a test about the envelope has no ops channel."""
    assert (await NeverPaused().state()).paused is False


# --- The silence it puts on the pacer ----------------------------------------


@pytest.mark.parametrize("kind", list(SendKind))
async def test_a_paused_rebe_sends_nothing_of_either_kind(
    kind: SendKind, pacer: Pacer, evolution: FakeEvolution, switch: InMemoryPauseSwitch
) -> None:
    """One switch over one pacer is what makes "posts and replies alike" true."""
    await switch.set_paused(True, reason="cool it for a bit")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(kind, GROUP, MESSAGE)

    assert refused.value.reason is RefusalReason.SOFT_PAUSE
    assert evolution.calls == [], "not even a typing indicator should reach the group"


async def test_a_pause_is_not_a_transport_failure(
    pacer: Pacer, switch: InMemoryPauseSwitch
) -> None:
    """The operator asked for silence; nothing is broken and nothing is retried."""
    await switch.set_paused(True, reason="post less today")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert not isinstance(refused.value, EvolutionError)
    assert refused.value.retry_after is None, "only a human decides when this ends"
    assert "post less today" in str(refused.value)


async def test_unpausing_resumes_without_a_burst_of_held_messages(
    pacer: Pacer,
    evolution: FakeEvolution,
    log: InMemorySendLog,
    clock: ManualClock,
    switch: InMemoryPauseSwitch,
) -> None:
    """Nothing is queued while paused, so resuming is one message and not a batch."""
    await switch.set_paused(True, reason="cool it")
    for index in range(3):
        with pytest.raises(SendRefusedError):
            await pacer.send(SendKind.POST, GROUP, f"noticia {index}")

    await switch.set_paused(False)
    await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]
    assert await log.count_on(clock.now().date()) == 1


async def test_the_switch_is_read_on_every_send_not_once_at_boot(
    pacer: Pacer, evolution: FakeEvolution, switch: InMemoryPauseSwitch
) -> None:
    """Flipping it has to silence a process that is already running."""
    await pacer.send(SendKind.REPLY, GROUP, "antes de la pausa")
    await switch.set_paused(True, reason="ya")

    with pytest.raises(SendRefusedError):
        await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == ["antes de la pausa"]
