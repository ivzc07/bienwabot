"""The ops channel: the control that is not in the group, and what it does not touch."""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta

import pytest

from rebe_agent.alerts import TelegramAlerter, ThrottledAlerter
from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.evolution import EvolutionClient, EvolutionError
from rebe_agent.heartbeat import Heartbeat
from rebe_agent.ops import Control, OpsChannel
from rebe_agent.pacer import Envelope, Pacer, RefusalReason, SendRefusedError
from rebe_agent.pause import InMemoryPauseSwitch, PauseState
from rebe_agent.ramp import InMemoryRampStore, Ramp
from rebe_agent.sends import InMemorySendLog, SendKind
from rebe_agent.signals import Signal, Watchtower
from rebe_agent.telegram import TelegramClient, Update
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.kuma_stub import PUSH_URL, FakeKuma
from tests.support import GROUP, NOON, RecordingAlerter
from tests.telegram_stub import CHAT_ID, TOKEN, FakeTelegram, message

MEMBER_CHAT = "5215500000000"
"""Somebody who is not the maintainer, messaging the bot directly."""

MESSAGE = "Nuevo modelo de OpenAI, ahora corre local. Se ve interesante."

ROOMY = Envelope(sends_per_hour=1000, sends_per_day=1000, post_gap=(timedelta(0), timedelta(0)))


def ramp_for(clock: ManualClock) -> Ramp:
    """A ramp that forgets on restart. What it does is `tests/test_ramp.py`; here
    it is only the fifth thing an assembled ops channel is made of."""
    return Ramp(InMemoryRampStore(), clock, InMemorySendLog())


def pacer_for(
    evolution: FakeEvolution,
    log: InMemorySendLog,
    clock: ManualClock,
    *,
    pause: InMemoryPauseSwitch | None = None,
    watch: Watchtower | None = None,
) -> Pacer:
    return Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        log,
        clock,
        envelope=ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(20260725),
        pause=pause,
        watch=watch,
    )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
def switch(clock: ManualClock) -> InMemoryPauseSwitch:
    return InMemoryPauseSwitch(clock)


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


def control_for(
    telegram: FakeTelegram, switch: InMemoryPauseSwitch, *, poll_seconds: int = 0
) -> Control:
    return Control(
        TelegramClient(TOKEN, CHAT_ID, http_client=telegram.client()),
        switch,
        CHAT_ID,
        poll_seconds=poll_seconds,
        retry_seconds=0.001,
    )


def ops_message(text: str, *, chat_id: str = CHAT_ID, update_id: int = 1) -> Update:
    return Update(update_id=update_id, chat_id=chat_id, text=text)


# --- The control ------------------------------------------------------------


async def test_the_maintainer_can_silence_her_from_telegram(
    telegram: FakeTelegram, switch: InMemoryPauseSwitch
) -> None:
    await control_for(telegram, switch).handle(ops_message("/pausa"))

    assert (await switch.state()).paused is True
    assert telegram.texts, "the operator gets told the switch moved"


async def test_a_pause_can_say_why(telegram: FakeTelegram, switch: InMemoryPauseSwitch) -> None:
    """Two hours later, "why is Rebe quiet" has to be answerable."""
    await control_for(telegram, switch).handle(ops_message("/pausa mucho ruido hoy"))

    assert (await switch.state()).reason == "mucho ruido hoy"


async def test_resuming_lets_her_talk_again(
    telegram: FakeTelegram, switch: InMemoryPauseSwitch
) -> None:
    control = control_for(telegram, switch)
    await control.handle(ops_message("/pausa"))

    await control.handle(ops_message("/reanuda", update_id=2))

    assert (await switch.state()).paused is False


async def test_asking_for_the_state_changes_nothing(
    telegram: FakeTelegram, switch: InMemoryPauseSwitch
) -> None:
    control = control_for(telegram, switch)
    await control.handle(ops_message("/pausa porque si"))

    await control.handle(ops_message("/estado", update_id=2))

    assert (await switch.state()).paused is True
    assert "porque si" in telegram.texts[-1]


async def test_the_bot_suffix_and_the_english_name_are_both_accepted(
    telegram: FakeTelegram, switch: InMemoryPauseSwitch
) -> None:
    """Telegram appends @thebot to commands in some clients, and the maintainer
    types whichever word comes to hand."""
    await control_for(telegram, switch).handle(ops_message("/Pause@RebeOpsBot"))

    assert (await switch.state()).paused is True


async def test_only_the_ops_chat_can_flip_the_switch(
    telegram: FakeTelegram, switch: InMemoryPauseSwitch
) -> None:
    """The whole reason the control lives here: a group member cannot reach it.

    Nothing typed in the WhatsApp group is a control path at all - Rebe never
    reads a command from the group - and a stranger who finds the Telegram bot
    gets silence rather than a confirmation that it exists.
    """
    await control_for(telegram, switch).handle(ops_message("/pausa", chat_id=MEMBER_CHAT))

    assert (await switch.state()).paused is False
    assert telegram.texts == []


async def test_something_that_is_not_a_command_flips_nothing(
    telegram: FakeTelegram, switch: InMemoryPauseSwitch
) -> None:
    await control_for(telegram, switch).handle(ops_message("hola, todo bien?"))

    assert (await switch.state()).paused is False


async def test_a_command_that_could_not_be_carried_out_is_offered_again(
    telegram: FakeTelegram,
) -> None:
    """The offset moves only after the switch actually moved. Acknowledging first
    would drop a `/pausa` whose write failed - on the one control path that exists
    to stop a banned number sending."""

    class UnwritableSwitch(InMemoryPauseSwitch):
        broken = True

        async def set_paused(self, paused: bool, *, reason: str = "") -> PauseState:
            if self.broken:
                raise RuntimeError("the rebe database is unreachable")
            return await super().set_paused(paused, reason=reason)

    switch = UnwritableSwitch(ManualClock(NOON))
    control = control_for(telegram, switch)

    with pytest.raises(RuntimeError):
        await control.handle(ops_message("/pausa", update_id=7))
    telegram.updates = [[message(7, "/pausa")]]
    switch.broken = False
    stopping = asyncio.Event()
    running = asyncio.create_task(control.run(stopping))
    while not (await switch.state()).paused:
        await asyncio.sleep(0)
    stopping.set()
    await running

    assert telegram.polls[0]["offset"] == 0, "the failed command was never acknowledged"


async def test_a_command_is_acted_on_once_however_often_telegram_offers_it(
    switch: InMemoryPauseSwitch,
) -> None:
    """Telegram redelivers an update until the offset moves past it, so the offset
    is what stops one `/pausa` being handled forever."""
    telegram = FakeTelegram(updates=[[message(7, "/pausa")], []])
    control = control_for(telegram, switch)
    stopping = asyncio.Event()

    running = asyncio.create_task(control.run(stopping))
    while len(telegram.polls) < 2:
        await asyncio.sleep(0)
    stopping.set()
    await running

    assert telegram.polls[0]["offset"] == 0
    assert telegram.polls[1]["offset"] == 8
    assert len(telegram.texts) == 1


async def test_a_telegram_that_is_down_does_not_take_the_control_channel_with_it(
    switch: InMemoryPauseSwitch,
) -> None:
    """The ops channel is the last thing that should die when something breaks."""
    telegram = FakeTelegram(status=502)
    stopping = asyncio.Event()

    running = asyncio.create_task(control_for(telegram, switch).run(stopping))
    while len(telegram.polls) < 3:
        await asyncio.sleep(0)
    stopping.set()
    await running

    assert len(telegram.polls) >= 3, "it kept trying"


# --- What a failed send sounds like ------------------------------------------


@pytest.mark.parametrize("status", [463, 429])
async def test_a_rate_limited_send_reaches_telegram_and_names_the_signal(
    status: int, clock: ManualClock, telegram: FakeTelegram
) -> None:
    """The whole path, through the real seams: WhatsApp pushes back on a send, and
    the maintainer's phone buzzes over a channel that does not need WhatsApp.

    Both statuses, because the playbook names the 463 reach-out time-lock and the
    spec answers "463 / 4xx rate error" with one response.
    """
    evolution = FakeEvolution()
    evolution.text_status = status
    alerts = ThrottledAlerter(
        TelegramAlerter(TelegramClient(TOKEN, CHAT_ID, http_client=telegram.client())), clock
    )
    watch = Watchtower(alerts, pause=InMemoryPauseSwitch(clock))
    pacer = pacer_for(evolution, InMemorySendLog(), clock, watch=watch)

    with pytest.raises(EvolutionError):
        await pacer.send(SendKind.POST, GROUP, MESSAGE)

    (alert,) = telegram.texts
    assert Signal.RATE_LIMITED in alert
    assert "463" in alert


async def test_the_alert_does_not_swallow_the_failure(
    clock: ManualClock, switch: InMemoryPauseSwitch
) -> None:
    """The caller still has to hear "this did not go out" - the news leg drops the
    item on it, and a caller told nothing would think the group had seen the post."""
    evolution = FakeEvolution()
    evolution.text_status = 500
    alerts = RecordingAlerter()
    pacer = pacer_for(evolution, InMemorySendLog(), clock, watch=Watchtower(alerts, pause=switch))

    with pytest.raises(EvolutionError) as failed:
        await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert failed.value.status == 500
    assert Signal.SEND_FAILED in alerts.messages[0]


async def test_a_refusal_is_not_worth_waking_anybody_for(
    clock: ManualClock, switch: InMemoryPauseSwitch
) -> None:
    """The envelope saying "not now" is the envelope working, and a maintainer who
    is paged for it stops reading the alerts."""
    alerts = RecordingAlerter()
    await switch.set_paused(True, reason="cool it")
    pacer = pacer_for(
        FakeEvolution(), InMemorySendLog(), clock, pause=switch, watch=Watchtower(alerts)
    )

    with pytest.raises(SendRefusedError):
        await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert alerts.messages == []


# --- What a pause does not stop ----------------------------------------------


async def test_the_heartbeat_keeps_flowing_while_she_is_paused(
    clock: ManualClock, switch: InMemoryPauseSwitch
) -> None:
    """A pause must look different from a crash, or the operator learns to ignore
    the monitor. Rebe stays in the group, the process stays up, the beat keeps
    flowing - only the sending stops. The scheduler is the same story and lands
    with the cadence ticket: nothing here reads the switch except the pacer."""
    kuma = FakeKuma()
    evolution = FakeEvolution()
    pacer = pacer_for(evolution, InMemorySendLog(), clock, pause=switch)
    await switch.set_paused(True, reason="cool it")

    await Heartbeat(PUSH_URL, kuma.client()).beat()
    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.POST, GROUP, "una noticia")

    assert refused.value.reason is RefusalReason.SOFT_PAUSE
    assert [beat.status for beat in kuma.beats] == ["up"]
    assert evolution.calls == []


async def test_the_channel_serves_the_heartbeat_and_the_control_together(
    clock: ManualClock, switch: InMemoryPauseSwitch
) -> None:
    """One `serve` is what the run loop awaits, so neither half can be forgotten."""
    kuma = FakeKuma()
    telegram = FakeTelegram(updates=[[message(3, "/pausa")]])
    alerts = RecordingAlerter()
    channel = OpsChannel(
        alerts=alerts,
        watchtower=Watchtower(alerts, pause=switch),
        pause=switch,
        heartbeat=Heartbeat(PUSH_URL, kuma.client(), interval=0.001),
        control=control_for(telegram, switch),
        ramp=ramp_for(clock),
    )
    stopping = asyncio.Event()

    running = asyncio.create_task(channel.serve(stopping))
    while not kuma.beats or not (await switch.state()).paused:
        await asyncio.sleep(0)
    stopping.set()
    await running

    assert kuma.beats
    assert (await switch.state()).paused is True


async def test_a_loop_that_dies_ends_the_channel_rather_than_half_serving(
    clock: ManualClock, switch: InMemoryPauseSwitch, caplog: pytest.LogCaptureFixture
) -> None:
    """A process that keeps running without a heartbeat is a process lying to Kuma."""

    class BrokenHeartbeat(Heartbeat):
        async def run(self, stopping: asyncio.Event) -> None:
            raise RuntimeError("the heartbeat loop fell over")

    alerts = RecordingAlerter()
    channel = OpsChannel(
        alerts=alerts,
        watchtower=Watchtower(alerts, pause=switch),
        pause=switch,
        heartbeat=BrokenHeartbeat(PUSH_URL, FakeKuma().client()),
        control=control_for(FakeTelegram(), switch),
        ramp=ramp_for(clock),
    )

    await channel.serve(asyncio.Event(), grace=0.01)

    assert "heartbeat" in caplog.text
