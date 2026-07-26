"""The post-pairing ramp, and backing off when WhatsApp pushes back.

Nothing here waits on real time, which is the point of the ticket: two clean
weeks, a 72-hour idle gap and an hour of backing off all pass in the time it
takes to run an assertion, because the clock is moved by hand and the sleeping
goes through a `ManualSleeper` that advances that clock instead of the world.

Three layers are exercised, in this order: the arithmetic of the ramp on its own,
the pacer refusing what the ramp says it must, and a whole scheduled day coming
out three posts long.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

import pytest

from rebe_agent.cadence import DayPlan, Slot, moment_on
from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.evolution import (
    EvolutionClient,
    EvolutionError,
    EvolutionRateLimitedError,
)
from rebe_agent.heartbeat import Heartbeat
from rebe_agent.news import Posted
from rebe_agent.pacer import Envelope, Pacer, RefusalReason, SendRefusedError
from rebe_agent.pause import InMemoryPauseSwitch
from rebe_agent.plans import InMemoryPlanStore
from rebe_agent.ramp import InMemoryRampStore, Ramp, RampPlan, RampReason
from rebe_agent.scheduler import Scheduler
from rebe_agent.sends import InMemorySendLog, SendKind, SendRecord, fingerprint
from rebe_agent.signals import Signal, Watchtower
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.kuma_stub import PUSH_URL, FakeKuma
from tests.support import GROUP, MEXICO_CITY, NOON, RecordingAlerter, item

SEED = 20260729

WEDNESDAY = date(2026, 7, 29)
"""A weekday, so the cadence draws four windows rather than two."""

WEEK = timedelta(days=7)
DAY = timedelta(days=1)


def at(moment: time, day: date = WEDNESDAY) -> datetime:
    return moment_on(day, moment, MEXICO_CITY)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
def sends() -> InMemorySendLog:
    return InMemorySendLog()


@pytest.fixture
def store() -> InMemoryRampStore:
    return InMemoryRampStore()


@pytest.fixture
def ramp(store: InMemoryRampStore, clock: ManualClock, sends: InMemorySendLog) -> Ramp:
    return Ramp(store, clock, sends)


async def seed_send(
    log: InMemorySendLog,
    when: datetime,
    *,
    kind: SendKind = SendKind.POST,
    text: str = "algo",
) -> None:
    """One message Rebe already sent, as a restart would find it in the log."""
    await log.record(
        SendRecord(
            sent_at=when,
            day=when.astimezone(MEXICO_CITY).date(),
            kind=kind,
            chat=GROUP,
            fingerprint=fingerprint(text),
        )
    )


async def a_busy(span: timedelta, ramp: Ramp, sends: InMemorySendLog, clock: ManualClock) -> None:
    """Move `span` forward with Rebe posting every day, so no idle gap opens.

    Two clean weeks means two weeks of her *posting*, and the ramp reads the send
    log to tell that apart from a fortnight of silence. So a test about the
    calendar has to put the sends there.
    """
    await ramp.state()
    remaining = span
    while remaining > timedelta(0):
        step = min(DAY, remaining)
        clock.advance(step)
        await seed_send(sends, clock.now(), text=f"nota del {clock.now():%Y-%m-%d %H:%M}")
        remaining -= step


# --- the clamp, week by week --------------------------------------------------


async def test_the_first_week_after_pairing_is_clamped_to_three_posts(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """Section 1 of the playbook: week one caps the day at three news posts."""
    assert await ramp.post_cap() == 3

    await a_busy(timedelta(days=6, hours=23), ramp, sends, clock)

    assert await ramp.post_cap() == 3


async def test_the_second_week_is_clamped_to_four(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """The playbook's own week-two cap is five, but section 1 of the cadence spec
    puts the target at four and the lower of the two is the one that binds."""
    await a_busy(WEEK, ramp, sends, clock)

    assert await ramp.post_cap() == 4

    await a_busy(timedelta(days=6, hours=23), ramp, sends, clock)

    assert await ramp.post_cap() == 4


async def test_after_two_clean_weeks_there_is_no_clamp(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """Steady state is the cadence spec's, unclamped - which is what `None` says."""
    await a_busy(2 * WEEK, ramp, sends, clock)

    assert await ramp.post_cap() is None


async def test_the_ramp_start_survives_a_restart(
    store: InMemoryRampStore, clock: ManualClock, sends: InMemorySendLog
) -> None:
    """A ramp a redeploy silently restarted would keep her at three posts a day
    forever; one a redeploy skipped would front-load a fortnight-old number."""
    first = Ramp(store, clock, sends)
    started = (await first.state()).started_at
    await a_busy(WEEK + timedelta(hours=1), first, sends, clock)

    after_restart = Ramp(store, clock, sends)

    assert (await after_restart.state()).started_at == started
    assert await after_restart.post_cap() == 4


async def test_the_ramp_starts_once_and_records_why(ramp: Ramp, clock: ManualClock) -> None:
    first = await ramp.state()
    clock.advance(timedelta(hours=2))

    assert (await ramp.state()).started_at == first.started_at
    assert first.reason is RampReason.PAIRED


# --- an idle gap --------------------------------------------------------------


async def test_an_idle_gap_of_seventy_two_hours_puts_her_back_on_week_one(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """A number that has said nothing for three days is a cold number, whatever
    week of the ramp the calendar says it is in."""
    await a_busy(3 * WEEK, ramp, sends, clock)
    assert await ramp.post_cap() is None

    clock.advance(timedelta(hours=72))

    assert await ramp.post_cap() == 3
    assert (await ramp.state()).reason is RampReason.IDLE


async def test_a_gap_shorter_than_seventy_two_hours_leaves_the_ramp_alone(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    await a_busy(3 * WEEK, ramp, sends, clock)

    clock.advance(timedelta(hours=71, minutes=59))

    assert await ramp.post_cap() is None


async def test_a_silence_that_goes_on_keeps_her_on_the_week_one_clamp(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """Stamping the re-entry once and letting the ramp age through the rest of
    the gap would end a fortnight of quiet at full rate, which is the cold resume
    the whole rule exists to prevent."""
    await ramp.state()
    await seed_send(sends, clock.now())
    clock.advance(timedelta(hours=73))
    assert await ramp.post_cap() == 3

    clock.advance(2 * WEEK)

    assert await ramp.post_cap() == 3


async def test_the_ramp_counts_from_the_day_she_starts_talking_again(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """And then runs its full two weeks from there, rather than holding her at
    week one for a fortnight after the silence ended."""
    await ramp.state()
    await seed_send(sends, clock.now())
    clock.advance(timedelta(hours=73))
    assert await ramp.post_cap() == 3

    await a_busy(WEEK, ramp, sends, clock)

    assert await ramp.post_cap() == 4


async def test_a_number_that_has_never_sent_anything_stays_on_the_ramp(
    ramp: Ramp, clock: ManualClock
) -> None:
    """No sends at all is the coldest a number gets, so the gap is measured from
    the ramp itself when there is no send to measure it from."""
    await ramp.state()

    clock.advance(3 * WEEK)

    assert await ramp.post_cap() == 3


# --- a disconnect and a reconnect ---------------------------------------------


async def test_a_link_that_went_down_stops_every_send(ramp: Ramp) -> None:
    await ramp.link_down("Evolution reports the connection as 'close'")

    halt = await ramp.halt()

    assert halt is not None
    assert "close" in halt.detail


async def test_a_reconnect_puts_her_back_on_week_one(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """Verified without waiting a real week: three weeks of steady state, one
    disconnect, one reconnect, and she is on three posts a day again."""
    await a_busy(3 * WEEK, ramp, sends, clock)
    assert await ramp.post_cap() is None

    await ramp.link_down("the socket dropped")
    await ramp.link_up()

    assert await ramp.post_cap() == 3
    assert (await ramp.state()).reason is RampReason.RECONNECTED
    assert await ramp.halt() is None, "sending resumes, it just resumes clamped"


async def test_an_open_we_were_not_waiting_for_changes_nothing(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """Evolution announces an open link at every boot. Treating that as a
    reconnect would put a redeployed agent back on week one forever."""
    await a_busy(3 * WEEK, ramp, sends, clock)

    await ramp.link_up()

    assert await ramp.post_cap() is None


async def test_the_link_hold_is_bounded_so_a_lost_reconnect_is_not_forever(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """If the `open` webhook never arrives, the halt lapses on its own rather
    than silencing her until somebody notices - and the lapse is treated as the
    reconnect it stands in for, so she comes back clamped rather than at the old
    rate. Every way back from a disconnect is a cold resume."""
    await a_busy(3 * WEEK, ramp, sends, clock)
    await ramp.link_down("the socket dropped")

    clock.advance(RampPlan().link_hold + timedelta(seconds=1))

    assert await ramp.halt() is None
    assert await ramp.post_cap() == 3
    assert (await ramp.state()).reason is RampReason.RECONNECTED


async def test_a_link_that_is_still_down_keeps_extending_the_hold(
    ramp: Ramp, clock: ManualClock
) -> None:
    await ramp.link_down("the socket dropped")
    clock.advance(RampPlan().link_hold - timedelta(minutes=1))
    await ramp.link_down("still down")

    clock.advance(timedelta(minutes=2))

    assert await ramp.halt() is not None


# --- backing off --------------------------------------------------------------


async def test_a_rate_error_stops_sending_for_the_back_off_window(
    ramp: Ramp, clock: ManualClock
) -> None:
    await ramp.back_off("a 463 reach-out time-lock")

    halt = await ramp.halt()

    assert halt is not None
    assert halt.until == clock.now() + RampPlan().back_off


async def test_the_back_off_lets_her_go_again_when_it_is_over(
    ramp: Ramp, clock: ManualClock
) -> None:
    await ramp.back_off("a 463 reach-out time-lock")

    clock.advance(RampPlan().back_off + timedelta(seconds=1))

    assert await ramp.halt() is None


async def test_a_reconnect_does_not_wipe_a_rate_limit_back_off(ramp: Ramp) -> None:
    """The two say different things. A socket coming back is no evidence at all
    that WhatsApp has stopped throttling the number."""
    await ramp.back_off("a 463 reach-out time-lock")
    await ramp.link_down("the socket dropped")

    await ramp.link_up()

    assert await ramp.halt() is not None


# --- what Evolution's connection.update does to it ----------------------------


async def test_a_disconnect_from_evolution_stops_sending(ramp: Ramp) -> None:
    """Section 5's failure table, through the object that reports it."""
    alerts = RecordingAlerter()

    await Watchtower(alerts, ramp=ramp).connection_changed("close")

    assert await ramp.halt() is not None
    assert any(Signal.DISCONNECTED in message for message in alerts.messages)


async def test_a_reconnect_from_evolution_re_enters_the_ramp(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    tower = Watchtower(RecordingAlerter(), ramp=ramp)
    await a_busy(3 * WEEK, ramp, sends, clock)
    await tower.connection_changed("close")

    await tower.connection_changed("open")

    assert await ramp.halt() is None
    assert await ramp.post_cap() == 3


async def test_a_reconnect_is_not_an_alert(ramp: Ramp) -> None:
    """An `open` is a change of posture, not news. Alerting on it would train the
    maintainer to ignore the channel that carries the disconnect."""
    alerts = RecordingAlerter()

    await Watchtower(alerts, ramp=ramp).connection_changed("open")

    assert alerts.messages == []


async def test_a_half_open_socket_is_neither(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """Baileys reports `connecting` on its way to both states, and it is not a
    reason to stop sending nor a reconnect to come back from."""
    await a_busy(3 * WEEK, ramp, sends, clock)

    await Watchtower(RecordingAlerter(), ramp=ramp).connection_changed("connecting")

    assert await ramp.halt() is None
    assert await ramp.post_cap() is None


async def test_a_temporary_ban_waited_out_comes_back_on_the_ramp(
    ramp: Ramp, sends: InMemorySendLog, clock: ManualClock
) -> None:
    """Section 4: "If the primary is temp-banned: pause, wait it out, resume on
    the ramp." The ban arrives as a disconnect and the operator resumes hours
    later, by which time the link hold has lapsed into a re-entry."""
    await a_busy(3 * WEEK, ramp, sends, clock)

    await Watchtower(RecordingAlerter(), ramp=ramp).connection_changed("close", reason=401)
    clock.advance(timedelta(hours=6))

    assert await ramp.halt() is None
    assert await ramp.post_cap() == 3


async def test_a_ban_shaped_disconnect_stops_sending_too(ramp: Ramp) -> None:
    """403 reads as a permanent ban, and that is a stronger signal than a dropped
    socket - but the link is down either way, so sending stops either way."""
    alerts = RecordingAlerter()
    switch = InMemoryPauseSwitch(ManualClock(NOON))

    await Watchtower(alerts, pause=switch, ramp=ramp).connection_changed("close", reason=403)

    assert await ramp.halt() is not None
    assert (await switch.state()).paused is True
    assert any(Signal.PERMANENT_BAN in message for message in alerts.messages)


# --- the pacer, which is where all of it binds --------------------------------


ROOMY = Envelope(sends_per_hour=1000, sends_per_day=1000, post_gap=(timedelta(0), timedelta(0)))
"""The other ceilings lifted, so a test exercises the ramp and nothing else."""


def make_pacer(
    evolution: FakeEvolution,
    log: InMemorySendLog,
    clock: ManualClock,
    ramp: Ramp,
    *,
    watch: Watchtower | None = None,
    pause: InMemoryPauseSwitch | None = None,
) -> Pacer:
    return Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        log,
        clock,
        envelope=ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
        ramp=ramp,
        watch=watch,
        pause=pause,
    )


def rate_limited() -> FakeEvolution:
    """An Evolution that answers a send with a 463 reach-out time-lock."""
    evolution = FakeEvolution()
    evolution.text_status = 463
    return evolution


async def test_a_fourth_post_in_week_one_is_refused_by_the_envelope(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    """The clamp lives in the one place a message leaves through, so it covers
    the drawn slots and the breaking-news overrides alike."""
    evolution = FakeEvolution()
    pacer = make_pacer(evolution, sends, clock, ramp)

    for number in range(3):
        await pacer.send(SendKind.POST, GROUP, f"nota {number}")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.POST, GROUP, "nota 4")

    assert refused.value.reason is RefusalReason.RAMP_CLAMP
    assert len(evolution.texts) == 3


async def test_a_reply_is_not_clamped_by_the_ramp(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    """The playbook clamps news posts and says "replies as normal"."""
    evolution = FakeEvolution()
    pacer = make_pacer(evolution, sends, clock, ramp)
    for number in range(3):
        await pacer.send(SendKind.POST, GROUP, f"nota {number}")

    await pacer.send(SendKind.REPLY, GROUP, "si, justo eso")

    assert len(evolution.texts) == 4


async def test_the_clamp_lifts_once_the_ramp_is_over(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    evolution = FakeEvolution()
    pacer = make_pacer(evolution, sends, clock, ramp)
    await a_busy(2 * WEEK, ramp, sends, clock)

    for number in range(5):
        await pacer.send(SendKind.POST, GROUP, f"nota {number}")

    assert len(evolution.texts) == 5


async def test_a_rate_limited_send_is_not_retried_in_a_tight_loop(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    """The acceptance criterion, counted on the wire rather than read off the
    code: a 463 is one call to Evolution, and the next three are refused here."""
    evolution = rate_limited()
    alerts = RecordingAlerter()
    pacer = make_pacer(evolution, sends, clock, ramp, watch=Watchtower(alerts, ramp=ramp))

    with pytest.raises(EvolutionRateLimitedError):
        await pacer.send(SendKind.POST, GROUP, "nota 1")
    calls_after_the_463 = len(evolution.calls)

    for number in range(3):
        with pytest.raises(SendRefusedError) as refused:
            await pacer.send(SendKind.POST, GROUP, f"nota {number + 2}")
        assert refused.value.reason is RefusalReason.BACKING_OFF

    assert len(evolution.calls) == calls_after_the_463, "nothing went near the transport"
    assert len(evolution.texts) == 1
    assert any(Signal.RATE_LIMITED in message for message in alerts.messages)


async def test_a_transport_failure_that_is_not_a_rate_error_does_not_back_off(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    """A 500 from Evolution is a broken hop, not WhatsApp pushing back, and
    stopping the day over it would be an outage the playbook never asked for."""
    evolution = FakeEvolution()
    evolution.text_status = 500
    pacer = make_pacer(
        evolution, sends, clock, ramp, watch=Watchtower(RecordingAlerter(), ramp=ramp)
    )

    with pytest.raises(EvolutionError):
        await pacer.send(SendKind.POST, GROUP, "nota 1")

    assert await ramp.halt() is None


async def test_a_disconnected_link_stops_posts_and_replies_alike(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    evolution = FakeEvolution()
    pacer = make_pacer(evolution, sends, clock, ramp)
    await ramp.link_down("Evolution reports the connection as 'close'")

    for kind in (SendKind.POST, SendKind.REPLY):
        with pytest.raises(SendRefusedError) as refused:
            await pacer.send(kind, GROUP, f"algo para {kind}")
        assert refused.value.reason is RefusalReason.LINK_DOWN

    assert evolution.calls == [], "not even a typing indicator"


async def test_a_reconnect_resumes_sending_under_the_week_one_clamp(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    """The whole point of the ticket in one test: three weeks in, the link drops,
    it comes back, and she is on three posts a day rather than her previous rate."""
    evolution = FakeEvolution()
    pacer = make_pacer(evolution, sends, clock, ramp)
    await a_busy(3 * WEEK, ramp, sends, clock)
    clock.advance(timedelta(hours=26))  # a fresh local day, with nothing posted on it yet

    await ramp.link_down("the socket dropped")
    await ramp.link_up()
    for number in range(3):
        await pacer.send(SendKind.POST, GROUP, f"nota {number}")
    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.POST, GROUP, "nota 4")

    assert refused.value.reason is RefusalReason.RAMP_CLAMP
    assert len(evolution.texts) == 3


async def test_the_heartbeat_keeps_flowing_while_sending_is_stopped(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    """The distinction the ticket is about: the agent is alive, the number is not
    sending, and whoever reads the alert has to be able to tell those apart."""
    evolution = FakeEvolution()
    pacer = make_pacer(evolution, sends, clock, ramp)
    kuma = FakeKuma()
    heartbeat = Heartbeat(PUSH_URL, kuma.client())

    await heartbeat.beat()
    await ramp.link_down("Evolution reports the connection as 'close'")
    with pytest.raises(SendRefusedError):
        await pacer.send(SendKind.POST, GROUP, "nota")
    await heartbeat.beat()

    assert len(kuma.beats) == 2
    assert {beat.status for beat in kuma.beats} == {"up"}
    assert evolution.calls == []


# --- a permanent ban ----------------------------------------------------------


async def test_a_permanent_ban_stops_every_send_and_swaps_no_instance(
    clock: ManualClock, sends: InMemorySendLog, ramp: Ramp
) -> None:
    """No automatic failover, ever: swapping on a possibly-false ban signal burns
    the only warm standby. The instance is configuration, and the swap is a human
    editing `EVOLUTION_INSTANCE` and redeploying - see
    `docs/wayfinder/ramp-and-recovery-runbook.md`."""
    evolution = FakeEvolution()
    switch = InMemoryPauseSwitch(clock)
    pacer = make_pacer(evolution, sends, clock, ramp, pause=switch)
    tower = Watchtower(RecordingAlerter(), pause=switch, ramp=ramp)

    await tower.report(Signal.PERMANENT_BAN, detail="WhatsApp refused the number")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.POST, GROUP, "nota")
    assert refused.value.reason is RefusalReason.SOFT_PAUSE
    assert (await switch.state()).paused is True
    assert evolution.calls == []


# --- a whole day, clamped -----------------------------------------------------


class PacedLeg:
    """A news leg boiled down to the one property this test needs: it posts
    through the shared pacer, so the envelope has its say on every slot."""

    def __init__(self, pacer: Pacer) -> None:
        self._pacer = pacer
        self.posted = 0

    async def run(self, chat: str, *, limit: int = 1) -> Sequence[Posted]:
        self.posted += 1
        text = f"miren, nota numero {self.posted}"
        message = await self._pacer.send(SendKind.POST, chat, text)
        return [Posted(item=item(source_id=f"item-{self.posted}"), text=text, message=message)]


def make_day(
    clock: ManualClock,
    sends: InMemorySendLog,
    ramp: Ramp,
    evolution: FakeEvolution,
    *,
    plans: InMemoryPlanStore | None = None,
) -> tuple[Scheduler, InMemoryPlanStore]:
    """The real scheduler over the real pacer, with only Evolution standing in."""
    sleeper = ManualSleeper(clock)
    pacer = Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        sends,
        clock,
        sleeper=sleeper,
        rng=random.Random(SEED),
        ramp=ramp,
    )
    plans = plans if plans is not None else InMemoryPlanStore()
    scheduler = Scheduler(
        PacedLeg(pacer),
        GROUP,
        plans,
        sends,
        clock,
        sleeper=sleeper,
        rng=random.Random(SEED),
    )
    return scheduler, plans


async def run_the_day(scheduler: Scheduler, steps: int = 11) -> None:
    for _ in range(steps):
        await scheduler.step()


async def test_a_weekday_that_drew_four_slots_posts_three_in_week_one(
    sends: InMemorySendLog, store: InMemoryRampStore
) -> None:
    """The acceptance criterion end to end: the plan is unchanged, four windows
    and all, and what reaches the group is three."""
    clock = ManualClock(at(time(5, 0)))
    ramp = Ramp(store, clock, sends)
    evolution = FakeEvolution()
    scheduler, plans = make_day(clock, sends, ramp, evolution)

    await run_the_day(scheduler)

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert len(plan.slots) == 4, "the ramp clamps what goes out, not what is drawn"
    assert len(evolution.texts) == 3
    assert [str(slot.state) for slot in plan.slots] == ["posted", "posted", "posted", "dropped"]


FIVE_SLOTS = (
    ("morning", time(9, 14), time(10, 30)),
    ("midday", time(11, 0), time(12, 30)),
    ("afternoon", time(13, 47), time(15, 0)),
    ("evening", time(18, 30), time(20, 0)),
    ("late", time(21, 45), time(23, 0)),
)
"""A day the overrides ran long on, registered rather than drawn.

Five is what section 4 of the cadence spec can add to a four-window day, and the
criterion names it: a plan that drew four *or five* slots posts at most three.
"""


async def test_a_day_with_five_slots_posts_three_of_them_in_week_one(
    sends: InMemorySendLog, store: InMemoryRampStore
) -> None:
    clock = ManualClock(at(time(5, 0)))
    ramp = Ramp(store, clock, sends)
    plans = InMemoryPlanStore()
    await plans.register(
        DayPlan(
            day=WEDNESDAY,
            slots=tuple(
                Slot(window=window, at=at(due), closes=at(closes))
                for window, due, closes in FIVE_SLOTS
            ),
        )
    )
    evolution = FakeEvolution()
    scheduler, _ = make_day(clock, sends, ramp, evolution, plans=plans)

    await run_the_day(scheduler)

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert len(evolution.texts) == 3
    assert [str(slot.state) for slot in plan.slots] == [
        "posted",
        "posted",
        "posted",
        "dropped",
        "dropped",
    ]


async def test_the_same_day_after_two_clean_weeks_posts_all_four(
    sends: InMemorySendLog, store: InMemoryRampStore
) -> None:
    clock = ManualClock(at(time(5, 0)) - 2 * WEEK)
    ramp = Ramp(store, clock, sends)
    await a_busy(2 * WEEK, ramp, sends, clock)
    evolution = FakeEvolution()
    scheduler, plans = make_day(clock, sends, ramp, evolution)

    await run_the_day(scheduler)

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert len(evolution.texts) == 4
    assert all(str(slot.state) == "posted" for slot in plan.slots)
