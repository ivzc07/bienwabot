"""The scheduler leg: the dawn roll, and what each drawn slot does when it comes due.

Nothing here waits on real time. The clock is moved by hand and the sleeping goes
through a `ManualSleeper` that advances that clock instead of the world, so a whole
day of Rebe's posting runs inside one test in a few milliseconds. The randomness is
seeded, so "jittered" is still repeatable.

One test at the bottom drives the real news leg - the one from #18, unchanged -
against a stand-in DeepSeek and a stand-in Evolution, because "the roll schedules
the news leg" is the acceptance criterion and a stub leg cannot prove it.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

import pytest

from rebe_agent.brain import build_brain
from rebe_agent.cadence import DAWN, Cadence, DayPlan, PostWindow, Slot, SlotState, moment_on
from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.config import Settings, load_settings
from rebe_agent.evolution import EvolutionClient, EvolutionError
from rebe_agent.news import NewsLeg, Posted
from rebe_agent.pacer import Pacer, RefusalReason, SendRefusedError, SentMessage
from rebe_agent.plans import InMemoryPlanStore
from rebe_agent.posted import InMemoryPostedStore
from rebe_agent.scheduler import Scheduler
from rebe_agent.sends import InMemorySendLog, SendKind, SendRecord, fingerprint
from rebe_agent.usage import InMemoryUsageStore
from tests.deepseek_stub import FakeDeepSeek, json_output_response
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, MEXICO_CITY, RecordingAlerter, item
from tests.test_config import COMPLETE_ENV

SEED = 20260729

WEDNESDAY = date(2026, 7, 29)
THURSDAY = date(2026, 7, 30)

MORNING_CLOSES = time(10, 30)
"""The weekday morning window's edge, which most of these slots belong to."""


def at(moment: time, day: date = WEDNESDAY) -> datetime:
    return moment_on(day, moment, MEXICO_CITY)


def slot(
    due: time, *, window: str = "morning", closes: time = MORNING_CLOSES, day: date = WEDNESDAY
) -> Slot:
    return Slot(window=window, at=at(due, day), closes=at(closes, day))


POSTED_ITEM = Posted(
    item=item(),
    text="miren, ya salio\nhttps://openai.com/index/nuevo-modelo",
    message=SentMessage(
        message_id="stub", at=at(time(9, 14)), typing_seconds=1.8, waited_seconds=0.0
    ),
)


class StubLeg:
    """A news leg that posts whatever it was told to, and counts the asking."""

    def __init__(self, *answers: Sequence[Posted] | Exception) -> None:
        self._answers: list[Sequence[Posted] | Exception] = list(answers) or [[POSTED_ITEM]]
        self.calls: list[tuple[str, int]] = []

    async def run(self, chat: str, *, limit: int = 1) -> Sequence[Posted]:
        self.calls.append((chat, limit))
        answer = self._answers[min(len(self.calls) - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class StubWatch:
    """An override leg that answers whatever it was told to, and counts the asking.

    The real one is driven end to end in `tests/test_breaking.py`; what these
    tests are about is *when* the loop asks it, which is this module's half.
    """

    def __init__(self, clock: ManualClock, *, claims: Posted | None = None) -> None:
        self._clock = clock
        self.checks: list[datetime] = []
        self.claims = 0
        self._claimed = claims

    async def check(self) -> Posted | None:
        self.checks.append(self._clock.now())
        return None

    async def claim_slot(self) -> Posted | None:
        self.claims += 1
        claimed, self._claimed = self._claimed, None
        return claimed


@pytest.fixture
def plans() -> InMemoryPlanStore:
    return InMemoryPlanStore()


@pytest.fixture
def sends() -> InMemorySendLog:
    return InMemorySendLog()


def make_scheduler(
    leg: StubLeg,
    clock: ManualClock,
    plans: InMemoryPlanStore,
    sends: InMemorySendLog,
    *,
    cadence: Cadence | None = None,
    seed: int = SEED,
    breaking: StubWatch | None = None,
) -> tuple[Scheduler, ManualSleeper]:
    sleeper = ManualSleeper(clock)
    scheduler = Scheduler(
        leg,
        GROUP,
        plans,
        sends,
        clock,
        cadence=cadence,
        sleeper=sleeper,
        rng=random.Random(seed),
        breaking=breaking,
    )
    return scheduler, sleeper


async def seed_send(
    sends: InMemorySendLog,
    when: datetime,
    *,
    text: str = "ahi va",
    kind: SendKind = SendKind.REPLY,
) -> None:
    """One message Rebe already sent, as a restart would find it in the log."""
    await sends.record(
        SendRecord(
            sent_at=when,
            day=when.astimezone(MEXICO_CITY).date(),
            kind=kind,
            chat=GROUP,
            fingerprint=fingerprint(text),
        )
    )


# --- the dawn roll -----------------------------------------------------------


async def test_the_scheduler_waits_for_dawn_before_drawing_anything(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Nothing is drawn at 05:00, because the day is drawn at 06:00."""
    clock = ManualClock(at(time(5, 0)))
    leg = StubLeg()
    scheduler, sleeper = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    assert sleeper.total == pytest.approx(3600)
    assert clock.now() == at(DAWN)
    assert await plans.plan_on(WEDNESDAY) is None
    assert leg.calls == []


async def test_the_dawn_roll_registers_one_slot_for_each_drawn_time(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """The headline acceptance criterion: one job a day draws the whole day."""
    clock = ManualClock(at(DAWN))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert [s.window for s in plan.slots] == ["morning", "midday", "evening", "late"]
    assert all(s.state is SlotState.PLANNED for s in plan.slots)
    assert leg.calls == [], "the roll draws times; it does not post"


async def test_the_loop_sleeps_until_the_next_planned_time(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    clock = ManualClock(at(DAWN))
    leg = StubLeg()
    scheduler, sleeper = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    assert clock.now() == at(time(9, 14))
    assert sleeper.total == pytest.approx(timedelta(hours=3, minutes=14).total_seconds())
    assert leg.calls == []


async def test_a_day_with_nothing_left_in_it_waits_for_the_next_dawn(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14), window="morning"),)))
    await plans.settle(WEDNESDAY, "morning", SlotState.POSTED)
    clock = ManualClock(at(time(11, 0)))
    scheduler, _ = make_scheduler(StubLeg(), clock, plans, sends)

    await scheduler.step()

    assert clock.now() == at(DAWN, THURSDAY)


async def test_a_process_that_comes_up_after_the_last_window_waits_for_dawn(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """A roll at 23:30 would draw a day that is already over. Better to sleep."""
    clock = ManualClock(at(time(23, 30)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    assert clock.now() == at(DAWN, THURSDAY)
    assert leg.calls == []


async def test_a_roll_that_runs_late_keeps_only_what_is_still_ahead_of_it(
    plans: InMemoryPlanStore, sends: InMemorySendLog, caplog: pytest.LogCaptureFixture
) -> None:
    """The process was down at dawn and comes up at 16:00: the morning and midday
    times it would have drawn are in the past, and a post at a time that has been
    and gone is worse than no post."""
    clock = ManualClock(at(time(16, 0)))
    scheduler, _ = make_scheduler(StubLeg(), clock, plans, sends)

    with caplog.at_level(logging.INFO):
        await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert [s.window for s in plan.slots] == ["evening", "late"]
    assert all(s.at > at(time(16, 0)) for s in plan.slots)
    assert "already past" in caplog.text


# --- one slot coming due -----------------------------------------------------


async def test_a_slot_that_comes_due_posts_one_curated_item(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    clock = ManualClock(at(time(9, 14)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    assert leg.calls == [(GROUP, 1)], "one slot is one post, not a catch-up batch"
    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.POSTED
    assert plan.pending == ()


async def test_a_window_with_nothing_worth_posting_is_skipped_in_silence(
    plans: InMemoryPlanStore, sends: InMemorySendLog, caplog: pytest.LogCaptureFixture
) -> None:
    """The soft edge from section 1: Rebe never posts filler to hit a number, and
    the quality bar itself belongs to the curator, not here. An empty run is the
    news leg saying nothing cleared it."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    clock = ManualClock(at(time(9, 14)))
    leg = StubLeg([])
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    with caplog.at_level(logging.INFO):
        await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.SKIPPED
    assert len(leg.calls) == 1, "a skipped window is not retried into a filler post"
    assert "morning" in caplog.text


async def test_a_slot_missed_while_the_process_was_down_is_not_posted_hours_late(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """09:14 posted at 14:00 is not a person catching up; it is a machine that was
    switched off. The slot is dropped, and the rest of the day stands."""
    await plans.register(
        DayPlan(
            day=WEDNESDAY,
            slots=(slot(time(9, 14)), slot(time(13, 47), window="midday", closes=time(15, 0))),
        )
    )
    clock = ManualClock(at(time(14, 0)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.DROPPED
    assert leg.calls == []
    assert [s.window for s in plan.pending] == ["midday"]


async def test_a_slot_a_little_past_its_window_still_goes_out(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Up to about thirty minutes past the edge is a person who got distracted."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(10, 25)),)))
    clock = ManualClock(at(time(10, 50)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    assert leg.calls == [(GROUP, 1)]


# --- collision with live conversation ----------------------------------------


async def test_a_slot_landing_on_her_own_message_is_deferred_past_it(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Dropping a news link seconds after answering someone reads as two programs
    running side by side, because it is. A person finishes the conversation and
    shares the link a bit later."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    last_message = at(time(9, 13))
    await seed_send(sends, last_message)
    clock = ManualClock(at(time(9, 14)))
    leg = StubLeg()
    scheduler, sleeper = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    assert leg.calls == [(GROUP, 1)]
    assert (
        last_message + timedelta(minutes=10) <= clock.now() <= last_message + timedelta(minutes=20)
    )
    assert sleeper.slept, "the post waited rather than landing on top of her reply"


async def test_the_deferral_is_measured_from_her_last_message_not_from_the_slot(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """She was mid-conversation for twenty minutes; the post waits out the last
    message of it, not the first."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    await seed_send(sends, at(time(9, 0)), text="una")
    await seed_send(sends, at(time(9, 20)), text="dos")
    clock = ManualClock(at(time(9, 21)))
    scheduler, _ = make_scheduler(StubLeg(), clock, plans, sends)

    await scheduler.step()

    assert clock.now() >= at(time(9, 30))


async def test_a_conversation_that_keeps_going_eventually_drops_the_slot(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """If the deferral pushes the post more than about thirty minutes past its
    window edge, the slot is given up rather than posted late."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(10, 25)),)))
    await seed_send(sends, at(time(10, 55)))
    clock = ManualClock(at(time(10, 56)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.DROPPED
    assert leg.calls == []


async def test_the_deferral_is_drawn_once_per_message_not_once_per_wait(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """A delay redrawn on every pass of the loop would be the *maximum* of its
    draws, because a longer draw always pushes the answer out again - so "ten to
    twenty minutes" would settle near twenty. One draw per message, one wait."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    await seed_send(sends, at(time(9, 13)))
    clock = ManualClock(at(time(9, 14)))
    scheduler, sleeper = make_scheduler(StubLeg(), clock, plans, sends)

    await scheduler.step()

    assert len(sleeper.slept) == 1


async def test_a_late_slot_is_never_deferred_into_the_overnight_hold(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """The 30 minutes of grace stop at 23:00, where the anti-ban hold starts. The
    pacer would refuse the send anyway; dropping it here means the day does not
    pay DeepSeek to write a post that was never going out."""
    await plans.register(
        DayPlan(day=WEDNESDAY, slots=(slot(time(22, 50), window="late", closes=time(23, 0)),))
    )
    await seed_send(sends, at(time(22, 55)))
    clock = ManualClock(at(time(22, 56)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.DROPPED
    assert leg.calls == []


async def test_a_late_slot_inside_its_own_window_still_posts(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """The cap is on the grace, not on the window: 22:59 is a time Rebe posts at."""
    await plans.register(
        DayPlan(day=WEDNESDAY, slots=(slot(time(22, 59), window="late", closes=time(23, 0)),))
    )
    clock = ManualClock(at(time(22, 59)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    assert leg.calls == [(GROUP, 1)]


async def test_a_message_from_long_before_the_slot_defers_nothing(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """The normal case: her last message was hours ago, so the slot posts on time."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    await seed_send(sends, at(time(8, 0)))
    clock = ManualClock(at(time(9, 14)))
    scheduler, sleeper = make_scheduler(StubLeg(), clock, plans, sends)

    await scheduler.step()

    assert clock.now() == at(time(9, 14))
    assert sleeper.slept == []


# --- what the envelope and the transport can say -----------------------------


async def test_a_refused_send_drops_the_slot_rather_than_hammering_the_envelope(
    plans: InMemoryPlanStore, sends: InMemorySendLog, caplog: pytest.LogCaptureFixture
) -> None:
    """The pacer already told the caller how long the door is shut; a slot that
    cannot go out inside its own window is one a person would simply not send."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    refusal = SendRefusedError(
        RefusalReason.MINIMUM_GAP, "the last post was 20m ago", retry_after=timedelta(minutes=55)
    )
    clock = ManualClock(at(time(9, 14)))
    leg = StubLeg(refusal)
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    with caplog.at_level(logging.INFO):
        await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.DROPPED
    assert len(leg.calls) == 1
    assert "minimum_gap" in caplog.text


async def test_a_transport_failure_does_not_take_the_day_down_with_it(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Evolution being down at 09:14 costs the morning slot, not the loop."""
    await plans.register(
        DayPlan(
            day=WEDNESDAY,
            slots=(slot(time(9, 14)), slot(time(13, 47), window="midday", closes=time(15, 0))),
        )
    )
    clock = ManualClock(at(time(9, 14)))
    leg = StubLeg(EvolutionError("bien-evo is not answering"), [POSTED_ITEM])
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()
    await scheduler.step()
    await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert [s.state for s in plan.slots] == [SlotState.DROPPED, SlotState.POSTED]


# --- a restart part-way through the day --------------------------------------


async def test_a_restart_keeps_the_remaining_slots_and_does_not_redraw_them(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """The plan is in the database, so the process that comes back is the same bot
    part-way through its day rather than a new one with new habits."""
    clock = ManualClock(at(DAWN))
    first, _ = make_scheduler(StubLeg(), clock, plans, sends)
    await first.step()
    rolled = await plans.plan_on(WEDNESDAY)
    assert rolled is not None

    restarted_clock = ManualClock(at(time(12, 0)))
    leg = StubLeg()
    restarted, _ = make_scheduler(leg, restarted_clock, plans, sends, seed=SEED + 1)
    await restarted.step()

    after = await plans.plan_on(WEDNESDAY)
    assert after is not None
    assert [s.at for s in after.slots] == [s.at for s in rolled.slots]
    assert leg.calls == [], "the restart landed between two slots, so it waited"


async def test_a_restart_does_not_repost_a_slot_that_already_went_out(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    await plans.register(
        DayPlan(
            day=WEDNESDAY,
            slots=(slot(time(9, 14)), slot(time(13, 47), window="midday", closes=time(15, 0))),
        )
    )
    clock = ManualClock(at(time(9, 14)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)
    await scheduler.step()

    restarted, _ = make_scheduler(leg, ManualClock(at(time(9, 30))), plans, sends)
    await restarted.step()

    assert len(leg.calls) == 1, "the second process found the morning slot already settled"


async def test_a_second_roll_of_the_same_day_cannot_double_register_it(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Two rolls of the same day would each be a valid plan and together a burst."""
    clock = ManualClock(at(DAWN))
    scheduler, _ = make_scheduler(StubLeg(), clock, plans, sends)
    await scheduler.step()
    rolled = await plans.plan_on(WEDNESDAY)
    assert rolled is not None

    again = DayPlan(day=WEDNESDAY, slots=(slot(time(9, 59)),))
    of_record = await plans.register(again)

    assert of_record.slots == rolled.slots


# --- a whole day, one step at a time -----------------------------------------


async def test_a_whole_weekday_runs_itself_from_dawn(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Every leg of the ticket in one pass: the roll, four one-shot slots firing in
    order at their drawn times, and the loop settling down to wait for tomorrow."""
    clock = ManualClock(at(time(5, 0)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    posted_at: list[datetime] = []
    for _ in range(11):
        before = len(leg.calls)
        await scheduler.step()
        if len(leg.calls) > before:
            posted_at.append(clock.now())

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert len(leg.calls) == 4
    assert posted_at == [s.at for s in plan.slots]
    assert all(s.state is SlotState.POSTED for s in plan.slots)
    assert clock.now() == at(DAWN, THURSDAY)


async def test_a_weekend_day_runs_two_slots_not_four(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    saturday = date(2026, 7, 25)
    clock = ManualClock(moment_on(saturday, DAWN, MEXICO_CITY))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    for _ in range(5):
        await scheduler.step()

    plan = await plans.plan_on(saturday)
    assert plan is not None
    assert [s.window for s in plan.slots] == ["midday", "evening"]
    assert len(leg.calls) == 2


async def test_a_cadence_of_one_window_is_still_a_day(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """The ramp in section 1 clamps the day to three posts in week one, so the
    window set is a parameter the loop reads rather than a constant it assumes."""
    single = Cadence(weekday=(PostWindow("solo", time(9, 0), time(10, 0)),))
    clock = ManualClock(at(DAWN))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends, cadence=single)

    for _ in range(3):
        await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert [s.window for s in plan.slots] == ["solo"]
    assert len(leg.calls) == 1


# --- looking up from the plan for breaking news ------------------------------


async def test_a_long_wait_is_cut_short_for_a_look_at_the_news(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """A day that only ever woke at its own drawn times could not notice a launch
    at 16:30, which is the restriction section 4 exists to remove."""
    await plans.register(
        DayPlan(day=WEDNESDAY, slots=(slot(time(19, 2), window="evening", closes=time(20, 0)),))
    )
    clock = ManualClock(at(time(14, 0)))
    watch = StubWatch(clock)
    scheduler, _ = make_scheduler(StubLeg(), clock, plans, sends, breaking=watch)

    await scheduler.step()

    assert len(watch.checks) == 1
    assert at(time(14, 20)) <= watch.checks[0] <= at(time(14, 40))
    assert clock.now() < at(time(19, 2)), "the slot is still ahead; only the wait was cut"


async def test_the_loop_comes_straight_back_to_the_same_wait(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """The look is a turn of the loop, not a detour: the slot it was waiting for
    is still the next thing to happen."""
    await plans.register(
        DayPlan(day=WEDNESDAY, slots=(slot(time(19, 2), window="evening", closes=time(20, 0)),))
    )
    clock = ManualClock(at(time(18, 0)))
    watch = StubWatch(clock)
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends, breaking=watch)

    for _ in range(6):
        await scheduler.step()

    assert clock.now() >= at(time(19, 2))
    assert leg.calls == [(GROUP, 1)]
    assert watch.checks, "and it looked at the news on the way"


async def test_a_wait_shorter_than_a_look_is_simply_waited(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Nothing is gained by waking ten minutes before a slot to check the news."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    clock = ManualClock(at(time(9, 10)))
    watch = StubWatch(clock)
    scheduler, _ = make_scheduler(StubLeg(), clock, plans, sends, breaking=watch)

    await scheduler.step()

    assert watch.checks == []
    assert clock.now() == at(time(9, 14))


async def test_the_night_is_watched_too(plans: InMemoryPlanStore, sends: InMemorySendLog) -> None:
    """Section 6 only works if something is looking: the overnight queue can only
    hold what the process noticed before dawn."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    await plans.settle(WEDNESDAY, "morning", SlotState.POSTED)
    clock = ManualClock(at(time(23, 30)))
    watch = StubWatch(clock)
    scheduler, _ = make_scheduler(StubLeg(), clock, plans, sends, breaking=watch)

    await scheduler.step()

    assert len(watch.checks) == 1
    assert clock.now() < at(DAWN, THURSDAY)


async def test_a_loop_with_no_override_wired_waits_exactly_as_long_as_it_planned(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Which is what keeps the scheduler's own behaviour a property of the plan."""
    await plans.register(
        DayPlan(day=WEDNESDAY, slots=(slot(time(19, 2), window="evening", closes=time(20, 0)),))
    )
    clock = ManualClock(at(time(14, 0)))
    scheduler, _ = make_scheduler(StubLeg(), clock, plans, sends)

    await scheduler.step()

    assert clock.now() == at(time(19, 2))


async def test_a_drawn_slot_stops_at_eight_posts_like_everything_else(
    plans: InMemoryPlanStore, sends: InMemorySendLog, caplog: pytest.LogCaptureFixture
) -> None:
    """The practical stop bounds the day, not one path into it. Only a day the
    overrides ran long can reach it - four drawn slots cannot - and that is the
    day that most needs somebody to stop rather than drift at the ceiling."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    for hour in range(8):
        await seed_send(sends, at(time(hour, 0)), text=f"la numero {hour}", kind=SendKind.POST)
    clock = ManualClock(at(time(9, 14)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    with caplog.at_level(logging.INFO):
        await scheduler.step()

    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.DROPPED
    assert leg.calls == []
    assert "where a normal day stops" in caplog.text


async def test_seven_posts_still_leaves_room_for_the_eighth(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    for hour in range(7):
        await seed_send(sends, at(time(hour, 0)), text=f"la numero {hour}", kind=SendKind.POST)
    clock = ManualClock(at(time(9, 14)))
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends)

    await scheduler.step()

    assert leg.calls == [(GROUP, 1)]


# --- a slot that the overnight queue takes -----------------------------------


async def test_a_slot_asks_the_overnight_queue_before_its_own_pool(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """Section 6: what broke while she slept goes out ahead of everything else in
    the queue, and the morning slot is where it goes."""
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    clock = ManualClock(at(time(9, 14)))
    watch = StubWatch(clock, claims=POSTED_ITEM)
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends, breaking=watch)

    await scheduler.step()

    assert watch.claims == 1
    assert leg.calls == [], "the slot was already spoken for"
    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.POSTED


async def test_a_slot_with_nothing_waiting_posts_from_the_pool_as_usual(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    clock = ManualClock(at(time(9, 14)))
    watch = StubWatch(clock)
    leg = StubLeg()
    scheduler, _ = make_scheduler(leg, clock, plans, sends, breaking=watch)

    await scheduler.step()

    assert watch.claims == 1
    assert leg.calls == [(GROUP, 1)]


# --- the real news leg, driven by the real scheduler -------------------------


async def test_a_due_slot_drives_the_news_leg_all_the_way_into_the_group(
    plans: InMemoryPlanStore, sends: InMemorySendLog
) -> None:
    """The scheduler fires the leg from #18 as it is: fetch, curate, one DeepSeek
    call, then the shared pacer. Only DeepSeek, Evolution and the feeds are
    stand-ins, and the pacer writes to the same send log the deferral reads."""
    settings: Settings = load_settings(dict(COMPLETE_ENV))
    clock = ManualClock(at(time(9, 14)))
    sleeper = ManualSleeper(clock)
    evolution = FakeEvolution()
    launch = item(
        source="openai",
        source_id="openai-1",
        title="OpenAI lanza un modelo que corre local",
        url="https://openai.com/index/local-model",
        published_at=at(time(7, 30)),
    )
    fake = FakeDeepSeek(
        json_output_response(
            '{"for_the_group": true, "text": "miren, salio un modelo que corre local"}'
        )
    )

    class OnePool:
        async def fetch(self, now: datetime) -> Sequence[object]:
            return [launch]

    leg = NewsLeg(
        build_brain(
            settings, clock, InMemoryUsageStore(), RecordingAlerter(), http_client=fake.client()
        ),
        Pacer(
            EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
            sends,
            clock,
            sleeper=sleeper,
            rng=random.Random(SEED),
        ),
        OnePool(),  # type: ignore[arg-type]
        InMemoryPostedStore(),
        clock,
    )
    await plans.register(DayPlan(day=WEDNESDAY, slots=(slot(time(9, 14)),)))
    scheduler = Scheduler(leg, GROUP, plans, sends, clock, sleeper=sleeper, rng=random.Random(SEED))

    await scheduler.step()

    assert evolution.texts == [
        "miren, salio un modelo que corre local\nhttps://openai.com/index/local-model"
    ]
    plan = await plans.plan_on(WEDNESDAY)
    assert plan is not None
    assert plan.slots[0].state is SlotState.POSTED
    latest = await sends.latest()
    assert latest is not None and latest.kind is SendKind.POST
