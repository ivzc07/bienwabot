"""The heartbeat: what proves the process *and* its loop are still alive.

The beats here run on a millisecond interval rather than the shipped sixty
seconds, so the loop can be watched for a few beats inside a test. Nothing waits
on a wall-clock deadline: each test drives the loop until it has seen enough.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from rebe_agent.heartbeat import HEARTBEAT_SECONDS, Heartbeat
from tests.kuma_stub import PUSH_TOKEN, PUSH_URL, FakeKuma

FAST = 0.001
"""A beat interval short enough that a test never waits on it."""


def heartbeat_for(kuma: FakeKuma, *, interval: float = FAST) -> Heartbeat:
    return Heartbeat(PUSH_URL, kuma.client(), interval=interval)


async def beats_reach(kuma: FakeKuma, count: int) -> None:
    """Let the loop run until it has pushed `count` times."""
    while len(kuma.beats) < count:
        await asyncio.sleep(0)


async def test_a_beat_tells_kuma_the_agent_is_up() -> None:
    kuma = FakeKuma()

    await heartbeat_for(kuma).beat()

    (beat,) = kuma.beats
    assert PUSH_TOKEN in beat.path
    assert beat.status == "up"
    assert beat.message


async def test_a_kuma_that_is_unreachable_never_breaks_the_loop() -> None:
    """A monitor being down is not an outage, and a heartbeat that raised would
    take the process with it - turning a monitoring blip into the thing it watches."""
    kuma = FakeKuma(fails=True)

    await heartbeat_for(kuma).beat()

    assert kuma.beats == []


async def test_a_rejected_push_never_breaks_the_loop(caplog: pytest.LogCaptureFixture) -> None:
    kuma = FakeKuma(status=404)

    await heartbeat_for(kuma).beat()

    assert "heartbeat" in caplog.text.lower()


async def test_the_beat_repeats_until_the_process_is_told_to_stop() -> None:
    """Kuma's whole contract: beats keep coming, and their absence is the alarm."""
    kuma = FakeKuma()
    stopping = asyncio.Event()
    running = asyncio.create_task(heartbeat_for(kuma).run(stopping))

    await beats_reach(kuma, 3)
    stopping.set()
    await running
    beaten = len(kuma.beats)

    await asyncio.sleep(0.01)
    assert len(kuma.beats) == beaten, "a stopped heartbeat is a silent one"


async def test_a_hung_loop_stops_the_beat_even_though_the_process_is_up() -> None:
    """Why the beat is emitted from inside the loop rather than by an HTTP handler.

    A health endpoint answers as long as the process is alive, so a bot whose loop
    has wedged looks healthy. Here the beat shares the loop with both legs, so
    anything that stops the loop turning stops the beat: while the block below
    holds, no beat can be emitted, and Kuma's missed-beat alert is what fires.
    """
    kuma = FakeKuma()
    stopping = asyncio.Event()
    running = asyncio.create_task(heartbeat_for(kuma).run(stopping))
    await beats_reach(kuma, 1)

    before = len(kuma.beats)
    time.sleep(0.05)  # a synchronous hang: the loop cannot run anything at all
    assert len(kuma.beats) == before

    await beats_reach(kuma, before + 1)  # and it recovers once the loop turns again
    stopping.set()
    await running


def test_the_shipped_interval_is_about_a_minute() -> None:
    """Kuma's monitor is configured against this number; drifting is a false alarm."""
    assert 30.0 <= HEARTBEAT_SECONDS <= 60.0
