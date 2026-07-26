"""Big news on top of the day, and what breaks at 03:00 waiting for the morning.

Sections 4 and 6 of the cadence spec, driven through the real news leg: the real
curator, the real ranker, the real anti-repost store and the real shared pacer.
Only DeepSeek, Evolution and the feed layer are stand-ins, because the whole
claim of this ticket is that an override is a *normal post that jumped the plan* -
and a stub leg could not prove it obeys the envelope on the way out.

Nothing here waits on real time. The clock is moved by hand and the pacer sleeps
through a `ManualSleeper` that moves it instead of the world.

The pools are built from the recorded payloads in `tests/fixtures` wherever the
test is about what an item *is* - which tier it lands in, whether it is worth a
slot - so those judgements are made about the shapes the real sources return.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pytest

from rebe_agent.brain import build_brain
from rebe_agent.breaking import Breaking
from rebe_agent.cadence import Cadence, DayPlan, Slot, SlotState, moment_on
from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.config import load_settings
from rebe_agent.evolution import EvolutionClient
from rebe_agent.feeds import FEEDS, Feed, feed_items, hacker_news_items
from rebe_agent.items import NewsItem
from rebe_agent.news import NewsLeg
from rebe_agent.overnight import InMemoryOvernightQueue
from rebe_agent.pacer import Envelope, Pacer
from rebe_agent.plans import InMemoryPlanStore
from rebe_agent.posted import InMemoryPostedStore
from rebe_agent.scheduler import Scheduler
from rebe_agent.sends import InMemorySendLog, SendKind, SendRecord, fingerprint
from rebe_agent.tiers import Tier
from rebe_agent.usage import InMemoryUsageStore
from tests.deepseek_stub import FakeDeepSeek, tool_call_response
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, MEXICO_CITY, RecordingAlerter, fixture, item
from tests.test_config import COMPLETE_ENV

SEED = 20260725

SATURDAY = date(2026, 7, 25)
"""The day the fixtures were recorded, so their timestamps mean what they say."""

SUNDAY = date(2026, 7, 26)
"""The morning after, for everything that breaks while she is asleep."""

AFTERNOON = time(16, 30)
"""Mid-afternoon, between two windows: the hour section 4's worked example uses."""


def at(moment: time, day: date = SATURDAY) -> datetime:
    return moment_on(day, moment, MEXICO_CITY)


def slot(due: time, *, window: str, closes: time, day: date = SATURDAY, **kwargs: object) -> Slot:
    return Slot(window=window, at=at(due, day), closes=at(closes, day), **kwargs)  # type: ignore[arg-type]


EVENING = slot(time(19, 30), window="evening", closes=time(20, 0))
LATE = slot(time(21, 45), window="late", closes=time(23, 0))


# --- the pools, read off the recorded payloads -------------------------------


def feed_named(key: str) -> Feed:
    return next(feed for feed in FEEDS if feed.key == key)


def from_feed(name: str, key: str, source_id: str) -> NewsItem:
    found = [
        candidate
        for candidate in feed_items(feed_named(key), fixture(name))
        if candidate.source_id == source_id
    ]
    assert len(found) == 1, f"expected exactly one {source_id}, got {len(found)}"
    return found[0]


def from_hn(name: str, source_id: str) -> NewsItem:
    found = [
        candidate
        for candidate in hacker_news_items(json.loads(fixture(name)))
        if candidate.source_id == source_id
    ]
    assert len(found) == 1, f"expected exactly one {source_id}, got {len(found)}"
    return found[0]


TOP_OF_HN = from_hn("hn_top_story.json", "42000001")
"""1,420 points: high tier by the points bar alone."""

ORDINARY = from_hn("hn_top_story.json", "42000002")
"""150 points: above the ranker's floor, and nowhere near breaking news."""

FILLER = from_feed("techcrunch_rss.xml", "techcrunch", "https://techcrunch.com/?p=99887")
"""General tech press nobody upvoted: the mediocre link at 22:00."""

FIRST_PARTY_LAUNCH = from_feed("openai_atom.xml", "openai", "https://openai.com/index/local-model")
"""A vendor announcing its own model: high tier with no points at all."""


# --- the stack under test ----------------------------------------------------


class Pool:
    """A fixed pool of candidates, standing in for the eight feeds and HN."""

    def __init__(self, *items: NewsItem) -> None:
        self._items = list(items)
        self.fetches = 0

    async def fetch(self, now: datetime) -> Sequence[NewsItem]:
        self.fetches += 1
        return list(self._items)


POSTS = (
    tool_call_response('{"opener": "miren", "line": "esto acaba de salir"}'),
    tool_call_response('{"opener": "chequen", "line": "y esto tambien"}'),
    tool_call_response('{"opener": "orale", "line": "y una mas"}'),
)
"""Three different answers, because the pacer refuses identical wording twice."""


@dataclass(frozen=True, slots=True)
class Stack:
    """Everything one test needs to reach into, wired the way production is."""

    breaking: Breaking
    leg: NewsLeg
    plans: InMemoryPlanStore
    sends: InMemorySendLog
    queue: InMemoryOvernightQueue
    posted: InMemoryPostedStore
    evolution: FakeEvolution
    deepseek: FakeDeepSeek
    pool: Pool
    clock: ManualClock


def build(
    clock: ManualClock,
    pool: Pool,
    *,
    plans: InMemoryPlanStore | None = None,
    sends: InMemorySendLog | None = None,
    queue: InMemoryOvernightQueue | None = None,
    posted: InMemoryPostedStore | None = None,
    cadence: Cadence | None = None,
    envelope: Envelope | None = None,
    evolution: FakeEvolution | None = None,
    deepseek: FakeDeepSeek | None = None,
) -> Stack:
    settings = load_settings(dict(COMPLETE_ENV))
    sleeper = ManualSleeper(clock)
    evolution = evolution if evolution is not None else FakeEvolution()
    deepseek = deepseek if deepseek is not None else FakeDeepSeek(*POSTS)
    plans = plans if plans is not None else InMemoryPlanStore()
    sends = sends if sends is not None else InMemorySendLog()
    queue = queue if queue is not None else InMemoryOvernightQueue()
    posted = posted if posted is not None else InMemoryPostedStore()

    leg = NewsLeg(
        build_brain(
            settings, clock, InMemoryUsageStore(), RecordingAlerter(), http_client=deepseek.client()
        ),
        Pacer(
            EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
            sends,
            clock,
            envelope=envelope,
            sleeper=sleeper,
            rng=random.Random(SEED),
        ),
        pool,
        posted,
        clock,
    )
    return Stack(
        breaking=Breaking(leg, GROUP, plans, sends, queue, clock, cadence=cadence),
        leg=leg,
        plans=plans,
        sends=sends,
        queue=queue,
        posted=posted,
        evolution=evolution,
        deepseek=deepseek,
        pool=pool,
        clock=clock,
    )


async def seed_send(
    sends: InMemorySendLog,
    when: datetime,
    *,
    kind: SendKind = SendKind.REPLY,
    text: str = "ahi va",
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


async def register(plans: InMemoryPlanStore, *slots: Slot, day: date = SATURDAY) -> None:
    await plans.register(DayPlan(day=day, slots=slots))


def states(plan: DayPlan | None) -> dict[str, SlotState]:
    assert plan is not None
    return {one.window: one.state for one in plan.slots}


# --- an override in the middle of the afternoon ------------------------------


async def test_a_big_story_mid_afternoon_posts_off_schedule() -> None:
    """The headline rule: it does not wait for the next window."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN, ORDINARY))
    await register(stack.plans, EVENING)

    posted = await stack.breaking.check()

    assert posted is not None
    assert posted.item.canonical_url == TOP_OF_HN.canonical_url
    assert stack.evolution.texts == [
        "miren esto acaba de salir\nhttps://www.anthropic.com/news/claude-5"
    ]
    latest = await stack.sends.latest()
    assert latest is not None and latest.kind is SendKind.POST


async def test_the_day_goes_from_four_posts_to_five_not_four_with_one_displaced() -> None:
    """Additive, per section 4. Delaying real news to the next window is exactly
    the restriction this rule exists to avoid, and so is spending a window on it."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN, ORDINARY))
    await register(stack.plans, EVENING)

    await stack.breaking.check()

    plan = await stack.plans.plan_on(SATURDAY)
    assert plan is not None
    assert len(plan.slots) == 2, "the day gained a slot rather than spending one"
    assert states(plan) == {"breaking-1": SlotState.POSTED, "evening": SlotState.PLANNED}
    assert [one.window for one in plan.pending] == ["evening"]


async def test_the_override_is_written_into_the_day_as_a_high_tier_slot() -> None:
    """So a restart at 17:00 can see the day already had its big moment, and so
    the plan stays the record of what happened rather than of what was drawn."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN))

    posted = await stack.breaking.check()

    plan = await stack.plans.plan_on(SATURDAY)
    assert plan is not None and posted is not None
    written = plan.slots[0]
    assert written.tier is Tier.HIGH
    assert written.state is SlotState.POSTED
    assert written.at == posted.message.at, "the day records when the group saw it"


async def test_two_big_stories_in_one_day_are_two_override_slots() -> None:
    """The plan's key is the day and the window name, so a second override needs
    a name of its own rather than overwriting the first."""
    clock = ManualClock(at(time(9, 0)))
    stack = build(clock, Pool(TOP_OF_HN, FIRST_PARTY_LAUNCH))

    await stack.breaking.check()
    clock.advance(timedelta(hours=2))
    await stack.breaking.check()

    plan = await stack.plans.plan_on(SATURDAY)
    assert plan is not None
    assert [one.window for one in plan.slots] == ["breaking-1", "breaking-2"]


async def test_an_ordinary_day_never_reaches_the_override_at_all() -> None:
    """Nothing in the pool is big, so nothing jumps the plan and no token is spent."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(ORDINARY, FILLER))
    await register(stack.plans, EVENING)

    assert await stack.breaking.check() is None
    assert stack.deepseek.requests == [], "the tier is decided before anything is paid for"
    assert stack.evolution.texts == []
    assert states(await stack.plans.plan_on(SATURDAY)) == {"evening": SlotState.PLANNED}


async def test_an_item_already_posted_is_not_breaking_news_a_second_time() -> None:
    """The anti-repost gate is the news leg's, and the override goes through it."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN))
    await stack.posted.remember(TOP_OF_HN, clock.now())

    assert await stack.breaking.check() is None
    assert stack.evolution.texts == []


# --- the envelope still has the last word ------------------------------------


async def test_a_big_story_still_waits_out_the_minimum_gap() -> None:
    """It jumps the plan, never the envelope: the last post was twenty minutes
    ago and consecutive posts sit 75 to 90 minutes apart."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN))
    await seed_send(stack.sends, at(time(16, 10)), kind=SendKind.POST, text="la anterior")

    assert await stack.breaking.check() is None
    assert stack.evolution.texts == []
    assert await stack.queue.waiting() == [], "a daytime item waits for the next look, not for dawn"


async def test_a_big_story_still_waits_out_the_hourly_ceiling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN))
    for offset, text in ((50, "una"), (40, "dos"), (30, "tres")):
        await seed_send(stack.sends, clock.now() - timedelta(minutes=offset), text=text)

    with caplog.at_level(logging.INFO):
        assert await stack.breaking.check() is None

    assert stack.evolution.texts == []
    assert "hourly_ceiling" in caplog.text


async def test_a_big_story_waits_for_a_conversation_to_finish() -> None:
    """Dropping a link a minute after answering somebody reads as two programs
    running side by side, and a launch is not an excuse to do it."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN))
    await seed_send(stack.sends, clock.now() - timedelta(minutes=1))

    assert await stack.breaking.check() is None
    assert stack.deepseek.requests == [], "nothing is written for a post that is not going yet"

    clock.advance(timedelta(minutes=25))
    assert await stack.breaking.check() is not None


async def test_the_conversation_wait_is_the_same_answer_every_time_it_is_asked() -> None:
    """The watch comes round every twenty to forty minutes, so a wait redrawn per
    look would be the *maximum* of its draws. It is read off the send instead."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN))
    await seed_send(stack.sends, clock.now() - timedelta(minutes=1))

    for _ in range(5):
        assert await stack.breaking.check() is None
    clock.advance(timedelta(minutes=20))

    assert await stack.breaking.check() is not None


# --- the practical stop ------------------------------------------------------


async def test_the_eighth_post_of_the_day_still_goes_out() -> None:
    clock = ManualClock(at(time(22, 0)))
    stack = build(clock, Pool(TOP_OF_HN))
    for hour in range(7):
        await seed_send(
            stack.sends, at(time(8 + hour, 0)), kind=SendKind.POST, text=f"la numero {hour}"
        )

    assert await stack.breaking.check() is not None


async def test_the_ninth_is_the_practical_stop_long_before_the_anti_ban_ceiling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Eight posts shapes a normal day; twelve sends exists to catch a runaway
    loop. A day that hit the second number would already have stopped being one."""
    clock = ManualClock(at(time(22, 0)))
    stack = build(clock, Pool(TOP_OF_HN))
    for hour in range(8):
        await seed_send(
            stack.sends, at(time(8 + hour, 0)), kind=SendKind.POST, text=f"la numero {hour}"
        )

    with caplog.at_level(logging.INFO):
        assert await stack.breaking.check() is None

    assert stack.evolution.texts == []
    assert stack.deepseek.requests == []
    assert "practical stop" in caplog.text
    assert await stack.sends.count_on(SATURDAY) < Envelope().sends_per_day


async def test_the_practical_stop_counts_posts_and_not_her_replies() -> None:
    """Eight is about how much of the group's day is Rebe sharing links. Somebody
    asking her a question is not her filling the day up."""
    clock = ManualClock(at(time(22, 0)))
    stack = build(clock, Pool(TOP_OF_HN))
    for hour in range(8):
        await seed_send(stack.sends, at(time(8 + hour, 0)), text=f"respuesta {hour}")

    assert await stack.breaking.check() is not None


async def test_a_brain_that_gives_no_answer_costs_the_override_and_not_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The look runs inside the day's loop, where an exception would end the
    process. Everything that can go wrong with one post stays with that post."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN), deepseek=FakeDeepSeek(500))

    with caplog.at_level(logging.WARNING):
        assert await stack.breaking.check() is None

    assert stack.evolution.texts == []
    assert "did not get out" in caplog.text


async def test_evolution_being_down_costs_the_override_and_not_the_loop() -> None:
    clock = ManualClock(at(AFTERNOON))
    broken = FakeEvolution()
    broken.text_status = 500
    stack = build(clock, Pool(TOP_OF_HN), evolution=broken)

    assert await stack.breaking.check() is None
    assert await stack.plans.plan_on(SATURDAY) is None, "nothing was written down"


async def test_a_morning_slot_falls_back_to_its_own_pool_when_the_brain_is_down() -> None:
    """A slot has somewhere to fall back to, so a queued item nobody could write
    up leaves the slot to the normal pool rather than ending the day."""
    clock = ManualClock(NIGHT)
    stack = build(clock, Pool(overnight_item()), deepseek=FakeDeepSeek(500))
    await stack.breaking.check()

    clock.set(at(time(9, 14), SUNDAY))

    assert await stack.breaking.claim_slot() is None
    assert len(await stack.queue.waiting()) == 1, "and the story is still held"


# --- pruning what is left of the day -----------------------------------------


async def test_a_remaining_slot_with_only_a_weak_candidate_is_pruned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A person who just shared the big thing does not also drop a roundup at
    22:00, and the reason goes in the log so a thin day can be told from this one."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN, FILLER))
    await register(stack.plans, EVENING, LATE)

    with caplog.at_level(logging.INFO):
        await stack.breaking.check()

    assert states(await stack.plans.plan_on(SATURDAY)) == {
        "breaking-1": SlotState.POSTED,
        "evening": SlotState.PRUNED,
        "late": SlotState.PRUNED,
    }
    assert "pruning the evening slot" in caplog.text
    assert "nothing at all" in caplog.text, "the last slot had no candidate left at all"


async def test_a_remaining_slot_with_a_strong_candidate_is_left_alone() -> None:
    """Each slot is paired with the candidate it would actually get: the evening
    still has a good link, and only the one after it is filler."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN, ORDINARY, FILLER))
    await register(stack.plans, EVENING, LATE)

    await stack.breaking.check()

    assert states(await stack.plans.plan_on(SATURDAY)) == {
        "breaking-1": SlotState.POSTED,
        "evening": SlotState.PLANNED,
        "late": SlotState.PRUNED,
    }


async def test_a_high_tier_slot_is_never_pruned() -> None:
    """Section 4 is explicit about it, so it is a rule here rather than an
    accident of overrides being written down already posted."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN, FILLER))
    held = slot(time(20, 0), window="held-over", closes=time(21, 0), tier=Tier.HIGH)
    await register(stack.plans, held, LATE)

    await stack.breaking.check()

    assert states(await stack.plans.plan_on(SATURDAY)) == {
        "breaking-1": SlotState.POSTED,
        "held-over": SlotState.PLANNED,
        "late": SlotState.PRUNED,
    }


async def test_a_slot_that_already_happened_is_not_pruned_after_the_fact() -> None:
    """Pruning is about what is left of the day. What already went out is history."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(TOP_OF_HN, FILLER))
    await register(stack.plans, slot(time(14, 30), window="midday", closes=time(16, 30)))
    await stack.plans.settle(SATURDAY, "midday", SlotState.POSTED)

    await stack.breaking.check()

    assert states(await stack.plans.plan_on(SATURDAY))["midday"] is SlotState.POSTED


async def test_nothing_is_pruned_when_no_big_story_went_out() -> None:
    """The pruning rule hangs off the override, not off a weak pool: a thin day
    is still a day, and its slots skip themselves one at a time."""
    clock = ManualClock(at(AFTERNOON))
    stack = build(clock, Pool(FILLER))
    await register(stack.plans, EVENING, LATE)

    await stack.breaking.check()

    assert states(await stack.plans.plan_on(SATURDAY)) == {
        "evening": SlotState.PLANNED,
        "late": SlotState.PLANNED,
    }


# --- what breaks while she is asleep -----------------------------------------


NIGHT = at(time(3, 0), SUNDAY)


def overnight_item(**kwargs: object) -> NewsItem:
    """A launch that broke in the small hours, an hour before Rebe would see it."""
    return item(published_at=NIGHT - timedelta(hours=1), **kwargs)  # type: ignore[arg-type]


async def test_a_launch_at_three_in_the_morning_posts_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A person who reliably posts AI links at 03:00 is not a person, and one such
    post is enough to give the game away to anybody scrolling back."""
    clock = ManualClock(NIGHT)
    breaking_news = overnight_item()
    stack = build(clock, Pool(breaking_news))

    with caplog.at_level(logging.INFO):
        assert await stack.breaking.check() is None

    assert stack.evolution.texts == []
    assert stack.deepseek.requests == [], "nothing is written at 03:00 either"
    assert [held.source_key for held in await stack.queue.waiting()] == [breaking_news.source_key]
    assert "holding them for the morning window" in caplog.text


async def test_the_queue_goes_out_when_the_morning_window_opens() -> None:
    """A few hours old by morning is something nobody in the group will notice."""
    clock = ManualClock(NIGHT)
    breaking_news = overnight_item()
    stack = build(clock, Pool(breaking_news))
    await stack.breaking.check()

    clock.set(at(time(9, 14), SUNDAY))
    posted = await stack.breaking.claim_slot()

    assert posted is not None
    assert posted.item.source_key == breaking_news.source_key
    assert len(stack.evolution.texts) == 1


async def test_the_overnight_item_goes_ahead_of_everything_else_in_the_queue() -> None:
    """Section 6's "ahead of everything else": the morning's own pool has a
    fresher item that outranks it, and the night's story still takes the slot."""
    clock = ManualClock(NIGHT)
    held = overnight_item(source_id="held-overnight", title="OpenAI lanza un modelo local")
    fresher = item(
        source="deepmind",
        source_id="this-morning",
        title="DeepMind lanza un modelo de video",
        url="https://deepmind.google/discover/blog/video",
        published_at=at(time(8, 30), SUNDAY),
    )
    pool = Pool(held, fresher)
    stack = build(clock, pool)
    await stack.breaking.check()

    clock.set(at(time(9, 14), SUNDAY))
    ranked = await stack.leg.unposted(clock.now())
    assert ranked[0].source_id == "this-morning", "the pool alone would have posted the fresh one"

    posted = await stack.breaking.claim_slot()

    assert posted is not None and posted.item.source_id == "held-overnight"


async def test_only_the_strongest_of_two_overnight_items_takes_the_morning_slot() -> None:
    """The rest fall back to normal tier: nothing is holding them any more, so
    they compete for the later windows on the ranker's terms like anything else."""
    clock = ManualClock(NIGHT)
    stronger = overnight_item(source_id="stronger", authority=1.0)
    weaker = overnight_item(
        source="huggingface",
        source_id="weaker",
        title="Hugging Face lanza un espacio nuevo",
        url="https://huggingface.co/blog/espacio",
        authority=0.85,
    )
    stack = build(clock, Pool(stronger, weaker))
    await stack.breaking.check()
    assert len(await stack.queue.waiting()) == 2

    clock.set(at(time(9, 14), SUNDAY))
    posted = await stack.breaking.claim_slot()

    assert posted is not None and posted.item.source_id == "stronger"
    assert await stack.queue.waiting() == [], "the weaker one is demoted, not held for tomorrow"
    assert not await stack.posted.knows(weaker), "so it can still win a later window"


async def test_the_look_at_08_20_leaves_the_queue_to_the_morning_slot() -> None:
    """The hold lifts at 08:00 and the watch comes round every twenty to forty
    minutes, so without this the night's story would go out at 08:20 as an
    off-schedule extra - not "when the morning window opens, jittered as usual",
    and a fifth post instead of the morning's own."""
    clock = ManualClock(NIGHT)
    stack = build(clock, Pool(overnight_item()))
    await stack.breaking.check()

    clock.set(at(time(8, 20), SUNDAY))
    assert await stack.breaking.check() is None
    assert stack.evolution.texts == []
    assert len(await stack.queue.waiting()) == 1

    clock.set(at(time(9, 14), SUNDAY))
    assert await stack.breaking.claim_slot() is not None


async def test_the_demoted_item_is_never_posted_as_an_override_afterwards() -> None:
    """ "Fall back to normal tier" has to mean it: an item the morning passed over
    is still a launch by the letter of the rule, and a look at 11:00 that treated
    it as breaking news would give the day two overrides out of one night."""
    clock = ManualClock(NIGHT)
    weaker = overnight_item(
        source="huggingface",
        source_id="weaker",
        title="Hugging Face lanza un espacio nuevo",
        url="https://huggingface.co/blog/espacio",
        authority=0.85,
    )
    stack = build(clock, Pool(overnight_item(source_id="stronger", authority=1.0), weaker))
    await stack.breaking.check()
    clock.set(at(time(9, 14), SUNDAY))
    await stack.breaking.claim_slot()

    clock.set(at(time(11, 0), SUNDAY))
    assert await stack.breaking.check() is None
    assert len(stack.evolution.texts) == 1

    posted = await stack.leg.run(GROUP)
    assert [one.item.source_id for one in posted] == ["weaker"], "it competes, as a normal item"


async def test_the_morning_slot_is_only_claimed_once() -> None:
    clock = ManualClock(NIGHT)
    stack = build(clock, Pool(overnight_item()))
    await stack.breaking.check()

    clock.set(at(time(9, 14), SUNDAY))
    assert await stack.breaking.claim_slot() is not None
    clock.advance(timedelta(hours=4))

    assert await stack.breaking.claim_slot() is None, "the day's later slots are the pool's again"


async def test_a_slot_with_an_empty_queue_is_left_to_the_normal_pool() -> None:
    clock = ManualClock(at(time(9, 14), SUNDAY))
    stack = build(clock, Pool(TOP_OF_HN))

    assert await stack.breaking.claim_slot() is None
    assert stack.evolution.texts == []


async def test_a_queued_item_the_night_outlived_is_dropped_rather_than_posted_stale() -> None:
    """The queue is read back through the curator on the morning's clock, so the
    freshness window applies to it like it applies to anything else."""
    clock = ManualClock(NIGHT)
    stack = build(clock, Pool(overnight_item()))
    await stack.breaking.check()

    clock.set(at(time(9, 14), SUNDAY) + timedelta(days=3))

    assert await stack.breaking.claim_slot() is None
    assert await stack.queue.waiting() == []
    assert stack.evolution.texts == []


async def test_the_same_story_offered_all_night_is_queued_once() -> None:
    """The watch looks at the pool every twenty to forty minutes until dawn."""
    clock = ManualClock(NIGHT)
    stack = build(clock, Pool(overnight_item()))

    for _ in range(6):
        await stack.breaking.check()
        clock.advance(timedelta(minutes=30))

    assert len(await stack.queue.waiting()) == 1


async def test_the_overnight_queue_outlives_the_process_that_filled_it() -> None:
    """Proved against a real Postgres in `tests/test_overnight_store.py`; what is
    proved here is that the queue is the only thing that remembers, so a second
    process built over the same store picks the night up where the first left it."""
    queue = InMemoryOvernightQueue()
    night = build(ManualClock(NIGHT), Pool(overnight_item()), queue=queue)
    await night.breaking.check()

    morning_clock = ManualClock(at(time(9, 14), SUNDAY))
    morning = build(morning_clock, Pool(), queue=queue)
    posted = await morning.breaking.claim_slot()

    assert posted is not None
    assert len(morning.evolution.texts) == 1


# --- the whole thing, driven by the real scheduler ---------------------------


async def test_an_afternoon_launch_lands_between_two_slots_and_the_day_runs_on() -> None:
    """Section 4's worked example, one step of the real loop at a time: the day is
    waiting for its evening slot, a launch lands at 16:00, the loop notices it
    within the half hour and posts it, and the evening slot still happens."""
    clock = ManualClock(at(time(16, 0)))
    launch = item(
        source_id="afternoon-launch",
        title="OpenAI lanza un modelo que corre local",
        published_at=at(time(15, 30)),
    )
    later = item(
        source="deepmind",
        source_id="for-the-evening",
        title="Un repaso a los modelos que caben en un telefono",
        url="https://deepmind.google/discover/blog/repaso",
        published_at=at(time(14, 0)),
    )
    stack = build(clock, Pool(launch, later))
    await register(stack.plans, EVENING)
    scheduler = Scheduler(
        stack.leg,
        GROUP,
        stack.plans,
        stack.sends,
        clock,
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
        breaking=stack.breaking,
    )

    for _ in range(12):
        await scheduler.step()

    plan = await stack.plans.plan_on(SATURDAY)
    assert states(plan) == {"breaking-1": SlotState.POSTED, "evening": SlotState.POSTED}
    assert len(stack.evolution.texts) == 2, "the day went to two posts, not one"
    assert [record.kind for record in await stack.sends.since(at(time(16, 0)))] == [
        SendKind.POST,
        SendKind.POST,
    ]
    assert plan is not None
    assert plan.slots[0].at < at(time(16, 40)), "she posted it when she saw it"


async def test_the_hold_is_about_the_hour_and_not_about_the_item() -> None:
    """The same story, an hour after the window opened, is simply posted: what
    queued it at 03:00 was the clock, and nothing about it is queued afterwards."""
    clock = ManualClock(at(time(9, 0), SUNDAY))
    stack = build(clock, Pool(overnight_item()))

    assert await stack.breaking.check() is not None
    assert await stack.queue.waiting() == []
