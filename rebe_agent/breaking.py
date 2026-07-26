"""When something genuinely big happens, Rebe posts it anyway.

Sections 4 and 6 of `docs/wayfinder/posting-cadence-spec.md`, as one object that
the scheduler asks two questions.

**"Has anything broken?"** - `check`, run every twenty to forty minutes between
the day's drawn slots. It looks at the same curated pool the news leg posts from,
classifies it, and if the best item is high tier it goes out now, on top of the
day. Additive, per section 4: four posts become five, not four with something
displaced. Delaying real news to the next window is exactly the restriction this
rule exists to avoid.

**"Is anything waiting from last night?"** - `claim_slot`, asked as a drawn slot
comes due. Anything that broke between 23:00 and 08:00 was queued rather than
posted, and the strongest of it takes the morning slot ahead of everything else
in the queue. The rest fall back to normal tier and compete for the later windows
like any other candidate.

Three things this module deliberately does not do.

**It does not send.** Every post leaves through the same `NewsLeg.post_one` the
scheduled slots use, which means the same DeepSeek call, the same anti-repost
store and the same shared `Pacer` - so the override obeys the minimum gap, the
hourly ceiling and the overnight hold like everything else. A high-tier item is
news that jumps *the plan*, never news that jumps the envelope. When the pacer
says no, the item stays unposted and the next look tries again: "as soon as
pacing allows" is a loop, not an exemption.

**It does not rank or filter.** `rebe_agent.curate` decides what is worth
posting and `rebe_agent.tiers` decides what is big; this decides what to do about
it.

**It does not draw the day.** The plan belongs to `rebe_agent.cadence`. An
override writes itself into that plan afterwards as an extra, already-posted
slot, so the day's record says five posts happened and which of them was the
override - and so a restart at 17:00 can see it.

The one number that is this module's own is the practical stop: eight posts in a
local day, counting both tiers. The anti-ban envelope's twelve is the absolute
one, and the gap between them is the point - the ceiling exists to catch a
runaway loop, not to shape a normal day.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime

from rebe_agent.brain import BrainError
from rebe_agent.cadence import (
    WAKING_CLOSES,
    WAKING_OPENS,
    Cadence,
    DayPlan,
    Slot,
    SlotState,
    minutes,
)
from rebe_agent.clock import Clock, local_day
from rebe_agent.curate import score
from rebe_agent.evolution import EvolutionError
from rebe_agent.items import NewsItem
from rebe_agent.news import NewsLeg, Posted, PostRejectedError
from rebe_agent.overnight import OvernightQueue, source_keys
from rebe_agent.pacer import SendRefusedError
from rebe_agent.plans import PlanStore
from rebe_agent.sends import SendKind, SendLog
from rebe_agent.tiers import DEFAULT_BAR, Tier, TierBar, classify, weak

logger = logging.getLogger("rebe_agent.breaking")

OVERRIDE_WINDOW = "breaking"
"""What an override slot is called in the plan, before its number.

Numbered rather than dated because the plan's unique key is `(day, window)`, so
two big stories on one Tuesday need two names - and a name a human reading the
table understands is worth more than an opaque one.
"""


class Breaking:
    """The high-tier override: the day's plan, and what outranks it."""

    def __init__(
        self,
        leg: NewsLeg,
        chat: str,
        plans: PlanStore,
        sends: SendLog,
        queue: OvernightQueue,
        clock: Clock,
        *,
        cadence: Cadence | None = None,
        bar: TierBar | None = None,
    ) -> None:
        self._leg = leg
        self._chat = chat
        self._plans = plans
        self._sends = sends
        self._queue = queue
        self._clock = clock
        self._cadence = cadence or Cadence()
        self._bar = bar or DEFAULT_BAR

    async def check(self) -> Posted | None:
        """One look at the pool: post the big thing, hold it for morning, or nothing.

        Never raises for a refusal or a transport failure. This runs between the
        day's slots, where there is nothing to settle and nobody to tell: an item
        that could not go out this time is still in the pool next time, which is
        what "as soon as pacing allows" means in a loop that keeps looking.
        """
        now = self._clock.now()
        candidates = await self._leg.unposted(now)
        # The night's items are not breaking news to this path, whatever tier they
        # would classify as. One is holding the morning slot and the rest were
        # demoted when it went; posting either here would be the override leg
        # taking back a decision section 6 gave to the morning window - and would
        # put the night's story out at 08:20 rather than at a jittered time inside
        # the morning window, which is the tell the whole rule exists to avoid.
        spoken_for = source_keys(await self._queue.held())
        big = [
            item
            for item in candidates
            if item.source_key not in spoken_for and self._tier(item) is Tier.HIGH
        ]
        if not big:
            return None

        if self._asleep(now):
            await self._hold(big, now)
            return None

        if not await self._may_post(now):
            return None

        try:
            posted = await self._leg.post_one(self._chat, big[0])
        except SendRefusedError as exc:
            # The envelope, not the transport: the item keeps its tier and the
            # next look asks again. Nothing is dropped and nothing is queued -
            # queueing a daytime item would turn a gap of forty minutes into a
            # wait for tomorrow morning.
            logger.info("holding %s until the envelope opens: %s", big[0].canonical_url, exc)
            return None
        except (BrainError, PostRejectedError, EvolutionError) as exc:
            # A brain that gave no answer, an answer Rebe would not send, a
            # transport that is down. All three are the same decision here: one
            # failed override is worth far less than the loop that would have made
            # the next one, and every one of them has already been reported by
            # whoever saw it. The next look tries again.
            logger.warning("the breaking post did not get out: %s", exc)
            return None

        # The moment it landed, not the moment the look began: the pacer spends a
        # typing pause and can hold for the per-minute floor in between, and the
        # day's record should say when the group saw it.
        landed = posted.message.at
        logger.info(
            "%s is high tier and went out on top of the day at %s",
            posted.item.canonical_url,
            landed.strftime("%H:%M"),
        )
        day = local_day(landed, self._clock.zone)
        await self._record(day, landed)
        await self._prune(day, landed, [item for item in candidates if item is not posted.item])
        return posted

    async def claim_slot(self) -> Posted | None:
        """Post what broke overnight, if anything is still waiting for a window.

        Called as a drawn slot comes due, which is why nothing here checks the
        hour: the first slot of the day is in the morning window by construction,
        and the queue draining at a later one is a morning that went wrong rather
        than a rule that needs restating.

        A refusal and a transport failure are raised, so the slot that called this
        settles exactly the way it would have for a normal post. A brain that gave
        no answer is not: the scheduled path answers that with an empty run and a
        skipped slot, and this returns `None` so the slot falls back to its own
        pool rather than ending the day's loop on an exception it never sees.

        The queue is demoted once the morning has been decided, one way or the
        other: something went out, or nothing in it was worth a slot any more.
        Neither of those is true of a send that was refused or that broke, so
        those leave the night's story waiting for the next window.
        """
        waiting = await self._queue.waiting()
        if not waiting:
            return None

        now = self._clock.now()
        # Through the curator again, on this morning's clock: a queued item that
        # went stale overnight, or that has since been posted by a normal slot,
        # is not the thing that was worth holding.
        ranked = await self._leg.fresh(waiting, now)
        if not ranked:
            logger.info("nothing in the overnight queue is still worth the morning slot")
            await self._queue.demote()
            return None

        try:
            posted = await self._leg.post_one(self._chat, ranked[0])
        except (BrainError, PostRejectedError) as exc:
            logger.warning("the overnight item could not be written: %s", exc)
            return None

        logger.info(
            "the overnight queue took this slot with %s, held %s; %d other item(s) "
            "fall back to normal tier",
            posted.item.canonical_url,
            minutes(now - posted.item.published_at),
            len(ranked) - 1,
        )
        await self._queue.demote()
        return posted

    def _tier(self, item: NewsItem) -> Tier:
        return classify(item, filters=self._leg.filters, bar=self._bar)

    def _asleep(self, now: datetime) -> bool:
        """Is it one of the hours nothing is posted in?

        The same 23:00 to 08:00 the plan is drawn inside and the pacer refuses
        outside, checked here for the third time and deliberately so: this one
        stops the day paying DeepSeek to write a post the pacer was always going
        to refuse, and turns the refusal into a queued item instead of a lost one.
        """
        local = now.astimezone(self._clock.zone).time()
        return not (WAKING_OPENS <= local < WAKING_CLOSES)

    async def _hold(self, big: Sequence[NewsItem], now: datetime) -> None:
        """Queue the night's high-tier items, all of them, for the morning to sort.

        All of them rather than only the best, because "best" at 23:40 and "best"
        at 04:10 are different questions and neither is the one that matters. The
        morning asks it once, on its own clock.
        """
        for item in big:
            await self._queue.queue(item, now)
        logger.info(
            "%d high-tier item(s) broke at %s; holding them for the morning window",
            len(big),
            now.strftime("%H:%M"),
        )

    async def _may_post(self, now: datetime) -> bool:
        """The two reasons an override waits that the pacer knows nothing about."""
        posts = await self._posts_today(now)
        if posts >= self._cadence.daily_stop:
            logger.info(
                "%d posts today is the practical stop; the big story waits rather "
                "than pushing the day at the anti-ban ceiling",
                posts,
            )
            return False

        resume = await self._deferred_until(now)
        if resume is not None:
            # Not a sleep: the next look comes round in twenty to forty minutes
            # anyway, and a loop holding a lock on the news for ten of them would
            # be the one thing a conversation should never wait behind.
            logger.info(
                "Rebe is mid-conversation until %s; the big story waits for the next look",
                resume.strftime("%H:%M"),
            )
            return False
        return True

    async def _posts_today(self, now: datetime) -> int:
        """How many posts have gone out on the local day, both tiers together.

        Posts and not sends: the practical stop is about how much of the group's
        day is Rebe sharing links, while replies are somebody else starting the
        conversation. The envelope counts them together, and that is its job.
        """
        return await self._sends.count_on(local_day(now, self._clock.zone), kind=SendKind.POST)

    async def _deferred_until(self, now: datetime) -> datetime | None:
        """When a post may follow her last message of any kind, or `None` for now.

        Section 5's rule, off the same `Cadence` the drawn slots read it from, so
        a big story and an ordinary one wait out the same conversation for the
        same length of time.
        """
        last = await self._sends.latest()
        if last is None:
            return None
        resume = self._cadence.resume_after(last)
        return resume if resume > now else None

    async def _record(self, day: date, now: datetime) -> None:
        """Write the override into the day as an extra slot that already posted.

        The plan is the record of the day, so a day that went to five posts has to
        say five. `closes` is the moment itself: an override has no window to
        drift past, having never been drawn into one.

        This can register a day the roll has not reached yet only if an override
        ever posts before 06:00, and it cannot: everything before 08:00 is queued
        rather than posted, and the roll runs two hours before that. The ordering
        is what keeps a day from ever looking rolled when all it has is this row.
        """
        plan = await self._plans.plan_on(day)
        taken = (
            sum(1 for slot in plan.slots if slot.window.startswith(OVERRIDE_WINDOW)) if plan else 0
        )
        await self._plans.register(
            DayPlan(
                day=day,
                slots=(
                    Slot(
                        window=f"{OVERRIDE_WINDOW}-{taken + 1}",
                        at=now,
                        closes=now,
                        state=SlotState.POSTED,
                        tier=Tier.HIGH,
                    ),
                ),
            )
        )

    async def _prune(self, day: date, now: datetime, candidates: Sequence[NewsItem]) -> None:
        """Give up the remaining slots whose best available candidate is weak.

        Each remaining slot is paired with the candidate it would actually get -
        the first slot takes the best item left, the second takes the next - and a
        slot pointed at a mediocre link, or at nothing, is pruned. A person who
        just shared the big thing does not also drop a roundup at 22:00; a person
        who has a second good link still shares it.

        High-tier slots are never pruned, per section 4. In a normal day none of
        them is pending - an override is written down already posted - so this is
        the rule stated where it can be read rather than a branch that fires.
        """
        plan = await self._plans.plan_on(day)
        if plan is None:
            return

        remaining = [slot for slot in plan.pending if slot.tier is not Tier.HIGH]
        for index, slot in enumerate(remaining):
            candidate = candidates[index] if index < len(candidates) else None
            if candidate is not None and not weak(
                candidate, now, ranking=self._leg.ranking, bar=self._bar
            ):
                continue
            logger.info(
                "pruning the %s slot: the day already had its big story and the best "
                "thing left for it is %s",
                slot.window,
                f"{candidate.canonical_url} at {score(candidate, now, self._leg.ranking):.2f}"
                if candidate is not None
                else "nothing at all",
            )
            await self._plans.settle(day, slot.window, SlotState.PRUNED)
