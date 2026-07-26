"""The scheduler leg: one job at dawn, and one shot per slot it drew.

Section 2.2 of `docs/wayfinder/deployment-architecture-spec.md` gives this leg one
job - fire the news leg on a timer - and section 3 of the cadence spec says when:
a single roll at about 06:00 draws the day, and every post that follows is a
one-shot at a drawn time. Nothing here is on a cron expression except the roll,
because a fixed offset is the rhythm the whole spec exists to avoid.

**The loop is `sleep until the next thing, then do it`.** The spec names
APScheduler, and this is that shape without the dependency: the repo already has
the seam that makes waiting testable (`Clock` and `Sleeper`), a scheduler built on
it can be driven through a whole day inside a test, and the "one-shot jobs" it
registers are rows in the `rebe` database rather than objects in a job store -
which is what makes a restart part-way through a day pick the day back up instead
of losing it. An in-memory job store would lose exactly the thing this leg cannot
afford to lose.

**One replica, still.** The pacer's counters and this loop's idea of what is due
both live in-process; two replicas would each roll their own day and double-fire
every slot. The deployment spec pins `rebe-agent` to one replica for the pacer's
sake, and this leg inherits that. The plan table would survive a second replica -
its unique `(day, window)` is what stops two rolls becoming eight slots - but two
processes firing the same slot would still send twice, so the invariant stands.

**Three ways a slot can end.** It posts; it is *skipped* because nothing in the
curated pool cleared the quality bar, which is the news leg's judgement and not
this module's; or it is *dropped*, because it came due too far past its window,
because a conversation deferred it past that edge, or because the envelope refused
it. A dropped slot is never retried into the next window: the day's shape is the
point, and a post that arrives two hours late is a machine catching up.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Protocol

from rebe_agent.cadence import (
    DAWN,
    Cadence,
    DayPlan,
    Slot,
    SlotState,
    draw_plan,
    minutes,
    moment_on,
    spread,
)
from rebe_agent.clock import Clock, RealSleeper, Sleeper
from rebe_agent.evolution import EvolutionError
from rebe_agent.news import Posted
from rebe_agent.pacer import SendRefusedError
from rebe_agent.plans import PlanStore
from rebe_agent.sends import SendLog

logger = logging.getLogger("rebe_agent.scheduler")

DAY = timedelta(days=1)


class Poster(Protocol):
    """What a slot fires: the news leg from #18, or a stand-in in a test.

    A structural type rather than the class itself, so this module schedules the
    news leg without owning it - and so a test of the loop does not have to stand
    up a brain and a transport to watch a slot come due.
    """

    async def run(self, chat: str, *, limit: int = 1) -> Sequence[Posted]:
        """Post up to `limit` fresh items, best first. An empty answer is a skip."""


class Scheduler:
    """The bot that runs itself: draw the day at dawn, then keep the appointments."""

    def __init__(
        self,
        poster: Poster,
        chat: str,
        plans: PlanStore,
        sends: SendLog,
        clock: Clock,
        *,
        cadence: Cadence | None = None,
        sleeper: Sleeper | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._poster = poster
        self._chat = chat
        self._plans = plans
        self._sends = sends
        self._clock = clock
        self._cadence = cadence or Cadence()
        self._sleeper = sleeper or RealSleeper()
        self._rng = rng or random.Random()

    async def serve(self) -> None:
        """Sleep, wake, do the one thing that is due, repeat, until cancelled.

        A failure the two documented exits below do not cover - the database, say -
        ends the loop and with it the process, on purpose: the platform restarts
        it, and a restart is safe here precisely because the plan is not in memory.
        """
        logger.info("scheduler leg up; the day is rolled at %s local", DAWN.strftime("%H:%M"))
        while True:
            await self.step()

    async def step(self) -> None:
        """Wait for the next due thing, then do it. Exactly one turn of the loop.

        One turn is one of four things: wait for dawn, roll the day, wait for a
        slot, or fire one. Keeping them one to a turn is what lets a test drive a
        whole day and read what happened at each step.
        """
        now = self._clock.now()
        today = self._local_day(now)
        plan = await self._plans.plan_on(today)

        if plan is None:
            dawn = self._dawn_on(today)
            if now < dawn:
                await self._sleep_until(dawn, "the day's roll")
                return
            await self._roll(today, now)
            return

        due = plan.pending
        if not due:
            await self._sleep_until(self._dawn_on(today + DAY), "tomorrow's roll")
            return

        slot = due[0]
        if slot.at > now:
            await self._sleep_until(slot.at, f"the {slot.window} slot")
            return
        await self._fire(plan.day, slot)

    async def _roll(self, day: date, now: datetime) -> None:
        """Draw the day and register a one-shot for each drawn time.

        A roll that runs late - the process was down at 06:00 - keeps only the
        times still ahead of it. Posting at a time that has already been and gone
        is not the habit the plan was drawn to imitate.
        """
        drawn = draw_plan(day, zone=self._clock.zone, rng=self._rng, cadence=self._cadence)
        ahead = tuple(slot for slot in drawn.slots if slot.at > now)
        missed = len(drawn.slots) - len(ahead)
        if missed:
            logger.info(
                "the roll for %s ran %s late; %d drawn time(s) were already past",
                day.isoformat(),
                minutes(now - self._dawn_on(day)),
                missed,
            )

        if not ahead:
            logger.info("no window is left on %s; waiting for the next roll", day.isoformat())
            await self._sleep_until(self._dawn_on(day + DAY), "tomorrow's roll")
            return

        plan = await self._plans.register(DayPlan(day=day, slots=ahead))
        logger.info(
            "registered %d slot(s) for %s: %s",
            len(plan.slots),
            day.isoformat(),
            ", ".join(f"{slot.window} at {slot.at:%H:%M}" for slot in plan.slots),
        )

    async def _fire(self, day: date, slot: Slot) -> None:
        """One slot, from due to settled: defer, drop, or post exactly once."""
        deadline = self._cadence.deadline_for(slot, self._clock.zone)
        # One deferral per message she sent, remembered across the waits below.
        drawn: dict[datetime, timedelta] = {}
        while True:
            now = self._clock.now()
            if now >= deadline:
                await self._drop(
                    day,
                    slot,
                    f"it is {now:%H:%M} and the window closed at {slot.closes:%H:%M}",
                )
                return

            resume = await self._deferred_until(now, drawn)
            if resume is None:
                break
            if resume >= deadline:
                await self._drop(
                    day,
                    slot,
                    f"Rebe is mid-conversation until {resume:%H:%M}, past what a "
                    f"{slot.closes:%H:%M} window can be stretched to",
                )
                return
            logger.info(
                "deferring the %s slot to %s: Rebe was talking to somebody",
                slot.window,
                resume.strftime("%H:%M"),
            )
            await self._sleep_until(resume, f"the {slot.window} slot")

        try:
            sent = await self._poster.run(self._chat, limit=1)
        except SendRefusedError as exc:
            # The envelope, not the transport: nothing was sent and nothing is
            # broken. Retrying inside the window would be hammering a door the
            # pacer already said is shut, so the slot goes rather than the post.
            await self._drop(day, slot, str(exc))
            return
        except EvolutionError as exc:
            # The transport. One slot is worth less than the loop, so this is a
            # dropped slot and a warning rather than a dead process.
            logger.warning("the %s slot did not get out: %s", slot.window, exc)
            await self._plans.settle(day, slot.window, SlotState.DROPPED)
            return

        if not sent:
            logger.info(
                "nothing cleared the quality bar for the %s window; posting nothing", slot.window
            )
            await self._plans.settle(day, slot.window, SlotState.SKIPPED)
            return

        logger.info(
            "the %s slot posted %s",
            slot.window,
            ", ".join(post.item.canonical_url for post in sent),
        )
        await self._plans.settle(day, slot.window, SlotState.POSTED)

    async def _deferred_until(
        self, now: datetime, drawn: dict[datetime, timedelta]
    ) -> datetime | None:
        """When a post may follow her last message, or `None` if it may go now.

        Her last message of *any* kind, post or reply, per section 5: what looks
        like two programs is a link landing on top of a conversation, and the
        conversation is whichever leg was talking.

        One draw per message, remembered in `drawn`, and a fresh one only when she
        has said something new - which is the same discipline the pacer keeps for
        the opposite reason. Redrawing on every pass would hand the wait the
        *maximum* of its draws, since a longer draw always pushes the answer out
        again, and "ten to twenty minutes" would settle near twenty.
        """
        last = await self._sends.latest()
        if last is None:
            return None
        delay = drawn.get(last.sent_at)
        if delay is None:
            delay = spread(*self._cadence.defer, self._rng.random())
            drawn[last.sent_at] = delay
        resume = last.sent_at + delay
        return resume if resume > now else None

    async def _drop(self, day: date, slot: Slot, why: str) -> None:
        logger.info("dropping the %s slot: %s", slot.window, why)
        await self._plans.settle(day, slot.window, SlotState.DROPPED)

    async def _sleep_until(self, target: datetime, what: str) -> None:
        seconds = (target - self._clock.now()).total_seconds()
        if seconds <= 0:
            return
        logger.debug("waiting %s for %s", minutes(timedelta(seconds=seconds)), what)
        await self._sleeper.sleep(seconds)

    def _dawn_on(self, day: date) -> datetime:
        return moment_on(day, DAWN, self._clock.zone)

    def _local_day(self, moment: datetime) -> date:
        """The day in the agent's zone. A plan is about the group's day."""
        return moment.astimezone(self._clock.zone).date()
